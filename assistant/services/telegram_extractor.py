"""Structured extraction for Telegram requests and item-level corrections."""

import json

from datetime import date

from django.conf import settings
from openai import OpenAI

from .extractor import (
    AssistantAction,
    AssistantActionBatch,
    SYSTEM_PROMPT,
    normalise_action_inferences,
)


TELEGRAM_CONTEXT_PROMPT = """The untrusted content is a Telegram conversation
between one authorised household member and LifeOS. Interpret later user
messages as answers or corrections. The sender's
household role is supplied separately and is trusted application context:
resolve first-person words such as "me" and "my" to that role where the schema
requires ike/wife. Do not treat assistant clarification questions as new
household actions. Extract every distinct requested action in order. Prefer a
reasonable concrete interpretation over clarification; Telegram will show each
item separately for confirmation before anything is changed.
"""

TELEGRAM_CURRENT_ITEM_PROMPT = """Refine exactly one current item. The original
conversation may mention other actions, but they are already queued and must not
be returned. Apply the user's latest correction only to the supplied current
item. Return the best complete version of that one item.
"""


def build_telegram_messages(
    transcript: list[dict], *, current_date: date, sender_role: str,
    current_action: dict | None = None,
) -> list[dict]:
    lines = []
    for item in transcript:
        speaker = "LifeOS" if item.get("role") == "assistant" else "Household member"
        lines.append(f"{speaker}: {str(item.get('text', ''))[:4000]}")

    role = sender_role if sender_role in {"ike", "wife"} else "unknown"
    focus = ""
    context_prompt = TELEGRAM_CONTEXT_PROMPT
    final_instruction = "Extract every distinct household action represented by this conversation."
    if current_action is not None:
        context_prompt = TELEGRAM_CURRENT_ITEM_PROMPT
        focus = (
            "\n<current_item>\n"
            + json.dumps(current_action, ensure_ascii=False)
            + "\n</current_item>\n"
        )
        final_instruction = "Return only the corrected current item."

    user_content = (
        f"Current date: {current_date.isoformat()}\n"
        "Timezone: Europe/London\n"
        f"Authorised sender role: {role}\n\n"
        "<untrusted_telegram_conversation>\n"
        + "\n".join(lines)
        + "\n</untrusted_telegram_conversation>\n"
        + focus
        + "\n"
        + final_instruction
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": context_prompt},
        {"role": "user", "content": user_content},
    ]


def extract_telegram_action(
    transcript: list[dict], *, current_date: date, sender_role: str,
    multiple: bool = False, current_action: dict | None = None,
) -> AssistantAction | list[AssistantAction]:
    """Extract a batch initially, or one focused item during edit/clarification.

    The historical function name is retained because callers and tests already
    patch this seam.
    """
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response_format = AssistantActionBatch if multiple else AssistantAction
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=build_telegram_messages(
            transcript,
            current_date=current_date,
            sender_role=sender_role,
            current_action=current_action,
        ),
        response_format=response_format,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError("OpenAI response did not contain a parsed Telegram action.")
    if multiple:
        return [normalise_action_inferences(action, current_date) for action in parsed.actions]
    return normalise_action_inferences(parsed, current_date)
