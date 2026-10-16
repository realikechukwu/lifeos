import json
from datetime import time

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models

User = get_user_model()


def normalise_email(value: str) -> str:
    """Lowercase + strip an email address for consistent comparison/storage."""
    if not value:
        return ""
    return value.strip().lower()


class AssignedTo(models.TextChoices):
    """Who a Task (or a note's related person) belongs to. Deliberately a
    closed set resolved only from authorised household members/env vars —
    never an arbitrary name or email pulled from email content."""

    IKE = "ike", "Ike"
    WIFE = "wife", "Wife"
    BOTH = "both", "Both"
    UNASSIGNED = "unassigned", "Unassigned"


class RecipientTarget(models.TextChoices):
    """Who a Reminder email goes to. Resolved only via AUTHORISED_EMAIL_IKE /
    AUTHORISED_EMAIL_WIFE — never an address extracted from email text."""

    IKE = "ike", "Ike"
    WIFE = "wife", "Wife"
    BOTH = "both", "Both"


class HouseholdMember(models.Model):
    class Role(models.TextChoices):
        IKE = "ike", "Ike"
        WIFE = "wife", "Wife"
        CHILD = "child", "Child"
        OTHER = "other", "Other"

    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OTHER)
    authorised = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        self.email = normalise_email(self.email)
        super().save(*args, **kwargs)


class IncomingEmail(models.Model):
    class Source(models.TextChoices):
        EMAIL = "email", "Email"
        TELEGRAM = "telegram", "Telegram"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        PENDING_REVIEW = "pending_review", "Pending review"
        FAILED = "failed", "Failed"
        UNAUTHORISED = "unauthorised", "Unauthorised"

    gmail_message_id = models.CharField(max_length=64, unique=True)
    gmail_thread_id = models.CharField(max_length=64, blank=True, default="")
    rfc_message_id = models.CharField(max_length=998, blank=True, default="")
    auto_submitted = models.CharField(max_length=100, blank=True, default="")
    precedence = models.CharField(max_length=100, blank=True, default="")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.EMAIL)

    outer_sender = models.EmailField(help_text="Address that actually sent/forwarded the email to us.")
    recipients = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")

    body_text = models.TextField(blank=True, default="")
    body_html_sanitised = models.TextField(blank=True, default="")

    received_at = models.DateTimeField(null=True, blank=True)

    is_forwarded = models.BooleanField(default=False)
    original_forwarded_sender = models.EmailField(blank=True, default="")
    original_forwarded_date = models.DateTimeField(null=True, blank=True)

    has_attachments = models.BooleanField(default=False)
    attachment_metadata = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    processing_attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.subject or '(no subject)'} from {self.outer_sender}"


