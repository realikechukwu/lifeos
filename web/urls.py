from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
]
