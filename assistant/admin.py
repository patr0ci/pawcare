from django.contrib import admin
from django.db.models import Count, Sum

from .models import Conversation, EvalRun, Message, PendingAction


class ReadOnly:
    """Assistant records are an audit trail: staff read them to see what the model did, nobody edits them."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MessageInline(ReadOnly, admin.StackedInline):
    model = Message
    fields = ["role", "content", "model", "cost_usd", "latency_ms", "tool_calls", "created_at"]
    readonly_fields = fields
    extra = 0


class PendingActionInline(ReadOnly, admin.TabularInline):
    model = PendingAction
    fields = ["kind", "summary", "status", "result", "created_at"]
    readonly_fields = fields
    extra = 0


@admin.register(Conversation)
class ConversationAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["__str__", "user", "created_at", "messages", "cost"]
    date_hierarchy = "created_at"
    search_fields = ["user__username", "messages__content"]
    inlines = [MessageInline, PendingActionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n=Count("messages"), cost_sum=Sum("messages__cost_usd"))

    @admin.display(ordering="n")
    def messages(self, obj):
        return obj.n

    @admin.display(ordering="cost_sum", description="Cost (USD)")
    def cost(self, obj):
        return obj.cost_sum


@admin.register(Message)
class MessageAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "role", "short_content", "model", "cost_usd", "latency_ms"]
    list_filter = ["role", "model"]
    search_fields = ["content"]
    date_hierarchy = "created_at"

    @admin.display(description="Content")
    def short_content(self, obj):
        return obj.content[:120]


@admin.register(PendingAction)
class PendingActionAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "kind", "summary", "status"]
    list_filter = ["kind", "status"]


@admin.register(EvalRun)
class EvalRunAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "model", "passed", "total", "cost_usd"]
