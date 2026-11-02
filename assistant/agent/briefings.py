"""Agent-written scheduled briefings: the morning brief (with inbox triage)
and the Sunday week-ahead plan.

send_telegram_briefings queues these as agent jobs in the person's private
chat, so they can reply to the brief like any other message. If the agent
fails, the original template briefing is sent instead.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from core.models import AgentJob, AgentThread, TelegramBriefingDelivery, TelegramChat, TelegramUser

logger = logging.getLogger(__name__)

DAILY = TelegramBriefingDelivery.BriefingType.DAILY
WEEKLY = TelegramBriefingDelivery.BriefingType.WEEKLY

DAILY_BRIEF_PROMPT = """Scheduled morning brief for {today:%A %-d %B %Y}.

1. Look at today, plus tomorrow before 10:00, with get_calendar (Ike's work shifts appear as "Ike — …"). Check list_tasks for anything due today or overdue, and list_reminders for today.
2. If my personal inbox is connected, triage the last 24 hours: search_my_inbox with "newer_than:1d -category:promotions -category:social", and read anything that looks actionable. Look for things I need to reply to or do, appointments and dates, bills or payments, and deadlines. Ignore marketing and newsletters. If the inbox isn't connected, skip this quietly.
3. For a date, appointment or deadline from email that isn't already in the calendar or tasks, propose it (propose_event, propose_task or propose_reminder) so I can confirm it. Never propose duplicates. Propose at most 3.
4. Apply my saved memories and standing rules.

{home}Write the brief in this shape, leaving out any section that would be empty:
**Today**: shift and key events with times, plus any clash, early start or travel concern.
**Needs you**: up to 5 items from email or tasks, one line each: who or what, the action, and by when.
**Money**: bills or payments due in the next 7 days.
{home_section}**Proposed**: one line per card you created.
Finish with one practical suggestion for the day only if there's an obvious one. Keep it under 200 words. Never paste whole emails. If there's genuinely nothing to report, say so in one line."""

WEEKLY_PLAN_PROMPT = """Scheduled week-ahead plan for {start:%A %-d %B} to {end:%A %-d %B %Y}.

1. Read the whole week in one get_calendar call (Ike's work shifts appear as "Ike — …"). Check list_tasks for anything due this week or overdue, and list_reminders for this week.
2. If my personal inbox is connected, scan the last 7 days with search_my_inbox "newer_than:7d -category:promotions -category:social" for dates, deadlines or actions that fall this week. If it isn't connected, skip this quietly.
3. Think like a chief of staff. Spot clashes (commitments during shifts, early starts after a late or night shift, childcare or transport gaps), deadlines at risk, and genuinely free windows.
4. Propose only clearly useful items with propose_* tools: missing events or deadlines from email, or a reminder to prepare, book, buy or pay something. At most 5, and never duplicates.
5. Apply my saved memories and standing rules.

{home}Write the plan in this shape, leaving out any section that would be empty:
**The week**: one line per day, e.g. "Mon 19: Late 15:00–23:00; dentist 16:00 ⚠️ clash".
**Watch out**: each clash or risk, with a suggested fix.
**Deadlines & admin**: what's due this week.
{home_section}**Free time**: the best windows for rest, family, exercise or study.
**Proposed**: one line per card you created.
Keep it under 300 words."""

SCHEDULED_NOTE = (
    "This turn was started automatically by the LifeOS scheduler, not typed by the speaker. "
    "They will read your reply as a notification and may answer it later."
)


HOME_SECTION = "**Home**: each Homebase item, one line: what it is, the date, and what to do (renew, book, chase or update Homebase).\n"
# The morning brief only raises what is close; the week ahead looks a month out.
DAILY_HOME_DAYS = 7
WEEKLY_HOME_DAYS = 30


def _home(days: int, include_snags: bool) -> dict:
    # Imported here: homebase pulls in the proposal and document tools.
    from .homebase import home_admin_note

    note = home_admin_note(days=days, include_snags=include_snags)
    return {"home": f"{note}\n\n" if note else "", "home_section": HOME_SECTION if note else ""}


def briefing_prompt(briefing_type: str, local_date) -> str:
    if briefing_type == WEEKLY:
        start = local_date + timedelta(days=1)
        return WEEKLY_PLAN_PROMPT.format(start=start, end=start + timedelta(days=6), **_home(WEEKLY_HOME_DAYS, True))
    return DAILY_BRIEF_PROMPT.format(today=local_date, **_home(DAILY_HOME_DAYS, False))


def enqueue_briefing(chat: TelegramChat, user: TelegramUser, briefing_type: str, local_date) -> AgentJob:
    return AgentJob.objects.create(
        channel=AgentThread.Channel.TELEGRAM,
        telegram_chat=chat,
        telegram_user=user,
        briefing_type=briefing_type,
        text=briefing_prompt(briefing_type, local_date),
    )


def send_template_briefing(job: AgentJob, bot) -> None:
    """Fallback when the agent could not write the briefing."""
    # Imported here: telegram_handlers imports the agent package for routing.
    from assistant.services.telegram_handlers import (
        build_planning_message,
        build_today_message,
        planning_keyboard,
        today_keyboard,
    )

    if job.briefing_type == WEEKLY:
        start = timezone.localtime(job.created_at).date() + timedelta(days=1)
        message = build_planning_message(period=WEEKLY, start_date=start, end_date=start + timedelta(days=7))
        markup = planning_keyboard()
    else:
        message, markup = build_today_message(), today_keyboard()
    bot.send_message(job.telegram_chat.chat_id, message, reply_markup=markup)
