from itertools import groupby

from django.shortcuts import get_object_or_404, render
from django.utils.text import Truncator

from .models import Article
from .rendering import render_body


def index(request):
    articles = Article.objects.all()
    categories = [(category, list(items)) for category, items in groupby(articles, key=lambda a: a.category)]
    return render(request, "helpcenter/index.html", {"categories": categories})


def article(request, slug):
    article = get_object_or_404(Article, slug=slug)
    links = {a.title: a.get_absolute_url() for a in Article.objects.exclude(id=article.id)}
    # Search results and link previews show the article's own opening, not the site-wide pitch.
    excerpt = Truncator(" ".join(article.body.split())).chars(160)
    context = {"article": article, "blocks": render_body(article.body, links), "excerpt": excerpt}
    return render(request, "helpcenter/article.html", context)
