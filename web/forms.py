"""Plain Django ModelForms for the family web interface. Validation lives
here / on the models — never duplicated from the email-command pipeline in
`assistant/services/*`. Manual task/note creation through the web does not
call OpenAI or re-run any extraction/gating logic."""

from django import forms

from core.models import AssignedTo, HouseholdMember, Note, ParsedAction, Task

DATE_ATTRS = {"type": "date", "class": "input"}
TIME_ATTRS = {"type": "time", "class": "input"}


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "assigned_to", "status", "priority", "due_date", "due_time"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input title-input", "placeholder": "What needs doing?"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 3, "placeholder": "Notes"}),
            "assigned_to": forms.Select(attrs={"class": "input"}),
            "status": forms.Select(attrs={"class": "input"}),
            "priority": forms.Select(attrs={"class": "input"}),
            "due_date": forms.DateInput(attrs=DATE_ATTRS, format="%Y-%m-%d"),
            "due_time": forms.TimeInput(attrs=TIME_ATTRS, format="%H:%M"),
        }
        labels = {"description": "Notes", "assigned_to": "Who", "due_date": "Due date", "due_time": "Time"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A new task always starts open; status only matters once it exists.
        if not self.instance.pk:
            del self.fields["status"]

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Title is required.")
        return title


class QuickTaskForm(forms.ModelForm):
    """The one-line "add a task" box: a title, and optionally a due date and
    who it's for. Everything else takes the model defaults."""

    class Meta:
        model = Task
        fields = ["title", "due_date", "assigned_to"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].required = False

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Title is required.")
        return title

    def clean_assigned_to(self):
        return self.cleaned_data.get("assigned_to") or AssignedTo.UNASSIGNED


class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ["title", "body", "category", "related_household_member"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input title-input", "placeholder": "Title"}),
            "body": forms.Textarea(attrs={"class": "input", "rows": 10, "placeholder": "Write anything…"}),
            "category": forms.TextInput(attrs={"class": "input", "placeholder": "e.g. Lists, Home, Health", "list": "note-categories"}),
            "related_household_member": forms.Select(attrs={"class": "input"}),
        }
        labels = {"related_household_member": "About"}

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
            "calendar_category",
            "due_date", "due_time", "assigned_to", "task_search_text",
            "note_category",
            "reminder_date", "reminder_time", "reminder_recipient", "reminder_lead_days",
            "related_household_member",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 3}),
            "appointment_date": forms.DateInput(attrs=DATE_ATTRS),
            "start_time": forms.TimeInput(attrs=TIME_ATTRS),
            "end_time": forms.TimeInput(attrs=TIME_ATTRS),
            "all_day": forms.CheckboxInput(attrs={"class": "check"}),
            "location": forms.TextInput(attrs={"class": "input"}),
            "meeting_url": forms.URLInput(attrs={"class": "input"}),
            "organiser": forms.TextInput(attrs={"class": "input"}),
            "booking_reference": forms.TextInput(attrs={"class": "input"}),
            "calendar_category": forms.Select(attrs={"class": "input"}),
            "due_date": forms.DateInput(attrs=DATE_ATTRS),
            "due_time": forms.TimeInput(attrs=TIME_ATTRS),
            "assigned_to": forms.Select(attrs={"class": "input"}),
            "task_search_text": forms.TextInput(attrs={"class": "input"}),
            "note_category": forms.TextInput(attrs={"class": "input"}),
            "reminder_date": forms.DateInput(attrs=DATE_ATTRS),
            "reminder_time": forms.TimeInput(attrs=TIME_ATTRS),
            "reminder_recipient": forms.Select(attrs={"class": "input"}),
            "reminder_lead_days": forms.NumberInput(attrs={"class": "input"}),
            "related_household_member": forms.Select(attrs={"class": "input"}),
        }


TASK_STATUS_CHOICES = [("", "Any status")] + list(Task.Status.choices)
