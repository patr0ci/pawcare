"""Tools the model can call. Reads run immediately; writes only create a PendingAction for the user to confirm.

Every tool is scoped to the logged-in tutor: the model never passes a user id, so it can't reach anyone else's data.
"""

import json
from datetime import date, datetime

from django.utils import timezone
from django.utils.text import Truncator

from clinic import services
from clinic.models import Appointment, Service, Vet
from clinic.services import BookingError

from .models import Conversation, PendingAction

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_my_pets",
            "description": "List the logged-in client's pets with their ids, species and allergies.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "List bookable services with ids, duration and price.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_available_slots",
            "description": "Find free appointment times for a pet and service on a given date, across vets who treat that species.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pet_id": {"type": "integer"},
                    "service_id": {"type": "integer"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["pet_id", "service_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_appointments",
            "description": "List the client's upcoming appointments with ids.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_booking",
            "description": "Propose booking an appointment. The client must click Confirm before it is booked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pet_id": {"type": "integer"},
                    "service_id": {"type": "integer"},
                    "vet_id": {"type": "integer"},
                    "starts_at": {"type": "string", "description": "ISO 8601 start time from find_available_slots"},
                },
                "required": ["pet_id", "service_id", "vet_id", "starts_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_reschedule",
            "description": "Propose moving an appointment to a new time. The client must click Confirm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "starts_at": {"type": "string", "description": "ISO 8601 start time from find_available_slots"},
                },
                "required": ["appointment_id", "starts_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_cancellation",
            "description": "Propose cancelling an appointment. The client must click Confirm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        },
    },
]

STATUS_LABELS = {
    "list_my_pets": "Looking up your pets",
    "list_services": "Checking services",
    "find_available_slots": "Checking availability",
    "list_my_appointments": "Looking up your appointments",
    "propose_booking": "Preparing the booking",
    "propose_reschedule": "Preparing the change",
    "propose_cancellation": "Preparing the cancellation",
}


def fmt(dt: datetime) -> str:
    return timezone.localtime(dt).strftime("%a %b %-d, %-I:%M %p")


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


