from django import forms
from django.contrib import admin

from .models import Appointment, Pet, Service, Tutor, Vet
from .services import BookingError, check_slot

ACTIVE = (Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED)
SLOT_FIELDS = {"pet", "vet", "service", "starts_at"}


class PetInline(admin.TabularInline):
    model = Pet
    extra = 0
    # Allergies are edited on the pet's own page: the textarea pushed this table past the page's width.
    fields = ["name", "species", "breed", "birth_date", "weight_kg"]
    show_change_link = True
    # Ticking "Delete?" here removed a pet and its visit history with no confirmation page.
    can_delete = False


@admin.register(Tutor)
class TutorAdmin(admin.ModelAdmin):
    # Every demo account is named "Demo Visitor": the username is the column that tells tutors apart.
    list_display = ["user", "name", "phone"]
    list_select_related = ["user"]
    search_fields = ["user__username", "user__first_name", "user__last_name", "pets__name"]
    inlines = [PetInline]

    @admin.display(ordering="user__last_name")
    def name(self, obj):
        return obj.user.get_full_name()


@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    list_display = ["name", "species", "breed", "tutor"]
    list_filter = ["species"]
    list_select_related = ["tutor__user"]
    search_fields = ["name", "tutor__user__username", "tutor__user__first_name", "tutor__user__last_name"]
    autocomplete_fields = ["tutor"]


@admin.register(Vet)
class VetAdmin(admin.ModelAdmin):
    list_display = ["name", "specialty"]


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "duration_minutes", "price_usd"]


class AppointmentAdminForm(forms.ModelForm):
    """Staff bookings go through the same rules as the site and the assistant (opening hours, a free vet and pet,
    a vet who does the service), so the admin can't double-book either."""

    class Meta:
        model = Appointment
        fields = ["pet", "vet", "service", "starts_at", "status", "notes", "cancellation_reason"]
        help_texts = {"starts_at": "Clinic time, on the hour or half hour, within opening hours."}

    def clean(self):
        data = super().clean()
        # Only a new or moved visit, or one coming back from cancelled/completed, is checked: marking a past visit
        # completed or editing its notes mustn't fail as "in the past". self.instance still has the saved values.
        was_active = self.instance.pk and self.instance.status in ACTIVE
        moved = not was_active or SLOT_FIELDS & set(self.changed_data)
        slot = [data.get(field) for field in ("pet", "vet", "service", "starts_at")]
        if data.get("status") in ACTIVE and moved and all(slot):
            try:
                check_slot(*slot, ignore=self.instance if self.instance.pk else None)
            except BookingError as exc:
                raise forms.ValidationError(str(exc)) from None
        return data


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    form = AppointmentAdminForm
    list_display = ["starts_at", "pet", "tutor", "service", "vet", "status"]
    list_filter = ["status", "vet", "service"]
    list_select_related = ["pet__tutor__user", "service", "vet"]
    search_fields = ["pet__name", "pet__tutor__user__username"]
    date_hierarchy = "starts_at"
    # A select listed every pet as "Biscuit (Dog)" or "Miso (Cat)"; the lookup shows the pet list with tutors.
    raw_id_fields = ["pet"]

    @admin.display(description="Tutor", ordering="pet__tutor__user__username")
    def tutor(self, obj):
        return obj.pet.tutor
