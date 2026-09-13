from celery import Celery  # type: ignore[import-untyped]

from app.config import settings

celery_app = Celery(
    "vora",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.video_tasks"],
)
celery_app.conf.update(
    task_default_queue=settings.video_generation_queue,
    task_routes={"app.video_tasks.execute_video_generation": {"queue": settings.video_generation_queue}},
)
