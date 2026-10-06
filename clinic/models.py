from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# Opening hours used to generate bookable slots (Mon–Fri 9:00–18:00, Sat 9:00–13:00).
OPENING_HOURS = {
    0: (time(9), time(18)),
    1: (time(9), time(18)),
    2: (time(9), time(18)),
    3: (time(9), time(18)),
    4: (time(9), time(18)),
    5: (time(9), time(13)),
}
SLOT_STEP = timedelta(minutes=30)


class Tutor(models.Model):
    """A pet owner. Linked to a login so the assistant can only act on the owner's own pets."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tutor")
    phone = models.CharField(max_length=30, blank=True)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Pet(models.Model):
    class Species(models.TextChoices):
        DOG = "dog", "Dog"
        CAT = "cat", "Cat"
        RABBIT = "rabbit", "Rabbit"
        BIRD = "bird", "Bird"
        OTHER = "other", "Other"

    tutor = models.ForeignKey(Tutor, on_delete=models.CASCADE, related_name="pets")
    name = models.CharField(max_length=80)
    species = models.CharField(max_length=10, choices=Species.choices)
    breed = models.CharField(max_length=80, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    allergies = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.get_species_display()})"


class Vet(models.Model):
    name = models.CharField(max_length=120)
    specialty = models.CharField(max_length=120, blank=True)
    treats = models.JSONField(default=list, help_text="Species codes this vet sees, e.g. ['dog', 'cat'].")

    def __str__(self):
        return self.name


class Service(models.Model):
    name = models.CharField(max_length=120, unique=True)
    duration_minutes = models.PositiveIntegerField(default=30)
    price_usd = models.DecimalField(max_digits=8, decimal_places=2)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class AppointmentQuerySet(models.QuerySet):
    def active(self):
        return self.exclude(status=Appointment.Status.CANCELLED)


class Appointment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CONFIRMED = "confirmed", "Confirmed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, related_name="appointments")
    vet = models.ForeignKey(Vet, on_delete=models.PROTECT, related_name="appointments")
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    notes = models.TextField(blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppointmentQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at"]

    def __str__(self):
        return f"{self.pet} · {self.service} · {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def ends_at(self):
        return self.starts_at + timedelta(minutes=self.service.duration_minutes)


def available_slots(service: Service, vet: Vet, day) -> list[datetime]:
    """Start times on `day` where `vet` is free for the whole duration of `service`."""
    hours = OPENING_HOURS.get(day.weekday())
    if not hours:
        return []
    tz = timezone.get_current_timezone()
    opens = datetime.combine(day, hours[0], tzinfo=tz)
    closes = datetime.combine(day, hours[1], tzinfo=tz)
    duration = timedelta(minutes=service.duration_minutes)
    busy = [
        (a.starts_at, a.ends_at)
        for a in Appointment.objects.active().filter(vet=vet, starts_at__date=day).select_related("service")
    ]
    now = timezone.now()
    slots = []
    start = opens
    while start + duration <= closes:
        end = start + duration
        if start > now and all(end <= b_start or start >= b_end for b_start, b_end in busy):
            slots.append(start)
        start += SLOT_STEP
    return slots