class ParsedAction(models.Model):
    class ActionType(models.TextChoices):
        CREATE_CALENDAR_EVENT = "create_calendar_event", "Create calendar event"
        CREATE_TASK = "create_task", "Create task"
        CREATE_NOTE = "create_note", "Create note"
        CREATE_EMAIL_REMINDER = "create_email_reminder", "Create email reminder"
        MARK_TASK_COMPLETE = "mark_task_complete", "Mark task complete"
        REQUIRES_REVIEW = "requires_review", "Requires review"
        UNSUPPORTED = "unsupported", "Unsupported"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        PENDING_REVIEW = "pending_review", "Pending review"
        APPROVED = "approved", "Approved"
        EXECUTED = "executed", "Executed"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    incoming_email = models.ForeignKey(
        IncomingEmail, on_delete=models.CASCADE, related_name="parsed_actions"
    )

    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    extracted_data = models.JSONField(default=dict, blank=True)

    title = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")

    appointment_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)

    location = models.CharField(max_length=500, blank=True, default="")
    meeting_url = models.URLField(max_length=1000, blank=True, default="")
    organiser = models.CharField(max_length=255, blank=True, default="")
    booking_reference = models.CharField(max_length=255, blank=True, default="")

    # Recurring calendar events: raw structured fields from extraction.
    # Python (assistant.services.extractor.build_recurrence) assembles the
    # actual RRULE from these — never trusted from the model directly.
    recurrence_frequency = models.CharField(max_length=10, blank=True, default="")
    recurrence_interval = models.PositiveIntegerField(null=True, blank=True)
    recurrence_days_of_week = models.JSONField(default=list, blank=True)
    recurrence_until = models.DateField(null=True, blank=True)
    recurrence_count = models.PositiveIntegerField(null=True, blank=True)

    # Phase 2: tasks
    due_date = models.DateField(null=True, blank=True)
    due_time = models.TimeField(null=True, blank=True)
    assigned_to = models.CharField(max_length=20, choices=AssignedTo.choices, blank=True, default="")
    task_search_text = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Free text used to look up an existing task for mark_task_complete.",
    )

    # Phase 2: notes
    note_category = models.CharField(max_length=100, blank=True, default="")

    # Phase 2: reminders (standalone, or linked to a create_calendar_event action)
    reminder_date = models.DateField(null=True, blank=True)
    reminder_time = models.TimeField(null=True, blank=True)
    reminder_recipient = models.CharField(max_length=10, choices=RecipientTarget.choices, blank=True, default="")
    reminder_lead_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Set only when the user said something like 'the day before the event'. "
        "The actual reminder date is always computed in Python from the validated event date, never trusted from the model.",
    )

    related_household_member = models.ForeignKey(
        HouseholdMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="parsed_actions"
    )

    # Set only when the email explicitly asked to notify/email/cc both
    # household members about the confirmation reply. Resolved to real
    # addresses solely via AUTHORISED_EMAIL_IKE / AUTHORISED_EMAIL_WIFE in
    # assistant/services/email_sender.py — never from anything else in the
    # email. The reply's `to_addr` is always the authorised outer sender
    # regardless of this flag; this only ever adds a CC.
    notify_both = models.BooleanField(default=False)

    confidence = models.FloatField(default=0.0)
    missing_fields = models.JSONField(default=list, blank=True)
    ambiguity_notes = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")

    duplicate_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_type_display()}: {self.title or '(untitled)'}"


class CalendarEventRecord(models.Model):
    parsed_action = models.ForeignKey(
        ParsedAction, on_delete=models.CASCADE, related_name="calendar_events"
    )
    incoming_email = models.ForeignKey(
        IncomingEmail, on_delete=models.CASCADE, related_name="calendar_events"
    )

    google_event_id = models.CharField(max_length=255)
    calendar_id = models.CharField(max_length=255)

    title = models.CharField(max_length=255)
    appointment_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/London")
    all_day = models.BooleanField(default=False)

    location = models.CharField(max_length=500, blank=True, default="")
    booking_reference = models.CharField(max_length=255, blank=True, default="")

    # What was actually sent to Google in event_body["recurrence"], and a
    # human-readable version of the same rule for the confirmation email
    # and dashboard. Empty for a one-off event.
    recurrence_rule = models.TextField(blank=True, default="")
    recurrence_description = models.CharField(max_length=255, blank=True, default="")

    duplicate_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} on {self.appointment_date}"


class PatchworkShift(models.Model):
    """A work shift imported from the private Patchwork iCalendar feed.

    Patchwork remains authoritative: LifeOS mirrors the source label and time
    into the Google Calendar event it owns. Redundant work intervals remain
    recorded but are suppressed from the mirror so the decision is reversible
    on the next sync.
    """

    source_uid = models.CharField(max_length=512, unique=True)
    google_event_id = models.CharField(max_length=255, blank=True, default="")
    calendar_id = models.CharField(max_length=255, blank=True, default="")

    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/London")
    all_day = models.BooleanField(default=False)
    source_label = models.CharField(max_length=255, blank=True, default="")
    source_status = models.CharField(max_length=50, blank=True, default="")
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    active = models.BooleanField(default=True)
    suppressed = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "created_at"]
        indexes = [models.Index(fields=["active", "starts_at"])]

    def __str__(self):
        return f"{self.display_title} — {self.starts_at.isoformat()}"

    @property
    def display_title(self) -> str:
        return f"Ike — {self.source_label}" if self.source_label else "Ike work shift"


