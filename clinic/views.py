from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .demo import create_demo_tutor
from .models import Appointment


def home(request):
    return render(request, "clinic/home.html")


@require_POST
def demo_login(request):
    created_today = get_user_model().objects.filter(
        username__startswith="demo-", date_joined__date=timezone.localdate()
    ).count()
    if created_today >= settings.DEMO_ACCOUNTS_PER_DAY:
        return render(request, "clinic/home.html", {"demo_full": True}, status=429)
    tutor = create_demo_tutor()
    login(request, tutor.user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("assistant:chat")


@login_required
def my_pets(request):
    tutor = getattr(request.user, "tutor", None)
    pets = tutor.pets.all() if tutor else []
    upcoming = (
        Appointment.objects.active()
        .filter(pet__tutor=tutor, starts_at__gte=timezone.now())
        .select_related("pet", "vet", "service")
        if tutor
        else []
    )
    return render(request, "clinic/my_pets.html", {"pets": pets, "upcoming": upcoming})
