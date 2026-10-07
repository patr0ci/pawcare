from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from assistant import views as assistant_views
from clinic import views as clinic_views

urlpatterns = [
    path("", clinic_views.home, name="home"),
    path("healthz", assistant_views.health, name="health"),
    path("demo/", clinic_views.demo_login, name="demo_login"),
    path("my-pets/", clinic_views.my_pets, name="my_pets"),
    path("help/", include("helpcenter.urls")),
    path("assistant/", include("assistant.urls")),
    # Only login and logout: django.contrib.auth.urls also brings password reset/change, which render the admin's
    # templates (linking to the admin's path) and send mail this demo has no way to deliver.
    # A logged-in visitor is sent on instead of seeing the form, where "Use a demo account" would swap sessions.
    path("accounts/login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(settings.ADMIN_URL, admin.site.urls),
]
