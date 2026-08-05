from django.contrib import admin
from django.utils import timezone

from .models import (
    AuditLog,
    CalendarEventRecord,
    GoogleCredential,
    HouseholdMember,
    IncomingEmail,
    ParsedAction,
)


@admin.register(HouseholdMember)
class HouseholdMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "role", "authorised", "active", "updated_at")
    list_filter = ("role", "authorised", "active")
    search_fields = ("name", "email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(IncomingEmail)
class IncomingEmailAdmin(admin.ModelAdmin):
    list_display = (
        "subject",
        "outer_sender",
        "status",
        "is_forwarded",
        "has_attachments",
        "processing_attempts",
        "received_at",
    )
    list_filter = ("status", "is_forwarded", "has_attachments")
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


@admin.register(ParsedAction)
class ParsedActionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "action_type",
        "status",
        "appointment_date",
        "start_time",
        "confidence",
        "incoming_email_link",
        "updated_at",
    )
    list_filter = ("action_type", "status", "all_day")
    search_fields = ("title", "location", "booking_reference", "incoming_email__subject")
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
        "appointment_date",
        "start_time",
        "end_time",
        "all_day",
        "location",
        "meeting_url",
        "organiser",
        "booking_reference",
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
    inlines = [CalendarEventRecordInline]
    actions = ["approve_and_create_event", "reject_selected_actions", "retry_selected_actions"]

    @admin.display(description="Incoming email")
    def incoming_email_link(self, obj):
        return str(obj.incoming_email)

    @admin.action(description="Approve and create calendar event")
    def approve_and_create_event(self, request, queryset):
        # Delegate to the same validated service used by automatic processing.
        from assistant.services.google_calendar import create_event_for_parsed_action

        created, deferred, failed = 0, 0, 0
        for action in queryset:
            action.reviewed_by = request.user
            action.reviewed_at = timezone.now()
            action.save(update_fields=["reviewed_by", "reviewed_at"])
            try:
                record = create_event_for_parsed_action(action)
            except Exception as exc:  # noqa: BLE001 - surface to admin, don't crash the request
                action.status = ParsedAction.Status.FAILED
                action.failure_reason = str(exc)
                action.save(update_fields=["status", "failure_reason"])
                failed += 1
                continue
            action.refresh_from_db()
            if record is not None and action.status == ParsedAction.Status.EXECUTED:
                created += 1
            else:
                deferred += 1  # exact duplicate or possible reschedule; see status/failure_reason
        self.message_user(
            request, f"Created {created} event(s). {deferred} deferred (duplicate/reschedule). {failed} failed."
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
