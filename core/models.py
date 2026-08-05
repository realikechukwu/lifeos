import json

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
    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        PENDING_REVIEW = "pending_review", "Pending review"
        FAILED = "failed", "Failed"
        UNAUTHORISED = "unauthorised", "Unauthorised"

    gmail_message_id = models.CharField(max_length=64, unique=True)
    gmail_thread_id = models.CharField(max_length=64, blank=True, default="")

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
    appointment_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)

    location = models.CharField(max_length=500, blank=True, default="")
    meeting_url = models.URLField(max_length=1000, blank=True, default="")
    organiser = models.CharField(max_length=255, blank=True, default="")
    booking_reference = models.CharField(max_length=255, blank=True, default="")

    related_household_member = models.ForeignKey(
        HouseholdMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="parsed_actions"
    )

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

    duplicate_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} on {self.appointment_date}"


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
