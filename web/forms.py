"""Plain Django ModelForms for the family web interface. Validation lives
here / on the models — never duplicated from the email-command pipeline in
`assistant/services/*`. Manual task/note creation through the web does not
call OpenAI or re-run any extraction/gating logic."""

from django import forms

from core.models import HouseholdMember, Note, ParsedAction, Task

DATE_ATTRS = {"type": "date", "class": "form-control"}
TIME_ATTRS = {"type": "time", "class": "form-control"}


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "assigned_to", "status", "priority", "due_date", "due_time"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "due_date": forms.DateInput(attrs=DATE_ATTRS),
            "due_time": forms.TimeInput(attrs=TIME_ATTRS),
        }

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Title is required.")
        return title


class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ["title", "body", "category", "related_household_member"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "body": forms.Textarea(attrs={"class": "form-control", "rows": 6}),
            "category": forms.TextInput(attrs={"class": "form-control"}),
            "related_household_member": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["related_household_member"].queryset = HouseholdMember.objects.filter(active=True)
        self.fields["related_household_member"].required = False

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("title") or "").strip() and not (cleaned.get("body") or "").strip():
            raise forms.ValidationError("A note needs a title or a body.")
        return cleaned


class ParsedActionReviewForm(forms.ModelForm):
    """Editable extracted fields for a pending_review ParsedAction. Approve
    saves this form, then calls the same `admin_approve_and_execute` service
    the Django admin action uses — this form never creates the resulting
    event/task/note/reminder itself."""

    class Meta:
        model = ParsedAction
        fields = [
            "title", "description",
            "appointment_date", "start_time", "end_time", "all_day",
            "location", "meeting_url", "organiser", "booking_reference",
            "due_date", "due_time", "assigned_to", "task_search_text",
            "note_category",
            "reminder_date", "reminder_time", "reminder_recipient", "reminder_lead_days",
            "related_household_member",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "appointment_date": forms.DateInput(attrs=DATE_ATTRS),
            "start_time": forms.TimeInput(attrs=TIME_ATTRS),
            "end_time": forms.TimeInput(attrs=TIME_ATTRS),
            "all_day": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "meeting_url": forms.URLInput(attrs={"class": "form-control"}),
            "organiser": forms.TextInput(attrs={"class": "form-control"}),
            "booking_reference": forms.TextInput(attrs={"class": "form-control"}),
            "due_date": forms.DateInput(attrs=DATE_ATTRS),
            "due_time": forms.TimeInput(attrs=TIME_ATTRS),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "task_search_text": forms.TextInput(attrs={"class": "form-control"}),
            "note_category": forms.TextInput(attrs={"class": "form-control"}),
            "reminder_date": forms.DateInput(attrs=DATE_ATTRS),
            "reminder_time": forms.TimeInput(attrs=TIME_ATTRS),
            "reminder_recipient": forms.Select(attrs={"class": "form-select"}),
            "reminder_lead_days": forms.NumberInput(attrs={"class": "form-control"}),
            "related_household_member": forms.Select(attrs={"class": "form-select"}),
        }


TASK_STATUS_CHOICES = [("", "Any status")] + list(Task.Status.choices)
