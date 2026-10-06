from django.urls import path

from . import views

app_name = "assistant"
urlpatterns = [
    path("", views.chat, name="chat"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("messages/", views.send_message, name="send_message"),
    path("actions/<int:action_id>/confirm/", views.confirm_action, name="confirm_action"),
    path("actions/<int:action_id>/dismiss/", views.dismiss_action, name="dismiss_action"),
]
