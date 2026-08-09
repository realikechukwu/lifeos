from datetime import timedelta

from django.contrib import admin
from django.utils import timezone

from .models import (
    AuditLog,
    CalendarEventRecord,
    GoogleCredential,
    HouseholdMember,
    IncomingEmail,
    Note,
    ParsedAction,
    PatchworkShift,
    Reminder,
    Task,
    TelegramChat,
    TelegramBriefingDelivery,
    TelegramInboxAction,
    TelegramPreference,
    TelegramConversation,
    TelegramUpdate,
    TelegramUser,
)

STALE_PROCESSING_MINUTES = 15


@admin.register(HouseholdMember)
class HouseholdMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "role", "authorised", "active", "updated_at")
    list_filter = ("role", "authorised", "active")
    search_fields = ("name", "email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user_id", "role", "authorised", "updated_at")
    list_filter = ("role", "authorised")
    search_fields = ("display_name", "username", "user_id")
    readonly_fields = ("user_id", "role", "username", "display_name", "created_at", "updated_at")


@admin.register(TelegramPreference)
class TelegramPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "briefing_enabled", "briefing_time", "updated_at")
    list_filter = ("briefing_enabled",)
    search_fields = ("user__display_name", "user__username", "user__user_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TelegramBriefingDelivery)
class TelegramBriefingDeliveryAdmin(admin.ModelAdmin):
    list_display = ("user", "briefing_date", "sent_at")
    list_filter = ("briefing_date",)
    search_fields = ("user__display_name", "user__username", "user__user_id")
    readonly_fields = ("user", "briefing_date", "sent_at")


@admin.register(TelegramChat)
class TelegramChatAdmin(admin.ModelAdmin):
    list_display = ("title", "chat_id", "chat_type", "authorised", "updated_at")
    list_filter = ("chat_type", "authorised")
    search_fields = ("title", "chat_id")
    readonly_fields = ("chat_id", "chat_type", "title", "created_at", "updated_at")


@admin.register(TelegramUpdate)
class TelegramUpdateAdmin(admin.ModelAdmin):
    list_display = ("update_id", "update_type", "chat", "user", "processed", "created_at")
    list_filter = ("update_type", "processed")
    search_fields = ("update_id", "error")
    readonly_fields = [field.name for field in TelegramUpdate._meta.fields]


@admin.register(TelegramConversation)
class TelegramConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "requested_by", "status", "parsed_action", "updated_at")
    list_filter = ("status",)
    search_fields = ("chat__title", "requested_by__display_name", "incoming_email__body_text")
    readonly_fields = [field.name for field in TelegramConversation._meta.fields]


@admin.register(TelegramInboxAction)
class TelegramInboxActionAdmin(admin.ModelAdmin):
    list_display = ("id", "action", "status", "chat", "requested_by", "object_id", "updated_at")
    list_filter = ("action", "status")
    search_fields = ("chat__title", "requested_by__display_name", "object_id")
    readonly_fields = [field.name for field in TelegramInboxAction._meta.fields]


