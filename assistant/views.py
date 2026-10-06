import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .chat import answer
from .models import Conversation, Message

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
