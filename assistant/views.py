import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import Truncator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from clinic.services import BookingError

from .chat import answer
from .models import Conversation, EvalRun, Message, PendingAction
from .tools import execute

MAX_QUESTION_CHARS = 1000
logger = logging.getLogger(__name__)


def current_conversation(request) -> Conversation:
    conversation = Conversation.objects.filter(user=request.user, id=request.session.get("conversation_id")).first()
    if conversation is None:
        conversation = Conversation.objects.create(user=request.user)
        request.session["conversation_id"] = conversation.id
    return conversation


def remaining_today(user) -> int:
    return max(settings.ASSISTANT_DAILY_MESSAGE_LIMIT - Message.objects.today_for(user).count(), 0)


def timeline(conversation: Conversation) -> list[dict]:
    """Messages and action cards in the order they happened, so a reload looks like the live chat."""
    messages = list(conversation.messages.all())
    items = [{"kind": "message", "at": m.created_at, "obj": m} for m in messages]
    for action in conversation.actions.all():
        # A card belongs under the reply of the turn that proposed it (the reply is saved when the turn ends).
        reply = next(
            (m for m in messages if m.role == Message.Role.ASSISTANT and m.created_at >= action.created_at), None
        )
        items.append({"kind": "action", "at": reply.created_at if reply else action.created_at, "obj": action})
    return sorted(items, key=lambda i: (i["at"], i["kind"] == "action"))


@never_cache  # Back from My pets must not show a cached page with a card that was confirmed since
@login_required
def chat(request):
    if request.GET.get("new"):
        request.session.pop("conversation_id", None)
        return redirect("assistant:chat")  # so a reload doesn't start yet another conversation
    conversation = current_conversation(request)
    return render(
        request,
        "assistant/chat.html",
        {
            "timeline": timeline(conversation),
            "remaining": remaining_today(request.user),
            # Said up front, so nobody types a question only to be told the demo is out for the day.
            "budget_spent": Message.objects.spent_today_usd() >= settings.ASSISTANT_DAILY_BUDGET_USD,
            # The daily budget and limit reset at midnight in the clinic time zone (CLINIC_TIME_ZONE).
            "clinic_time_zone": settings.TIME_ZONE.replace("_", " "),
        },
    )


@login_required
@require_POST
def send_message(request):
    # Postgres can't store NUL; left in, the question passed this check and failed only after retrieval.
    question = request.POST.get("message", "").replace("\x00", "").strip()
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
            try:
                # The question may have been saved (and counted) before the failure, so the counter needs this too.
                error["remaining"] = remaining_today(request.user)
            except DatabaseError:
                pass  # the database may be why it failed; the error still has to reach the client
            yield f"data: {json.dumps(error)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _lock_action(request, action_id) -> PendingAction:
    """Row-locked, so two tabs (or a double click) can't both act on the same proposal. Call inside a transaction."""
    return get_object_or_404(PendingAction.objects.select_for_update(), id=action_id, conversation__user=request.user)


def _already_settled(action: PendingAction) -> JsonResponse:
    """A stale page (another tab, or the chat again after Back) clicked a card that was settled earlier. Saying what
    happened lets the card show the real outcome instead of "No changes" over a booking that was made."""
    messages = {
        PendingAction.Status.CONFIRMED: "This proposal was already confirmed.",
        PendingAction.Status.DISMISSED: "This proposal was already dismissed.",
    }
    # failed: the saved reason, e.g. "That proposal expired. Please ask again."
    message = messages.get(action.status) or action.result or "This proposal can't be confirmed any more."
    return JsonResponse({"status": action.status, "message": message}, status=409)


def _log_outcome(action: PendingAction):
    # Recorded as an assistant turn so the model sees what actually happened in later questions.
    Message.objects.create(conversation=action.conversation, role=Message.Role.ASSISTANT, content=action.result)


@login_required
@require_POST
def confirm_action(request, action_id):
    with transaction.atomic():
        action = _lock_action(request, action_id)
        if action.status != PendingAction.Status.PENDING:
            return _already_settled(action)
        if timezone.now() - action.created_at > PendingAction.TTL:
            action.status, action.result = PendingAction.Status.FAILED, "That proposal expired. Please ask again."
        else:
            try:
                with transaction.atomic():
                    outcome = execute(action)
                action.status, action.result = PendingAction.Status.CONFIRMED, outcome
            except BookingError as exc:
                action.status, action.result = PendingAction.Status.FAILED, f"Couldn't do that: {exc}"
            except Exception:
                logger.exception("Confirming action %s failed", action.id)
                action.status = PendingAction.Status.FAILED
                action.result = "Something changed since this was proposed. Please ask the assistant again."
        action.result = Truncator(action.result).chars(PendingAction.RESULT_MAX)
        action.save(update_fields=["status", "result"])
        _log_outcome(action)
    return JsonResponse({"status": action.status, "message": action.result})


@login_required
@require_POST
def dismiss_action(request, action_id):
    with transaction.atomic():
        action = _lock_action(request, action_id)
        if action.status != PendingAction.Status.PENDING:
            return _already_settled(action)
        action.status, action.result = PendingAction.Status.DISMISSED, "Okay, I didn't change anything."
        action.save(update_fields=["status", "result"])
        _log_outcome(action)
    return JsonResponse({"status": action.status, "message": action.result})


def dashboard_allowed(user) -> bool:
    return user.is_staff or settings.DEMO_PUBLIC_DASHBOARD


def dashboard(request):
    """Staff view: what the assistant costs, how it's used, and how it scores on the eval set.
    On the public demo (DEMO_PUBLIC_DASHBOARD) anyone can see it, with visitor names hidden."""
    if not dashboard_allowed(request.user):
        if request.user.is_authenticated:
            raise PermissionDenied
        # The site's own login page, not admin:login: that would give away the non-default admin path.
        return redirect_to_login(request.get_full_path())
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
        replies.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(answers=Count("id"), cost=Sum("cost_usd"))
        .order_by("day")
    )
    by_model = replies.values("model").annotate(answers=Count("id"), cost=Sum("cost_usd")).order_by("-answers")
    top_users = (
        replies.values("conversation__user__username")
        .annotate(answers=Count("id"), cost=Sum("cost_usd"))
        .order_by("-cost")[:10]
    )
    tools: dict[str, int] = {}
    for trace in replies.exclude(tool_calls=[]).values_list("tool_calls", flat=True):
        for call in trace:
            tools[call["name"]] = tools.get(call["name"], 0) + 1
    if not request.user.is_staff:
        # Public demo viewers see the numbers, not other visitors' account names.
        top_users = [u | {"conversation__user__username": f"visitor {i}"} for i, u in enumerate(top_users, 1)]
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


def health(request):
    """For uptime monitors and the container healthcheck. 503 only when the database is down: a missing model key
    or a spent daily budget shows up as assistant_available: false, which a monitor can alert on, without the
    healthcheck taking the rest of the site (help center, booking pages) offline."""
    llm_configured = settings.LLM_PROVIDER == "fake" or bool(settings.LLM_API_KEY)
    try:
        budget_left = settings.ASSISTANT_DAILY_BUDGET_USD - Message.objects.spent_today_usd()
        database = True
    except DatabaseError:
        budget_left, database = 0.0, False
    return JsonResponse(
        {
            "ok": database,
            "database": database,
            "llm_configured": llm_configured,
            "assistant_available": database and llm_configured and budget_left > 0,
        },
        status=200 if database else 503,
    )
