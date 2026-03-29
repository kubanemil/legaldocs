import logging
import os

from celery import Celery
from celery.signals import after_setup_logger

from parent.logging_config import JSONFormatter

broker_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Use db 1 for backend
_redis_base = os.environ.get("REDIS_URL", "redis://redis:6379/0")
backend_url = _redis_base.rsplit("/", 1)[0] + "/1"

celery_app = Celery(
    "legaldocs",
    broker=broker_url,
    backend=backend_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_time_limit=300,  # 5 minutes
    task_soft_time_limit=270,
    include=["parent.workers.tasks"],
)


@after_setup_logger.connect
def configure_celery_logging(logger: logging.Logger, **kwargs) -> None:
    """Replace Celery's default log formatter with JSONFormatter."""
    formatter = JSONFormatter()
    for handler in logger.handlers:
        handler.setFormatter(formatter)
