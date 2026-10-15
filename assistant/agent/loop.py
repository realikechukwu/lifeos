"""The agent loop: send the thread to Claude, run requested tools, repeat.

History is append-only. Every message is stored exactly as sent or received
and replayed unchanged on later turns, which keeps the prompt cache warm and
keeps Claude's thinking blocks valid across turns.
"""

import hashlib
import json
import logging
from dataclasses import dataclass

import anthropic
from django.conf import settings
from django.utils import timezone

from core.models import AgentMessage, AgentThread

from .prompts import SYSTEM_PROMPT
from .tools import HANDLERS, ACTIVE_TOOL_DEFINITIONS, ToolContext, ToolInputError

logger = logging.getLogger(__name__)

# Replies often come 5-60 minutes apart, so the 1-hour cache pays for itself.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE_CONTROL}]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Thinking blocks are bound to the system prompt and tools they were written
# under. If a deploy changes those mid-thread, drop the stale blocks instead of
# failing the request (threads are also rotated, see PROMPT_VERSION).
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
MAX_TOKENS = 32000


def _prompt_version() -> str:
    """Fingerprint of everything a thread's history is bound to."""
    payload = json.dumps(
        [settings.AGENT_MODEL, SYSTEM_PROMPT, ACTIVE_TOOL_DEFINITIONS], sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


PROMPT_VERSION = _prompt_version()

REFUSAL_REPLY = "Sorry, I can't help with that one."
INTERRUPTED_RESULT = (
    "This tool call was interrupted. A write may already have run; do not repeat writes "
    "automatically. Check recorded outcomes or ask the speaker first. Read-only lookups can be retried."
)
READ_ONLY_TOOLS = {
    "get_calendar", "list_tasks", "list_reminders", "search_notes",
    "list_profile_documents", "read_profile_document", "list_generated_documents",
}


@dataclass
class TurnResult:
    text: str
    stop_reason: str


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY or None)


def _history(thread: AgentThread) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in thread.messages.all()]


def _append(thread: AgentThread, role: str, content: list, response=None) -> dict:
    fields = {}
    if response is not None:
        usage = response.usage
        fields = {
            "model": response.model or "",
            "stop_reason": response.stop_reason or "",
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_input_tokens or 0,
            "cache_creation_tokens": usage.cache_creation_input_tokens or 0,
        }
        logger.info(
            "agent response thread=%s stop=%s input=%s cache_read=%s cache_write=%s output=%s",
            thread.id, response.stop_reason, usage.input_tokens,
            fields["cache_read_tokens"], fields["cache_creation_tokens"], usage.output_tokens,
        )
    AgentMessage.objects.create(thread=thread, role=role, content=content, **fields)
    return {"role": role, "content": content}


def _close_dangling_tool_calls(thread: AgentThread, history: list[dict]) -> bool:
    """If a previous attempt died between a tool call and its result, record
    an uncertain result so the history is valid to send again."""
    if not history or history[-1]["role"] != AgentMessage.Role.ASSISTANT:
        return False
    pending = [b for b in history[-1]["content"] if b.get("type") == "tool_use"]
    if pending:
        results = [
            {"type": "tool_result", "tool_use_id": b["id"], "content": INTERRUPTED_RESULT, "is_error": True}
            for b in pending
        ]
        history.append(_append(thread, AgentMessage.Role.USER, results))
    return any(b.get("name") not in READ_ONLY_TOOLS for b in pending)


def _run_tool(ctx: ToolContext, block) -> dict:
    handler = HANDLERS.get(block.name)
    result = {"type": "tool_result", "tool_use_id": block.id}
    if handler is None:
        return {**result, "content": f"Unknown tool {block.name!r}.", "is_error": True}
    try:
        return {**result, "content": handler(ctx, dict(block.input or {}))}
    except ToolInputError as exc:
        return {**result, "content": str(exc), "is_error": True}
    except Exception:  # noqa: BLE001 - report to the model instead of failing the turn
        logger.exception("agent tool %s failed", block.name)
        return {**result, "content": "The tool failed unexpectedly. Tell the speaker it didn't work.", "is_error": True}


def _text_of(content: list) -> str:
    return "\n\n".join(b.text.strip() for b in content if b.type == "text" and b.text.strip())


def append_input(thread: AgentThread, new_messages: list[dict]) -> None:
    """Record the incoming turn before calling the model, so a retried job
    continues the same turn instead of repeating it."""
    history = _history(thread)
    _close_dangling_tool_calls(thread, history)
    if history and history[-1]["role"] == AgentMessage.Role.SYSTEM:
        _append(thread, AgentMessage.Role.ASSISTANT, [{
            "type": "text", "text": "The previous attempt ended before a reply was produced.",
        }])
    for message in new_messages:
        _append(thread, message["role"], message["content"])


def run_turn(thread: AgentThread, ctx: ToolContext, *, client=None) -> TurnResult:
    """Run Claude on the thread as it stands until it answers."""
    client = client or _client()
    history = _history(thread)
    uncertain_write = _close_dangling_tool_calls(thread, history)

    max_rounds = 0 if uncertain_write else settings.AGENT_MAX_TOOL_ROUNDS
    tool_rounds = 0
    for _ in range(max_rounds + 5):  # tool rounds plus a few server-side pauses
        params = dict(
            model=settings.AGENT_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_BLOCKS,
            tools=ACTIVE_TOOL_DEFINITIONS,
            messages=history,
            cache_control=CACHE_CONTROL,
            thinking=THINKING,
            output_config={"effort": settings.AGENT_EFFORT},
            betas=[FALLBACK_BETA, THINKING_BINDING_BETA],
            fallbacks="default",
        )
        if tool_rounds >= max_rounds:
            # Out of tool budget: the model must answer with what it has.
            params["tool_choice"] = {"type": "none"}
        with client.beta.messages.stream(**params) as stream:
            response = stream.get_final_message()
        content = [block.to_dict(mode="json") for block in response.content]
        history.append(_append(thread, AgentMessage.Role.ASSISTANT, content, response))
        thread.last_activity_at = timezone.now()
        thread.save(update_fields=["last_activity_at"])

        if response.stop_reason == "refusal":
            return TurnResult(REFUSAL_REPLY, "refusal")
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason == "tool_use":
            tool_rounds += 1
            results = [_run_tool(ctx, b) for b in response.content if b.type == "tool_use"]
            history.append(_append(thread, AgentMessage.Role.USER, results))
            continue
        text = _text_of(response.content)
        if response.stop_reason == "max_tokens":
            text = (text + "\n\n(I ran out of room there; ask me to continue.)").strip()
        return TurnResult(text, response.stop_reason or "")
    return TurnResult("Sorry, that took too many steps. Could you try asking more simply?", "round_limit")
