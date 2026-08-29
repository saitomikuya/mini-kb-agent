"""Out-of-process heartbeat watchdog for blocking document conversion."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import time


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def refresh_job_heartbeat(database_path: Path, job_id: int) -> bool:
    """Refresh one RUNNING job and return whether it is still active."""
    with sqlite3.connect(database_path, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        updated = connection.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = 'RUNNING'",
            (utc_now_text(), job_id),
        )
        connection.commit()
        return updated.rowcount == 1


def parent_is_alive(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_watchdog(
    database_path: Path,
    job_id: int,
    parent_pid: int,
    interval: float,
) -> int:
    if job_id <= 0 or parent_pid <= 0 or interval <= 0:
        return 2

    while parent_is_alive(parent_pid):
        try:
            if not refresh_job_heartbeat(database_path, job_id):
                return 0
        except sqlite3.Error as exc:
            print(
                f"Job {job_id} heartbeat watchdog could not update SQLite: {exc}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(interval)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--interval", type=float, default=15.0)
    arguments = parser.parse_args()
    return run_watchdog(
        arguments.database,
        arguments.job_id,
        arguments.parent_pid,
        arguments.interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
