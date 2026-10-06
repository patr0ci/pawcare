import json
import logging

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse, StreamingHttpResponse
from datetime import timedelta

from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clinic.services import BookingError

from .chat import answer
from .models import Conversation, EvalRun, Message, PendingAction
from .tools import execute

ACTION_TTL = timedelta(minutes=30)

MAX_QUESTION_CHARS = 1000
logger = logging.getLogger(__name__)


def current_conversation(request) -> Conversation:
    conversation = Conversation.objects.filter(user=request.user, id=request.session.get("conversation_id")).first()
    if conversation is None:
        conversation = Conversation.objects.create(user=request.user)
        request.session["conversation_id"] = conversation.id
    return conversation


@login_required
def chat(request):
    if request.GET.get("new"):
        request.session.pop("conversation_id", None)
    conversation = current_conversation(request)
    return render(
        request,
        "assistant/chat.html",
        {
            "messages_": conversation.messages.all(),
            "pending": conversation.actions.filter(status=PendingAction.Status.PENDING),
            "remaining": settings.ASSISTANT_DAILY_MESSAGE_LIMIT - Message.objects.today_for(request.user).count(),
        },
    )


@login_required
@require_POST
def send_message(request):
    question = request.POST.get("message", "").strip()
    if not question or len(question) > MAX_QUESTION_CHARS:
        return JsonResponse({"error": "Message must be between 1 and 1000 characters."}, status=400)
    if Message.objects.today_for(request.user).count() >= settings.ASSISTANT_DAILY_MESSAGE_LIMIT:
        return JsonResponse({"error": "Daily message limit reached. Come back tomorrow!"}, status=429)
    if Message.objects.spent_today_usd() >= settings.ASSISTANT_DAILY_BUDGET_USD:
        return JsonResponse({"error": "The demo has used up today's AI budget. Please come back tomorrow!"}, status=429)

    conversation = current_conversation(request)

    def event_stream():
        try:
            for event in answer(conversation, question):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("Assistant failed to answer")
            error = {"type": "error", "message": "The assistant is unavailable right now. Please try again."}
            yield f"data: {json.dumps(error)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _own_pending_action(request, action_id) -> PendingAction:
    return get_object_or_404(
        PendingAction, id=action_id, conversation__user=request.user, status=PendingAction.Status.PENDING
    )


def _log_outcome(action: PendingAction, text: str):
    # Recorded as an assistant turn so the model sees what actually happened in later questions.
    Message.objects.create(conversation=action.conversation, role=Message.Role.ASSISTANT, content=text)


@login_required
@require_POST
def confirm_action(request, action_id):
    action = _own_pending_action(request, action_id)
    if timezone.now() - action.created_at > ACTION_TTL:
        action.status, action.result = PendingAction.Status.FAILED, "Expired. Please ask again."
    else:
        try:
            action.status, action.result = PendingAction.Status.CONFIRMED, execute(action)
        except BookingError as exc:
            action.status, action.result = PendingAction.Status.FAILED, f"Couldn't do that: {exc}"
    action.save(update_fields=["status", "result"])
    _log_outcome(action, action.result)
    return JsonResponse({"status": action.status, "message": action.result})


@login_required
@require_POST
def dismiss_action(request, action_id):
    action = _own_pending_action(request, action_id)
    action.status, action.result = PendingAction.Status.DISMISSED, "Okay, I didn't change anything."
    action.save(update_fields=["status", "result"])
    _log_outcome(action, action.result)
    return JsonResponse({"status": action.status, "message": action.result})


def dashboard_allowed(user) -> bool:
    return user.is_staff or (settings.DEMO_PUBLIC_DASHBOARD and user.is_authenticated)


def dashboard(request):
    """Staff view: what the assistant costs, how it's used, and how it scores on the eval set.
    On the public demo (DEMO_PUBLIC_DASHBOARD) any logged-in visitor can see it."""
    if not dashboard_allowed(request.user):
        return staff_member_required(lambda r: None)(request)
    since = timezone.now() - timedelta(days=30)
    replies = Message.objects.filter(role=Message.Role.ASSISTANT, created_at__gte=since).exclude(model="")
    totals = replies.aggregate(
        answers=Count("id"),
        cost=Sum("cost_usd"),
        tokens_in=Sum("prompt_tokens"),
        tokens_out=Sum("completion_tokens"),
        latency=Avg("latency_ms"),
    )
    daily = list(
        replies.annotate(day=TruncDate("created_at")).values("day").annotate(answers=Count("id"), cost=Sum("cost_usd")).order_by("day")
    )
    by_model = replies.values("model").annotate(answers=Count("id"), cost=Sum("cost_usd")).order_by("-answers")
    top_users = (
        replies.values("conversation__user__username").annotate(answers=Count("id"), cost=Sum("cost_usd")).order_by("-cost")[:10]
    )
    tools: dict[str, int] = {}
    for trace in replies.exclude(tool_calls=[]).values_list("tool_calls", flat=True):
        for call in trace:
            tools[call["name"]] = tools.get(call["name"], 0) + 1
    actions = PendingAction.objects.filter(created_at__gte=since).values("kind", "status").annotate(n=Count("id"))
    peak = max((d["answers"] for d in daily), default=0)
    return render(
        request,
        "assistant/dashboard.html",
        {
            "totals": totals,
            "cost_per_answer": (totals["cost"] or 0) / totals["answers"] if totals["answers"] else 0,
            "daily": [d | {"pct": round(100 * d["answers"] / peak) if peak else 0} for d in daily],
            "by_model": by_model,
            "top_users": top_users,
            "tools": sorted(tools.items(), key=lambda t: -t[1]),
            "actions": actions,
            "eval_run": EvalRun.objects.first(),
        },
    )