@admin.register(IncomingEmail)
class IncomingEmailAdmin(admin.ModelAdmin):
    list_display = (
        "subject",
        "source",
        "outer_sender",
        "status",
        "is_forwarded",
        "has_attachments",
        "processing_attempts",
        "received_at",
    )
    list_filter = ("source", "status", "is_forwarded", "has_attachments")
    search_fields = ("subject", "outer_sender", "gmail_message_id", "gmail_thread_id", "body_text")
    readonly_fields = (
        "gmail_message_id",
        "gmail_thread_id",
        "outer_sender",
        "recipients",
        "subject",
        "body_text",
        "body_html_sanitised",
        "received_at",
        "is_forwarded",
        "original_forwarded_sender",
        "original_forwarded_date",
        "has_attachments",
        "attachment_metadata",
        "processing_attempts",
        "last_error",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "received_at"


class CalendarEventRecordInline(admin.TabularInline):
    model = CalendarEventRecord
    extra = 0
    readonly_fields = (
        "google_event_id",
        "calendar_id",
        "title",
        "appointment_date",
        "start_time",
        "end_time",
        "all_day",
        "duplicate_key",
    )
    can_delete = False


class TaskInline(admin.TabularInline):
    model = Task
    fk_name = "source_parsed_action"
    extra = 0
    fields = ("title", "assigned_to", "status", "due_date")
    readonly_fields = ("title", "assigned_to", "status", "due_date")
    can_delete = False


class NoteInline(admin.TabularInline):
    model = Note
    fk_name = "source_parsed_action"
    extra = 0
    fields = ("title", "category")
    readonly_fields = ("title", "category")
    can_delete = False


class ReminderInline(admin.TabularInline):
    model = Reminder
    fk_name = "source_parsed_action"
    extra = 0
    fields = ("title", "recipient", "reminder_date", "reminder_time", "status")
    readonly_fields = ("title", "recipient", "reminder_date", "reminder_time", "status")
    can_delete = False


@admin.register(ParsedAction)
class ParsedActionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "action_type",
        "status",
        "appointment_date",
        "due_date",
        "reminder_date",
        "confidence",
        "incoming_email_link",
        "updated_at",
    )
    list_filter = ("action_type", "status", "all_day")
    search_fields = (
        "title", "location", "booking_reference", "task_search_text",
        "incoming_email__subject",
    )
    readonly_fields = (
        "incoming_email",
        "extracted_data",
        "missing_fields",
        "ambiguity_notes",
        "evidence",
        "duplicate_key",
        "reviewed_by",
        "reviewed_at",
        "executed_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "incoming_email",
        "action_type",
        "status",
        "title",
        "description",
        # Calendar
        "appointment_date",
        "start_time",
        "end_time",
        "all_day",
        "location",
        "meeting_url",
        "organiser",
        "booking_reference",
        # Task
        "due_date",
        "due_time",
        "assigned_to",
        "task_search_text",
        # Note
        "note_category",
        # Reminder
        "reminder_date",
        "reminder_time",
        "reminder_recipient",
        "reminder_lead_days",
        "related_household_member",
        "confidence",
        "missing_fields",
        "ambiguity_notes",
        "evidence",
        "extracted_data",
        "duplicate_key",
        "failure_reason",
        "reviewed_by",
        "reviewed_at",
        "executed_at",
        "created_at",
        "updated_at",
    )
    inlines = [CalendarEventRecordInline, TaskInline, NoteInline, ReminderInline]
    actions = ["approve_pending_actions", "reject_selected_actions", "retry_selected_actions"]

    @admin.display(description="Incoming email")
    def incoming_email_link(self, obj):
        return str(obj.incoming_email)

    @admin.action(description="Approve pending parsed actions (create/complete via the normal service)")
    def approve_pending_actions(self, request, queryset):
        # Delegate to the same dispatch used by automatic processing — no
        # duplicated business logic in the admin.
        from assistant.services.router import admin_approve_and_execute

        created, deferred, failed = 0, 0, 0
        for action in queryset:
            action.reviewed_by = request.user
            action.reviewed_at = timezone.now()
            action.save(update_fields=["reviewed_by", "reviewed_at"])
            try:
                outcome, _extra = admin_approve_and_execute(action)
            except Exception as exc:  # noqa: BLE001 - surface to admin, don't crash the request
                action.status = ParsedAction.Status.FAILED
                action.failure_reason = str(exc)
                action.save(update_fields=["status", "failure_reason"])
                failed += 1
                continue
            action.refresh_from_db()
            if outcome in ("pending_review", "task_ambiguous"):
                deferred += 1
            else:
                created += 1
        self.message_user(
            request, f"Executed {created} action(s). {deferred} deferred/still ambiguous. {failed} failed."
        )

    @admin.action(description="Reject selected actions")
    def reject_selected_actions(self, request, queryset):
        updated = queryset.exclude(status=ParsedAction.Status.EXECUTED).update(
            status=ParsedAction.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"Rejected {updated} action(s).")

    @admin.action(description="Retry selected actions (re-run processing)")
    def retry_selected_actions(self, request, queryset):
        from assistant.management.commands.poll_gmail import process_incoming_email

        retried, skipped = 0, 0
        for action in queryset:
            if action.status == ParsedAction.Status.EXECUTED:
                skipped += 1
                continue
            email = action.incoming_email
            # Telegram conversations are retried from their own conversation
            # flow; never hand a Telegram source to the Gmail poller.
            if email.source != IncomingEmail.Source.EMAIL:
                skipped += 1
                continue
            email.processing_attempts += 1
            email.status = IncomingEmail.Status.PROCESSING
            email.save(update_fields=["processing_attempts", "status"])
            process_incoming_email(email)
            retried += 1
        self.message_user(request, f"Retried {retried} action(s). Skipped {skipped} already-executed action(s).")


