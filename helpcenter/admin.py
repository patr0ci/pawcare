from django.contrib import admin

from .models import Article


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    """Read-only: articles come from helpcenter/content/*.md (python manage.py ingest_helpcenter)."""

    list_display = ["title", "category", "slug", "updated_at"]
    list_filter = ["category"]
    search_fields = ["title", "body"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
