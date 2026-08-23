from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("manifest.webmanifest", views.web_manifest, name="web_manifest"),
    path("service-worker.js", views.service_worker, name="service_worker"),

    path("", views.upcoming, name="upcoming"),

    path("calendar/", views.calendar_page, name="calendar"),
    path("calendar/events.json", views.calendar_events_json, name="calendar_events_json"),

    path("tasks/", views.task_list, name="task_list"),
    path("tasks/new/", views.task_create, name="task_create"),
    path("tasks/<int:pk>/edit/", views.task_edit, name="task_edit"),
    path("tasks/<int:pk>/complete/", views.task_complete, name="task_complete"),
    path("tasks/<int:pk>/cancel/", views.task_cancel, name="task_cancel"),

    path("notes/", views.note_list, name="note_list"),
    path("notes/new/", views.note_create, name="note_create"),
    path("notes/<int:pk>/", views.note_detail, name="note_detail"),
    path("notes/<int:pk>/edit/", views.note_edit, name="note_edit"),

    path("review/", views.review_list, name="review_list"),
    path("review/<int:pk>/", views.review_detail, name="review_detail"),

    path("healthz/", views.healthz, name="healthz"),
    path("health/", views.health, name="health"),
]
