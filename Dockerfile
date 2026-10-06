FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    EMBEDDING_CACHE_DIR=/app/.cache/fastembed

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# Bake the embedding model into the image so containers don't download it on boot.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/.cache/fastembed')" \
 && DJANGO_SECRET_KEY=build DJANGO_DEBUG=false python manage.py collectstatic --noinput \
 && useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["./docker-entrypoint.sh"]