class ToolRunner:
    def __init__(self, conversation: Conversation):
        self.conversation = conversation
        self.tutor = getattr(conversation.user, "tutor", None)
        self.proposed: list[PendingAction] = []

    def run(self, name: str, arguments: str) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        if self.tutor is None:
            return {"error": "This account has no client profile, so there are no pets or appointments."}
        try:
            return handler(**json.loads(arguments or "{}"))
        except BookingError as exc:
            return {"error": str(exc)}
        except (TypeError, ValueError, KeyError, OverflowError, Service.DoesNotExist, Vet.DoesNotExist) as exc:
            return {"error": f"Invalid arguments: {exc}"}

    # --- reads ---------------------------------------------------------------
    def tool_list_my_pets(self):
        return {
            "pets": [
                {"id": p.id, "name": p.name, "species": p.species, "breed": p.breed, "allergies": p.allergies}
                for p in self.tutor.pets.all()
            ]
        }

    def tool_list_services(self):
        return {
            "services": [
                {"id": s.id, "name": s.name, "minutes": s.duration_minutes, "price_usd": str(s.price_usd)}
                for s in Service.objects.order_by("name")
            ]
        }

    def tool_find_available_slots(self, pet_id: int, service_id: int, date: str):
        pet = services.own_pet(self.tutor, pet_id)
        service = Service.objects.get(id=service_id)
        day = parse_date(date)
        slots = services.find_slots(pet, service, day)
        by_vet: dict[int, dict] = {}
        for vet, start in slots:
            entry = by_vet.setdefault(vet.id, {"vet_id": vet.id, "vet": vet.name, "specialty": vet.specialty, "starts_at": []})
            entry["starts_at"].append(timezone.localtime(start).isoformat(timespec="minutes"))
        if slots:
            note = ""
        elif day < timezone.localdate():
            note = "That date is in the past."
        else:
            note = "No free times that day (closed Sundays; Saturdays 9:00–13:00)."
        return {"date": day.isoformat(), "weekday": day.strftime("%A"), "vets": list(by_vet.values()), "note": note}

    def tool_list_my_appointments(self):
        upcoming = (
            Appointment.objects.active()
            .filter(pet__tutor=self.tutor, starts_at__gte=timezone.now())
            .select_related("pet", "vet", "service")
        )
        return {
            "appointments": [
                {"id": a.id, "pet": a.pet.name, "service": a.service.name, "vet": a.vet.name, "when": fmt(a.starts_at)}
                for a in upcoming
            ]
        }

    # --- writes: propose only --------------------------------------------------
    def _propose(self, kind: str, payload: dict, summary: str) -> dict:
        action = PendingAction.objects.create(
            conversation=self.conversation, kind=kind, payload=payload,
            summary=Truncator(summary).chars(PendingAction.SUMMARY_MAX),
        )
        self.proposed.append(action)
        return {
            "status": "awaiting_client_confirmation",
            "summary": summary,
            "instruction": "Tell the client to review and click Confirm. Do not say it is done.",
        }

    def tool_propose_booking(self, pet_id: int, service_id: int, vet_id: int, starts_at: str):
        pet = services.own_pet(self.tutor, pet_id)
        service, vet, start = Service.objects.get(id=service_id), Vet.objects.get(id=vet_id), parse_time(starts_at)
        if (vet, start) not in services.find_slots(pet, service, timezone.localtime(start).date()):
            raise BookingError("That time isn't available. Check find_available_slots again.")
        summary = f"Book {service.name} for {pet.name} with {vet.name} on {fmt(start)} (${service.price_usd})"
        payload = {"pet_id": pet.id, "service_id": service.id, "vet_id": vet.id, "starts_at": start.isoformat()}
        return self._propose(PendingAction.Kind.BOOK, payload, summary)

    def tool_propose_reschedule(self, appointment_id: int, starts_at: str):
        appointment = services.own_appointment(self.tutor, appointment_id)
        start = parse_time(starts_at)
        services.check_slot(appointment.pet, appointment.vet, appointment.service, start, ignore=appointment)
        fee = services.late_change_fee(appointment)
        summary = f"Move {appointment.pet.name}'s {appointment.service.name} to {fmt(start)}"
        if fee:
            summary += f" (late change: ${fee} fee)"
        payload = {"appointment_id": appointment.id, "starts_at": start.isoformat()}
        return self._propose(PendingAction.Kind.RESCHEDULE, payload, summary)

    def tool_propose_cancellation(self, appointment_id: int, reason: str = ""):
        appointment = services.own_appointment(self.tutor, appointment_id)
        fee = services.late_change_fee(appointment)
        summary = f"Cancel {appointment.pet.name}'s {appointment.service.name} on {fmt(appointment.starts_at)}"
        if fee:
            summary += f" (less than 24 h notice: ${fee} fee)"
        return self._propose(PendingAction.Kind.CANCEL, {"appointment_id": appointment.id, "reason": reason}, summary)


def execute(action: PendingAction) -> str:
    """Run a confirmed action through the clinic's own booking rules. Raises BookingError."""
    tutor, p = action.conversation.user.tutor, action.payload
    if action.kind == PendingAction.Kind.BOOK:
        a = services.book(tutor, p["pet_id"], p["service_id"], p["vet_id"], parse_time(p["starts_at"]))
        return f"Booked: {a.service.name} for {a.pet.name} with {a.vet.name} on {fmt(a.starts_at)}."
    if action.kind == PendingAction.Kind.RESCHEDULE:
        a = services.reschedule(tutor, p["appointment_id"], parse_time(p["starts_at"]))
        return f"Moved: {a.pet.name}'s {a.service.name} is now on {fmt(a.starts_at)}."
    a = services.cancel(tutor, p["appointment_id"], p.get("reason", ""))
    return f"Cancelled: {a.pet.name}'s {a.service.name} on {fmt(a.starts_at)}."
