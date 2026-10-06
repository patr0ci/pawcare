"""Django settings for PawCare. Everything environment-specific comes from env vars (see .env.example)."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
if not DEBUG and SECRET_KEY == "dev-only-insecure-key":
    raise RuntimeError("Set DJANGO_SECRET_KEY in production.")
ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "clinic",
    "helpcenter",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.demo",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "pawcare"),
        "USER": os.getenv("POSTGRES_USER", "pawcare"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "pawcare"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5434"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("CLINIC_TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

# --- AI assistant ---------------------------------------------------------
# Any OpenAI-compatible endpoint works: OpenRouter, OpenAI, DeepSeek, a local vLLM...
# LLM_PROVIDER=fake runs fully offline with canned answers (used by tests and the no-key demo).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai_compatible")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-v4.1-flash")
# USD per 1M tokens, used for the cost dashboard. Keep in sync with your provider's price page.
LLM_PRICE_INPUT_PER_M = float(os.getenv("LLM_PRICE_INPUT_PER_M", "0.055"))
LLM_PRICE_OUTPUT_PER_M = float(os.getenv("LLM_PRICE_OUTPUT_PER_M", "1.32"))

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "fastembed")  # or "fake"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = 384
EMBEDDING_CACHE_DIR = os.getenv("EMBEDDING_CACHE_DIR", str(BASE_DIR / ".cache" / "fastembed"))

RAG_TOP_K = int(os.getenv("RAG_TOP_K", "4"))
# Chunks farther than this cosine distance are ignored, so the model says "I don't know" instead of guessing.
RAG_MAX_DISTANCE = float(os.getenv("RAG_MAX_DISTANCE", "0.42"))

# Per-user cap on assistant messages per day, so a public demo can't run up the LLM bill.
ASSISTANT_DAILY_MESSAGE_LIMIT = int(os.getenv("ASSISTANT_DAILY_MESSAGE_LIMIT", "30"))
# Site-wide caps. Per-user limits alone don't bound spend on a public demo: anyone can create more demo accounts.
ASSISTANT_DAILY_BUDGET_USD = float(os.getenv("ASSISTANT_DAILY_BUDGET_USD", "1.00"))
DEMO_ACCOUNTS_PER_DAY = int(os.getenv("DEMO_ACCOUNTS_PER_DAY", "300"))

# Public demo only: let any logged-in visitor open the staff dashboard (it shows costs and eval results).
DEMO_PUBLIC_DASHBOARD = env_bool("DEMO_PUBLIC_DASHBOARD", False)
