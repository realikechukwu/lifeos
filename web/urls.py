from django.urls import path

from . import ask_views, views

app_name = "web"

urlpatterns = [
    path("manifest.webmanifest", views.web_manifest, name="web_manifest"),
    path("service-worker.js", views.service_worker, name="service_worker"),

    path("", views.upcoming, name="upcoming"),

    path("ask/", ask_views.ask_page, name="ask"),
    path("ask/send/", ask_views.ask_send, name="ask_send"),
    path("ask/updates/", ask_views.ask_updates, name="ask_updates"),
    path("ask/new/", ask_views.ask_new, name="ask_new"),
    path("ask/retry/<int:pk>/", ask_views.ask_retry, name="ask_retry"),
    path("ask/proposals/confirm-all/", ask_views.ask_proposals_confirm_all, name="ask_proposals_confirm_all"),
    path("ask/proposals/<int:pk>/confirm/", ask_views.ask_proposal_confirm, name="ask_proposal_confirm"),
    path("ask/proposals/<int:pk>/cancel/", ask_views.ask_proposal_cancel, name="ask_proposal_cancel"),
    path("ask/documents/<int:pk>/", ask_views.ask_document_download, name="ask_document_download"),

    path("calendar/", views.calendar_page, name="calendar"),
    path("calendar/events.json", views.calendar_events_json, name="calendar_events_json"),

    path("tasks/", views.task_list, name="task_list"),
    path("tasks/new/", views.task_create, name="task_create"),
    path("tasks/quick-add/", views.task_quick_add, name="task_quick_add"),
    path("tasks/<int:pk>/edit/", views.task_edit, name="task_edit"),
    path("tasks/<int:pk>/complete/", views.task_complete, name="task_complete"),
    path("tasks/<int:pk>/reopen/", views.task_reopen, name="task_reopen"),
    path("tasks/<int:pk>/cancel/", views.task_cancel, name="task_cancel"),

    path("notes/", views.note_list, name="note_list"),
    path("notes/new/", views.note_create, name="note_create"),
    path("notes/<int:pk>/", views.note_detail, name="note_detail"),
    path("notes/<int:pk>/edit/", views.note_edit, name="note_edit"),
    path("notes/<int:pk>/delete/", views.note_delete, name="note_delete"),

    path("review/", views.review_list, name="review_list"),
    path("review/<int:pk>/", views.review_detail, name="review_detail"),

    path("healthz/", views.healthz, name="healthz"),
    path("health/", views.health, name="health"),
]
