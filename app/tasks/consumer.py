"""Import target for the independent Huey consumer process."""

from app.config import get_settings
from app.services.jobs import reconcile_worker_jobs
from app.tasks.queue import build_job_task_queue


job_task_queue = build_job_task_queue(get_settings())
reconcile_worker_jobs(
    job_task_queue.session_factory,
    job_task_queue.enqueue,
)
huey = job_task_queue.huey
