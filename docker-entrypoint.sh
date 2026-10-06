#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py seed_clinic
python manage.py ingest_helpcenter
# gthread workers keep SSE streams from blocking other requests.
exec gunicorn config.wsgi --bind 0.0.0.0:8000 --workers 2 --worker-class gthread --threads 8 --timeout 120 --access-logfile -