class Task(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    assigned_to = models.CharField(max_length=20, choices=AssignedTo.choices, default=AssignedTo.UNASSIGNED)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)

    due_date = models.DateField(null=True, blank=True)
    due_time = models.TimeField(null=True, blank=True)

    source_email = models.ForeignKey(
        IncomingEmail, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    source_parsed_action = models.ForeignKey(
        ParsedAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    created_by = models.ForeignKey(
        HouseholdMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_tasks"
    )

    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"


class Note(models.Model):
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    category = models.CharField(max_length=100, blank=True, default="")

    related_household_member = models.ForeignKey(
        HouseholdMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="notes"
    )
    source_email = models.ForeignKey(
        IncomingEmail, on_delete=models.SET_NULL, null=True, blank=True, related_name="notes"
    )
    source_parsed_action = models.ForeignKey(
        ParsedAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="notes"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or (self.body[:50] if self.body else "(empty note)")


class Reminder(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    recipient = models.CharField(max_length=10, choices=RecipientTarget.choices)

    reminder_date = models.DateField()
    reminder_time = models.TimeField()
    timezone = models.CharField(max_length=64, default="Europe/London")

    related_task = models.ForeignKey(
        Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )
    related_calendar_event = models.ForeignKey(
        CalendarEventRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )

    source_email = models.ForeignKey(
        IncomingEmail, on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )
    source_parsed_action = models.ForeignKey(
        ParsedAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Generated only when send_due_reminders claims the reminder (pending ->
    # processing), not at creation time. Nullable so many pending rows can
    # coexist without violating uniqueness.
    send_key = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    rfc_message_id = models.CharField(max_length=255, blank=True, default="")

    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    delivery_attempts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["reminder_date", "reminder_time"]

    def __str__(self):
        return f'"{self.title}" -> {self.recipient} on {self.reminder_date} {self.reminder_time}'


class AuditLog(models.Model):
    source_email = models.ForeignKey(
        IncomingEmail, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True, default="")
    object_id = models.CharField(max_length=100, blank=True, default="")
    success = models.BooleanField(default=True)
    details = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} ({'ok' if self.success else 'failed'})"


class TelegramUser(models.Model):
    """An allow-listed household member as Telegram identifies them.

    Telegram usernames and display names are descriptive only. Authorisation
    always comes from the immutable numeric user id configured by the owner.
    """

    user_id = models.BigIntegerField(unique=True)
    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    username = models.CharField(max_length=64, blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    authorised = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "user_id"]

    def __str__(self):
        return self.display_name or f"Telegram user {self.user_id}"


class TelegramPreference(models.Model):
    """Private Telegram briefing preferences for an authorised household user."""

    user = models.OneToOneField(TelegramUser, on_delete=models.CASCADE, related_name="preference")
    briefing_enabled = models.BooleanField(default=True)
    briefing_time = models.TimeField(default=time(7, 30))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        state = "on" if self.briefing_enabled else "off"
        return f"Telegram briefing for {self.user} ({state} at {self.briefing_time:%H:%M})"


class TelegramBriefingDelivery(models.Model):
    """Idempotency record for one user's private Telegram briefing."""

    class BriefingType(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    user = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="briefing_deliveries")
    briefing_type = models.CharField(
        max_length=10,
        choices=BriefingType.choices,
        default=BriefingType.DAILY,
    )
    briefing_date = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-briefing_date", "-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "briefing_type", "briefing_date"],
                name="telegram_one_briefing_per_type_day",
            ),
        ]

    def __str__(self):
        return f"Telegram {self.briefing_type} briefing for {self.user} on {self.briefing_date}"


class WeeklyEmailBriefing(models.Model):
    """Idempotency record for the Sunday week-ahead email, plus the household
    section written once so both adults receive exactly the same text."""

    week_start = models.DateField(unique=True)
    shared_text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-week_start"]

    def __str__(self):
        return f"Weekly email for the week of {self.week_start}"


class TelegramChat(models.Model):
    class ChatType(models.TextChoices):
        PRIVATE = "private", "Private"
        GROUP = "group", "Group"
        SUPERGROUP = "supergroup", "Supergroup"

    chat_id = models.BigIntegerField(unique=True)
    chat_type = models.CharField(max_length=20, choices=ChatType.choices)
    title = models.CharField(max_length=255, blank=True, default="")
    authorised = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chat_type", "chat_id"]

    def __str__(self):
        return self.title or f"Telegram chat {self.chat_id}"


class TelegramUpdate(models.Model):
    """Idempotency record for a Telegram webhook delivery.

    We deliberately do not persist Telegram's full raw JSON payload here.
    Conversation text is retained in the source record for the same audit and
    review needs as email, while the temporary model transcript is cleared
    after a successfully executed request.
    """

    update_id = models.BigIntegerField(unique=True)
    update_type = models.CharField(max_length=30, blank=True, default="")
    chat = models.ForeignKey(
        TelegramChat, on_delete=models.SET_NULL, null=True, blank=True, related_name="updates"
    )
    user = models.ForeignKey(
        TelegramUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="updates"
    )
    processed = models.BooleanField(default=False)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-update_id"]

    def __str__(self):
        return f"Telegram update {self.update_id}"


class TelegramConversation(models.Model):
    class Status(models.TextChoices):
        AWAITING_CLARIFICATION = "awaiting_clarification", "Awaiting clarification"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting confirmation"
        EXECUTING = "executing", "Executing"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    chat = models.ForeignKey(TelegramChat, on_delete=models.CASCADE, related_name="conversations")
    requested_by = models.ForeignKey(
        TelegramUser, on_delete=models.PROTECT, related_name="conversations"
    )
    incoming_email = models.OneToOneField(
        IncomingEmail, on_delete=models.CASCADE, related_name="telegram_conversation"
    )
    parsed_action = models.OneToOneField(
        ParsedAction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_conversation",
    )
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.AWAITING_CLARIFICATION
    )
    transcript = models.JSONField(default=list, blank=True)
    draft_extraction = models.JSONField(default=dict, blank=True)
    pending_extractions = models.JSONField(default=list, blank=True)
    batch_size = models.PositiveSmallIntegerField(default=0)
    batch_index = models.PositiveSmallIntegerField(default=0)
    clarification_count = models.PositiveSmallIntegerField(default=0)
    last_bot_message_id = models.BigIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["chat", "status"])]

    def __str__(self):
        return f"Telegram conversation {self.id} ({self.get_status_display()})"


