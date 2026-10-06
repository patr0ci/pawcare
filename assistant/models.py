from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class Conversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class MessageQuerySet(models.QuerySet):
    def today_for(self, user):
        return self.filter(conversation__user=user, role=Message.Role.USER, created_at__date=timezone.localdate())

    def totals(self):
        return self.aggregate(
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            cost_usd=Sum("cost_usd"),
        )


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user"
        ASSISTANT = "assistant"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    sources = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=120, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    tool_calls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = MessageQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]


class PendingAction(models.Model):
    """A write the assistant proposed. Nothing changes until the user clicks Confirm."""

    class Kind(models.TextChoices):
        BOOK = "book"
        RESCHEDULE = "reschedule"
        CANCEL = "cancel"

    class Status(models.TextChoices):
        PENDING = "pending"
        CONFIRMED = "confirmed"
        DISMISSED = "dismissed"
        FAILED = "failed"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="actions")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    payload = models.JSONField()
    summary = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    result = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.kind}: {self.summary} [{self.status}]"
