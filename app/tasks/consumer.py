"""Import target for the independent Huey consumer process."""

from app.config import get_settings
from app.tasks.queue import build_job_task_queue


job_task_queue = build_job_task_queue(get_settings())
huey = job_task_queue.huey