class TelegramInboxAction(models.Model):
    """A short, explicit state machine for inbox edits made inside Telegram.

    It is deliberately separate from ``TelegramConversation``: inbox actions
    operate on already-existing LifeOS records and never enter the email/LLM
    extraction pipeline.
    """

    class Action(models.TextChoices):
        EDIT_NOTE = "edit_note", "Edit note"
        EDIT_TASK = "edit_task", "Edit task"
        RESCHEDULE_EVENT = "reschedule_event", "Reschedule event"
        CANCEL_EVENT = "cancel_event", "Cancel event"
        SET_BRIEFING_TIME = "set_briefing_time", "Set briefing time"

    class Status(models.TextChoices):
        AWAITING_INPUT = "awaiting_input", "Awaiting input"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting confirmation"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    chat = models.ForeignKey(TelegramChat, on_delete=models.CASCADE, related_name="inbox_actions")
    requested_by = models.ForeignKey(TelegramUser, on_delete=models.PROTECT, related_name="inbox_actions")
    action = models.CharField(max_length=30, choices=Action.choices)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.AWAITING_INPUT)
    proposed_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["chat", "requested_by", "status"])]

    def __str__(self):
        return f"Telegram inbox action {self.id} ({self.get_action_display()})"


class GoogleCredential(models.Model):
    """Stores OAuth credentials for the assistant Gmail/Calendar account.

    The refresh token and other sensitive fields are stored only as a
    Fernet-encrypted blob, keyed by settings.APP_ENCRYPTION_KEY. Never
    expose `encrypted_data` in the admin.
    """

    account_email = models.EmailField(unique=True)
    encrypted_data = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Google credential"
        verbose_name_plural = "Google credentials"

    def __str__(self):
        return f"Google credential for {self.account_email}"

    @staticmethod
    def _fernet() -> Fernet:
        key = settings.APP_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "APP_ENCRYPTION_KEY is not set. Generate one with "
                "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
            )
        return Fernet(key.encode() if isinstance(key, str) else key)

    def set_credentials(self, data: dict) -> None:
        """Encrypt and store a dict of credential data (token, refresh_token, etc.)."""
        payload = json.dumps(data).encode("utf-8")
        self.encrypted_data = self._fernet().encrypt(payload).decode("utf-8")

    def get_credentials(self) -> dict:
        """Decrypt and return the stored credential dict, or {} if unset/invalid."""
        if not self.encrypted_data:
            return {}
        try:
            raw = self._fernet().decrypt(self.encrypted_data.encode("utf-8"))
        except InvalidToken:
            return {}
        return json.loads(raw.decode("utf-8"))


