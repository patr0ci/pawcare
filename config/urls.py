from django.contrib import admin
from django.urls import include, path

from clinic import views as clinic_views

urlpatterns = [
    path("", clinic_views.home, name="home"),
    path("demo/", clinic_views.demo_login, name="demo_login"),
    path("my-pets/", clinic_views.my_pets, name="my_pets"),
    path("help/", include("helpcenter.urls")),
    path("assistant/", include("assistant.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
]
