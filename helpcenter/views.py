from itertools import groupby

from django.shortcuts import get_object_or_404, render

from .models import Article


def index(request):
    articles = Article.objects.all()
    categories = [(category, list(items)) for category, items in groupby(articles, key=lambda a: a.category)]
    return render(request, "helpcenter/index.html", {"categories": categories})


def article(request, slug):
    article = get_object_or_404(Article, slug=slug)
    paragraphs = [p.strip() for p in article.body.split("\n\n") if p.strip()]
    return render(request, "helpcenter/article.html", {"article": article, "paragraphs": paragraphs})
