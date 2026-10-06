from django.urls import path

from . import views

app_name = "assistant"
urlpatterns = [
    path("", views.chat, name="chat"),
    path("messages/", views.send_message, name="send_message"),
]
