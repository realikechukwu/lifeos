"""Telegram channel for the agent: queueing from the webhook, thread
selection, per-turn context, and sending the reply."""

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    AgentJob,
    AgentMemory,
    AgentOutcome,
    AgentMessage,
    AgentThread,
    HouseholdMember,
    TelegramChat,
    TelegramUser,
)

from assistant.services.telegram import TelegramAPIError, TelegramBot

from .formatting import split_message, to_telegram_html
from .loop import append_input, run_turn
from .tools import ToolContext
from .documents import send_telegram_documents, upload_context
from .proposals import context_outcomes, send_cards

logger = logging.getLogger(__name__)

TYPING_REFRESH_SECONDS = 4.5
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."


def enqueue_telegram_message(
    *, chat: TelegramChat, user: TelegramUser, text: str, update_id: int, message_id: int | None
) -> None:
    """Queue a message for the worker. Idempotent per Telegram update."""
    try:
        with transaction.atomic():
            AgentJob.objects.create(
                channel=AgentThread.Channel.TELEGRAM,
                telegram_chat=chat,
                telegram_user=user,
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                text=text[:8000],
            )
    except IntegrityError:
        # Telegram redelivered an update that is already queued.
        pass


def close_thread(chat: TelegramChat, user: TelegramUser) -> bool:
    return bool(
        AgentThread.objects.filter(
            channel=AgentThread.Channel.TELEGRAM,
            telegram_chat=chat,
            telegram_user=user,
            closed_at__isnull=True,
        ).update(closed_at=timezone.now())
    )


def _thread_for(chat: TelegramChat, user: TelegramUser) -> AgentThread:
    now = timezone.now()
    thread = (
        AgentThread.objects.filter(
            channel=AgentThread.Channel.TELEGRAM,
            telegram_chat=chat,
            telegram_user=user,
            closed_at__isnull=True,
        )
        .order_by("-last_activity_at")
        .first()
    )
    idle_limit = timedelta(hours=settings.AGENT_THREAD_IDLE_HOURS)
    if thread and now - thread.last_activity_at > idle_limit:
        thread.closed_at = now
        thread.save(update_fields=["closed_at"])
        thread = None
    if thread is None:
        thread = AgentThread.objects.create(
            channel=AgentThread.Channel.TELEGRAM,
            role=user.role,
            telegram_chat=chat,
            telegram_user=user,
            last_activity_at=now,
        )
    return thread


def speaker_name(user: TelegramUser) -> str:
    if user.role == HouseholdMember.Role.IKE:
        return "Ike"
    return user.display_name or "Ike's wife"


def build_context_note(*, thread: AgentThread, chat: TelegramChat, user: TelegramUser, first_turn: bool) -> str:
    """Trusted per-turn facts, sent as a system message after the user's words."""
    now = timezone.localtime()
    where = (
        "a private Telegram chat"
        if chat.chat_type == TelegramChat.ChatType.PRIVATE
        else "the family Telegram group, where both adults can read your reply"
    )
    lines = [
        f"Current time: {now:%A %-d %B %Y, %H:%M} (Europe/London).",
        f"Speaker: {speaker_name(user)} (household role: {user.role}), writing in {where}.",
    ]
    if first_turn:
        memories = list(AgentMemory.objects.filter(role=user.role, active=True))
        if memories:
            lines.append(f"Saved memories for {speaker_name(user)}:")
            lines.extend(f"- [{m.id}] {m.text}" for m in memories)
        else:
            lines.append(f"No saved memories for {speaker_name(user)} yet.")
    return "\n".join(lines)


class _TypingIndicator:
    """Keeps "typing…" visible while the agent works (Telegram expires it after ~5s)."""

    def __init__(self, bot: TelegramBot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._bot.send_chat_action(self._chat_id, "typing")
            except TelegramAPIError:
                pass
            self._stop.wait(TYPING_REFRESH_SECONDS)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1)


def _send_reply(bot: TelegramBot, job: AgentJob, text: str) -> None:
    reply_to = job.telegram_message_id if job.telegram_chat.chat_type != TelegramChat.ChatType.PRIVATE else None
    for chunk in split_message(text):
        bot.send_message(job.telegram_chat.chat_id, to_telegram_html(chunk), reply_to_message_id=reply_to)
        reply_to = None


def process_telegram_jobs(jobs: list[AgentJob], *, bot: TelegramBot | None = None, client=None) -> None:
    """Answer one or more queued messages from the same person in the same chat
    as a single turn (several quick messages read as one)."""
    job = jobs[0]
    chat, user = job.telegram_chat, job.telegram_user
    bot = bot or TelegramBot()
    if job.thread_id is not None:
        # A retry: this turn's input is already in the thread.
        thread = job.thread
    else:
        thread = _thread_for(chat, user)
        first_turn = not thread.messages.exists()
        text = "\n\n".join(j.text for j in jobs if j.text.strip())
        blocks = []
        for item in jobs:
            blocks.extend(item.input_blocks or [{"type": "text", "text": item.text}])
        upload_note = upload_context(ToolContext(role=user.role, speaker_name=speaker_name(user), thread=thread, job=job))
        outcome_note, outcome_ids = context_outcomes(thread)
        batch_metadata = {"batch_id": job.pk}
        with transaction.atomic():
            append_input(thread, [
                {"role": AgentMessage.Role.USER, "content": blocks if any(j.input_blocks for j in jobs) else [{"type": "text", "text": text}]},
                {
                    "role": AgentMessage.Role.SYSTEM,
                    "content": build_context_note(thread=thread, chat=chat, user=user, first_turn=first_turn) + ("\n" + outcome_note if outcome_note else "") + "\n" + upload_note,
                },
            ])
            AgentOutcome.objects.filter(id__in=outcome_ids).update(delivered_at=timezone.now())
            AgentJob.objects.filter(id__in=[j.id for j in jobs]).update(thread=thread, reply_metadata=batch_metadata)
        job.reply_metadata = batch_metadata
    ctx = ToolContext(
        role=user.role,
        thread=thread,
        job=job,
        speaker_name=speaker_name(user),
        telegram_chat=chat,
        telegram_user=user,
        telegram_update_id=job.telegram_update_id,
        bot=bot,
    )
    if not job.reply_metadata.get("model_finished"):
        with _TypingIndicator(bot, chat.chat_id):
            result = run_turn(thread, ctx, client=client)
        job.reply_text = result.text
        job.reply_metadata["model_finished"] = True
        AgentJob.objects.filter(pk=job.pk).update(reply_text=job.reply_text, reply_metadata=job.reply_metadata)
    if not job.reply_metadata.get("text_sent"):
        if job.reply_text:
            _send_reply(bot, job, job.reply_text)
        job.reply_metadata["text_sent"] = True
        AgentJob.objects.filter(pk=job.pk).update(reply_metadata=job.reply_metadata)
    send_cards(thread, bot)
    for item in jobs:
        send_telegram_documents(item, bot)


def notify_failure(job: AgentJob, *, bot: TelegramBot | None = None) -> None:
    try:
        (bot or TelegramBot()).send_message(job.telegram_chat.chat_id, ERROR_REPLY)
    except TelegramAPIError:
        logger.warning("Could not tell chat about failed agent job %s", job.id)
