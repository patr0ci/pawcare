from django.contrib import admin
from django.db.models import Count, DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.text import Truncator

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
    list_display = ["__str__", "user", "created_at", "message_count", "cost"]
    date_hierarchy = "created_at"
    search_fields = ["user__username", "messages__content"]
    inlines = [MessageInline, PendingActionInline]

    def get_queryset(self, request):
        # Per-conversation subqueries, not Count/Sum over a join: a search on messages__content joins the
        # messages again, and the totals came out multiplied by the number of matches.
        messages = Message.objects.filter(conversation=OuterRef("pk")).order_by().values("conversation")
        return (
            super()
            .get_queryset(request)
            .annotate(
                n=Coalesce(Subquery(messages.annotate(n=Count("id")).values("n")), 0),
                cost_sum=Coalesce(
                    Subquery(messages.annotate(c=Sum("cost_usd")).values("c")),
                    Value(0),
                    output_field=DecimalField(max_digits=10, decimal_places=6),
                ),
            )
        )

    # Not named "messages": that is the reverse relation, and sorting the column ordered by a join on it.
    @admin.display(ordering="n", description="Messages")
    def message_count(self, obj):
        return obj.n

    @admin.display(ordering="cost_sum", description="Cost (USD)")
    def cost(self, obj):
        return obj.cost_sum


@admin.register(Message)
class MessageAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "conversation", "user", "role", "short_content", "model", "cost_usd", "latency_ms"]
    list_select_related = ["conversation__user"]
    list_filter = ["role", "model"]
    search_fields = ["content"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    @admin.display(ordering="conversation__user__username")
    def user(self, obj):
        return obj.conversation.user

    @admin.display(description="Content")
    def short_content(self, obj):
        return Truncator(obj.content).chars(120)


@admin.register(PendingAction)
class PendingActionAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "conversation", "kind", "summary", "status"]
    list_filter = ["kind", "status"]
    search_fields = ["summary"]
    date_hierarchy = "created_at"


@admin.register(EvalRun)
class EvalRunAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ["created_at", "model", "passed", "total", "cost_usd"]
