from django.contrib import admin

from .models import Appointment, Pet, Service, Tutor, Vet


class PetInline(admin.TabularInline):
    model = Pet
    extra = 0


@admin.register(Tutor)
class TutorAdmin(admin.ModelAdmin):
    list_display = ["__str__", "phone"]
    inlines = [PetInline]


@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    list_display = ["name", "species", "breed", "tutor"]
    list_filter = ["species"]


@admin.register(Vet)
class VetAdmin(admin.ModelAdmin):
    list_display = ["name", "specialty"]


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "duration_minutes", "price_usd"]


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "pet", "service", "vet", "status"]
    list_filter = ["status", "vet", "service"]
    date_hierarchy = "starts_at"