class AgentThread(models.Model):
    """One running conversation between a household member and the Claude agent.

    History is replayed to the model verbatim on every turn, so messages are
    append-only. A thread idle for AGENT_THREAD_IDLE_HOURS (or reset with
    /new) is closed and the next message starts a new one.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        EMAIL = "email", "Email"

    channel = models.CharField(max_length=20, choices=Channel.choices)
    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    email_thread_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # The system prompt + tools + model fingerprint the history was written
    # under; a Telegram thread with an older version is closed, not replayed.
    prompt_version = models.CharField(max_length=16, blank=True, default="")
    telegram_chat = models.ForeignKey(
        TelegramChat, on_delete=models.CASCADE, null=True, blank=True, related_name="agent_threads"
    )
    telegram_user = models.ForeignKey(
        TelegramUser, on_delete=models.CASCADE, null=True, blank=True, related_name="agent_threads"
    )
    last_activity_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-last_activity_at"]
        indexes = [models.Index(fields=["channel", "telegram_chat", "telegram_user", "closed_at"])]

    def __str__(self):
        return f"Agent thread {self.id} ({self.channel}, {self.role})"


class AgentMessage(models.Model):
    """One API-shaped message in an agent thread, stored exactly as sent/received."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        # Mid-conversation operator context (current time, who is speaking).
        SYSTEM = "system", "System"

    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.JSONField(default=list)
    # Usage is recorded on assistant messages for cost and cache-hit tracking.
    model = models.CharField(max_length=64, blank=True, default="")
    stop_reason = models.CharField(max_length=30, blank=True, default="")
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_read_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_creation_tokens = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Agent message {self.id} ({self.role})"


