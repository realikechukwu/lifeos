"""Structured extraction for a short Telegram clarification conversation.

This intentionally lives beside, rather than inside, the Gmail extractor so
the established email prompt and processing behaviour remain unchanged.
"""

from datetime import date

from django.conf import settings
from openai import OpenAI

from .extractor import AssistantAction, SYSTEM_PROMPT


TELEGRAM_CONTEXT_PROMPT = """The untrusted content is a Telegram conversation
between one authorised household member and LifeOS. Interpret later user
messages as answers or corrections to the same original request. The sender's
household role is supplied separately and is trusted application context:
resolve first-person words such as "me" and "my" to that role where the schema
requires ike/wife. Do not treat assistant clarification questions as new
household actions. Extract the latest complete version of exactly one action.
"""


def build_telegram_messages(
    transcript: list[dict], *, current_date: date, sender_role: str
) -> list[dict]:
    lines = []
    for item in transcript:
        speaker = "LifeOS" if item.get("role") == "assistant" else "Household member"
        lines.append(f"{speaker}: {str(item.get('text', ''))[:4000]}")

    role = sender_role if sender_role in {"ike", "wife"} else "unknown"
    user_content = (
        f"Current date: {current_date.isoformat()}\n"
        f"Authorised sender role: {role}\n\n"
        "<untrusted_telegram_conversation>\n"
        + "\n".join(lines)
        + "\n</untrusted_telegram_conversation>\n\n"
        "Extract the single, latest household action represented by this conversation."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": TELEGRAM_CONTEXT_PROMPT},
        {"role": "user", "content": user_content},
    ]


def extract_telegram_action(
    transcript: list[dict], *, current_date: date, sender_role: str
) -> AssistantAction:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=build_telegram_messages(
            transcript, current_date=current_date, sender_role=sender_role
        ),
        response_format=AssistantAction,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError("OpenAI response did not contain a parsed Telegram action.")
    return parsed
