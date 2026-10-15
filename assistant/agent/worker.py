"""Claiming and running queued agent jobs.

There is one worker process, so jobs run one at a time in arrival order.
Claims are conditional updates rather than row locks, which behaves the same
on SQLite and on Supabase's transaction pooler.
"""

import logging
from datetime import timedelta

import anthropic
from django.db.models import F
from django.utils import timezone
from googleapiclient.errors import HttpError

from core.models import AgentJob

from .telegram import notify_failure, process_telegram_jobs

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(seconds=20)


def _claim(job_ids: list[int]) -> list[int]:
    claimed = []
    for job_id in job_ids:
        if AgentJob.objects.filter(id=job_id, status=AgentJob.Status.QUEUED).update(
            status=AgentJob.Status.RUNNING, started_at=timezone.now(), attempts=F("attempts") + 1
        ):
            claimed.append(job_id)
    return claimed


def claim_next_jobs() -> list[AgentJob]:
    """Claim the oldest ready job plus any later queued messages from the
    same person in the same chat, so a burst of messages is one turn."""
    now = timezone.now()
    first = (
        AgentJob.objects.filter(status=AgentJob.Status.QUEUED)
        .exclude(attempts__gt=0, started_at__gt=now - RETRY_DELAY)
        .order_by("id")
        .first()
    )
    if first is None:
        return []
    if first.id not in _claim([first.id]):
        return []
    # A retried job already has its input in a thread; don't merge new messages into it.
    siblings = []
    if first.thread_id is None and first.channel == "telegram":
        siblings = list(
            AgentJob.objects.filter(
                status=AgentJob.Status.QUEUED,
                thread__isnull=True,
                channel=first.channel,
                telegram_chat_id=first.telegram_chat_id,
                telegram_user_id=first.telegram_user_id,
                id__gt=first.id,
            ).values_list("id", flat=True)
        )
    claimed = [first.id] + _claim(siblings)
    return list(AgentJob.objects.filter(id__in=claimed).select_related(
        "telegram_chat", "telegram_user", "thread", "incoming_email"
    ).order_by("id"))


def requeue_orphaned_jobs() -> int:
    """Jobs left running by a worker that stopped mid-turn go back in the queue."""
    return AgentJob.objects.filter(status=AgentJob.Status.RUNNING).update(status=AgentJob.Status.QUEUED)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, HttpError):
        return exc.resp.status in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in (408, 409, 529)


def run_jobs(jobs: list[AgentJob], *, bot=None, client=None, gmail_service=None) -> None:
    ids = [job.id for job in jobs]
    try:
        if jobs[0].channel == "email":
            from .email import process_email_job
            process_email_job(jobs[0], client=client, gmail_service=gmail_service)
        else:
            process_telegram_jobs(jobs, bot=bot, client=client)
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
        retry = _is_transient(exc) and jobs[0].attempts < MAX_ATTEMPTS
        logger.exception("agent jobs %s failed (retry=%s)", ids, retry)
        if retry:
            AgentJob.objects.filter(id__in=ids).update(status=AgentJob.Status.QUEUED, error=type(exc).__name__)
            return
        AgentJob.objects.filter(id__in=ids).update(
            status=AgentJob.Status.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000],
            finished_at=timezone.now(), text="", input_blocks=[],
        )
        if jobs[0].channel == "email":
            from .email import notify_email_failure
            jobs[0].error = f"{type(exc).__name__}: {exc}"[:2000]
            try:
                notify_email_failure(jobs[0], gmail_service=gmail_service)
            except Exception:
                logger.exception("Failed to notify email job %s", jobs[0].id)
        else:
            notify_failure(jobs[0], bot=bot)
        return
    AgentJob.objects.filter(id__in=ids).update(
        status=AgentJob.Status.DONE, error="", finished_at=timezone.now(), text="", input_blocks=[]
    )
