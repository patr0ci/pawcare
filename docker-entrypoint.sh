#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py seed_clinic
python manage.py ingest_helpcenter
python manage.py cleanup_demo_users --days 2
# Hourly cleanup while the container runs (no cron in the image).
( while sleep 3600; do python manage.py cleanup_demo_users --days 2 >/dev/null 2>&1; done ) &
# gthread workers keep SSE streams from blocking other requests.
exec gunicorn config.wsgi --bind 0.0.0.0:8000 --workers 2 --worker-class gthread --threads 8 --timeout 120 --access-logfile -