class AgentJob(models.Model):
    """A queued request for the agent worker. The webhook only enqueues; all
    model calls happen in run_agent_worker so Telegram never waits on them."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    channel = models.CharField(max_length=20, choices=AgentThread.Channel.choices)
    incoming_email = models.OneToOneField(
        IncomingEmail, on_delete=models.CASCADE, null=True, blank=True, related_name="agent_job"
    )
    input_blocks = models.JSONField(default=list, blank=True)
    reply_text = models.TextField(blank=True, default="")
    reply_metadata = models.JSONField(default=dict, blank=True)
    sent_reply_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    telegram_chat = models.ForeignKey(
        TelegramChat, on_delete=models.CASCADE, null=True, blank=True, related_name="agent_jobs"
    )
    telegram_user = models.ForeignKey(
        TelegramUser, on_delete=models.CASCADE, null=True, blank=True, related_name="agent_jobs"
    )
    telegram_update_id = models.BigIntegerField(null=True, blank=True, unique=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    # Set for scheduler-started briefings (daily / weekly); blank for messages.
    briefing_type = models.CharField(max_length=10, blank=True, default="")
    # Cleared once the job finishes: by then the text lives in the thread history.
    text = models.TextField(blank=True, default="")
    thread = models.ForeignKey(
        AgentThread, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["status", "id"])]

    def __str__(self):
        return f"Agent job {self.id} ({self.status})"


class AgentMemory(models.Model):
    """A durable fact or preference the agent saved for one household member."""

    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    text = models.CharField(max_length=500)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "Agent memories"

    def __str__(self):
        return f"{self.role}: {self.text[:60]}"


class AgentProposal(models.Model):
    class Kind(models.TextChoices):
        CREATE_EVENT = "create_event", "Create event"
        CREATE_TASK = "create_task", "Create task"
        CREATE_NOTE = "create_note", "Create note"
        CREATE_REMINDER = "create_reminder", "Create reminder"
        COMPLETE_TASK = "complete_task", "Complete task"
        UPDATE_TASK = "update_task", "Update task"
        RESCHEDULE_EVENT = "reschedule_event", "Reschedule event"
        CANCEL_EVENT = "cancel_event", "Cancel event"
        CANCEL_REMINDER = "cancel_reminder", "Cancel reminder"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        SUPERSEDED = "superseded", "Superseded"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"

    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="proposals")
    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    telegram_chat = models.ForeignKey(TelegramChat, on_delete=models.CASCADE, null=True, blank=True)
    telegram_user = models.ForeignKey(TelegramUser, on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=dict, blank=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    replaces = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True)
    confirmation_email = models.ForeignKey(
        IncomingEmail, on_delete=models.SET_NULL, null=True, blank=True, related_name="offered_proposals"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]


class AgentOutcome(models.Model):
    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="outcomes")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]


class AgentUpload(models.Model):
    """An extracted source kept with the job, never supplied back by the model."""

    job = models.ForeignKey(AgentJob, on_delete=models.CASCADE, related_name="uploads")
    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class ProfileDocument(models.Model):
    class Kind(models.TextChoices):
        CV = "cv", "CV"
        COVER_LETTER = "cover_letter", "Cover letter"
        EXPERIENCE = "experience", "Experience"
        OTHER = "other", "Other"

    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    title = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.TextField()
    source_filename = models.CharField(max_length=255)
    source_upload = models.ForeignKey(AgentUpload, on_delete=models.SET_NULL, null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]


class GeneratedDocument(models.Model):
    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="generated_documents")
    job = models.ForeignKey(AgentJob, on_delete=models.SET_NULL, null=True, blank=True)
    title = models.CharField(max_length=255)
    filename = models.CharField(max_length=255)
    content = models.TextField()
    # Private DB storage avoids a publicly served media directory and server setup.
    data = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class AgentEmailDelivery(models.Model):
    """A reservation counts toward the daily limit even if delivery is uncertain."""

    role = models.CharField(max_length=20, choices=HouseholdMember.Role.choices)
    day = models.DateField()
    slot = models.PositiveSmallIntegerField()
    message_id = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["role", "day", "slot"], name="agent_email_daily_slot")]


class AgentDocumentDelivery(models.Model):
    job = models.ForeignKey(AgentJob, on_delete=models.CASCADE, related_name="document_deliveries")
    document = models.ForeignKey(GeneratedDocument, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "document"], name="agent_one_document_per_job")]
