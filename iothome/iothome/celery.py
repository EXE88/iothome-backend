import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iothome.settings")

app = Celery("iothome")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Defaults; django-celery-beat stores the live schedule in the database, so
# these entries are what a fresh install starts with.
app.conf.beat_schedule = {
    "sweep-offline-devices": {
        "task": "gadgets.tasks.sweep_offline_devices",
        "schedule": 30.0,
    },
    "reconcile-last-seen": {
        "task": "gadgets.tasks.reconcile_last_seen",
        "schedule": 300.0,
    },
    "expire-stale-commands": {
        "task": "gadgets.tasks.expire_stale_commands",
        "schedule": 60.0,
    },
    "prune-old-commands": {
        "task": "gadgets.tasks.prune_old_commands",
        "schedule": crontab(hour=4, minute=0),
    },
}
