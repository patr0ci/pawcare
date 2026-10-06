from django.conf import settings


def demo(request):
    return {"demo_public_dashboard": settings.DEMO_PUBLIC_DASHBOARD}