@admin.register(CalendarEventRecord)
class CalendarEventRecordAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "appointment_date",
        "start_time",
        "all_day",
        "calendar_id",
        "parsed_action_link",
        "created_at",
    )
    list_filter = ("all_day", "calendar_id")
    search_fields = ("title", "google_event_id", "booking_reference", "duplicate_key")
    readonly_fields = [f.name for f in CalendarEventRecord._meta.fields]

    @admin.display(description="Parsed action")
    def parsed_action_link(self, obj):
        return str(obj.parsed_action)


@admin.register(PatchworkShift)
class PatchworkShiftAdmin(admin.ModelAdmin):
    list_display = (
        "source_label", "starts_at", "ends_at", "all_day", "active", "suppressed", "source_status", "updated_at",
    )
    list_filter = ("active", "suppressed", "all_day", "source_status")
    search_fields = ("source_label", "source_uid", "google_event_id", "calendar_id")
    readonly_fields = [f.name for f in PatchworkShift._meta.fields]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "assigned_to",
        "status",
        "priority",
        "due_date",
        "due_time",
        "created_by_link",
        "updated_at",
    )
    list_filter = ("status", "priority", "assigned_to")
    search_fields = ("title", "description")
    date_hierarchy = "due_date"
    readonly_fields = (
        "source_email",
        "source_parsed_action",
        "created_by",
        "completed_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "title",
        "description",
        "assigned_to",
        "status",
        "priority",
        "due_date",
        "due_time",
        "source_email",
        "source_parsed_action",
        "created_by",
        "completed_at",
        "created_at",
        "updated_at",
    )
    actions = ["mark_selected_tasks_complete"]

    @admin.display(description="Created by")
    def created_by_link(self, obj):
        return str(obj.created_by) if obj.created_by else "—"

    @admin.action(description="Mark selected tasks complete")
    def mark_selected_tasks_complete(self, request, queryset):
        from assistant.services.tasks import complete_task

        completed, skipped = 0, 0
        for task in queryset:
            if task.status == Task.Status.COMPLETED:
                skipped += 1
                continue
            complete_task(task)
            completed += 1
        self.message_user(request, f"Marked {completed} task(s) complete. Skipped {skipped} already complete.")


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "related_household_member", "source_email_link", "created_at")
    list_filter = ("category",)
    search_fields = ("title", "body")
    date_hierarchy = "created_at"
    readonly_fields = ("source_email", "source_parsed_action", "created_at", "updated_at")
    fields = (
        "title",
        "body",
        "category",
        "related_household_member",
        "source_email",
        "source_parsed_action",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Source email")
    def source_email_link(self, obj):
        return str(obj.source_email) if obj.source_email else "—"


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "recipient",
        "reminder_date",
        "reminder_time",
        "status",
        "is_stale_processing",
        "delivery_attempts",
        "claimed_at",
        "sent_at",
    )
    list_filter = ("status", "recipient")
    search_fields = ("title", "message", "send_key", "rfc_message_id")
    date_hierarchy = "reminder_date"
    readonly_fields = (
        "source_email",
        "source_parsed_action",
        "related_task",
        "related_calendar_event",
        "send_key",
        "rfc_message_id",
        "claimed_at",
        "sent_at",
        "last_error",
        "delivery_attempts",
        "created_at",
        "updated_at",
    )
    fields = (
        "title",
        "message",
        "recipient",
        "reminder_date",
        "reminder_time",
        "timezone",
        "status",
        "related_task",
        "related_calendar_event",
        "source_email",
        "source_parsed_action",
        "send_key",
        "rfc_message_id",
        "claimed_at",
        "sent_at",
        "last_error",
        "delivery_attempts",
        "created_at",
        "updated_at",
    )
    actions = [
        "cancel_selected_reminders",
        "reset_stale_processing_reminders",
        "retry_selected_failed_reminders",
    ]

    @admin.display(description="Stale?", boolean=True)
    def is_stale_processing(self, obj):
        if obj.status != Reminder.Status.PROCESSING or not obj.claimed_at:
            return False
        return (timezone.now() - obj.claimed_at).total_seconds() > STALE_PROCESSING_MINUTES * 60

    @admin.action(description="Cancel selected reminders")
    def cancel_selected_reminders(self, request, queryset):
        updated = queryset.exclude(status__in=[Reminder.Status.SENT, Reminder.Status.CANCELLED]).update(
            status=Reminder.Status.CANCELLED
        )
        for reminder_id in queryset.values_list("id", flat=True):
            AuditLog.objects.create(
                action="reminder_cancelled", object_type="Reminder", object_id=str(reminder_id), success=True,
            )
        self.message_user(request, f"Cancelled {updated} reminder(s).")

    @admin.action(description="Reset stale processing reminders to pending (manual retry)")
    def reset_stale_processing_reminders(self, request, queryset):
        cutoff = timezone.now() - timedelta(minutes=STALE_PROCESSING_MINUTES)
        stale = queryset.filter(status=Reminder.Status.PROCESSING, claimed_at__lt=cutoff)
        reset_count = 0
        for reminder in stale:
            reminder.status = Reminder.Status.PENDING
            reminder.claimed_at = None
            reminder.send_key = None
            reminder.rfc_message_id = ""
            reminder.save(update_fields=["status", "claimed_at", "send_key", "rfc_message_id", "updated_at"])
            AuditLog.objects.create(
                action="reminder_reset_stale", object_type="Reminder", object_id=str(reminder.id), success=True,
            )
            reset_count += 1
        self.message_user(request, f"Reset {reset_count} stale processing reminder(s) to pending.")

    @admin.action(description="Retry selected failed reminders")
    def retry_selected_failed_reminders(self, request, queryset):
        failed = queryset.filter(status=Reminder.Status.FAILED)
        retried = 0
        for reminder in failed:
            reminder.status = Reminder.Status.PENDING
            reminder.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                action="reminder_retry_requested", object_type="Reminder", object_id=str(reminder.id), success=True,
            )
            retried += 1
        self.message_user(request, f"Reset {retried} failed reminder(s) to pending for retry.")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "object_type", "object_id", "success", "created_at")
    list_filter = ("success", "action", "object_type")
    search_fields = ("action", "object_type", "object_id", "error_message")
    readonly_fields = [f.name for f in AuditLog._meta.fields]


@admin.register(GoogleCredential)
class GoogleCredentialAdmin(admin.ModelAdmin):
    # Deliberately never expose encrypted_data (or any decrypted value) here.
    list_display = ("account_email", "created_at", "updated_at")
    readonly_fields = ("account_email", "created_at", "updated_at")
    fields = ("account_email", "created_at", "updated_at")

    def has_add_permission(self, request):
        # Credentials are created only via `python manage.py google_auth`.
        return False
