import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from datetime import timedelta

from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clinic.services import BookingError

from .chat import answer
from .models import Conversation, Message, PendingAction
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
        return HttpResponseBadRequest("Message must be between 1 and 1000 characters.")
    if Message.objects.today_for(request.user).count() >= settings.ASSISTANT_DAILY_MESSAGE_LIMIT:
        return JsonResponse({"error": "Daily message limit reached. Come back tomorrow!"}, status=429)

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
