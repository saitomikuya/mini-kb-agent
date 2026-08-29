"""Production container and entrypoint acceptance checks."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_docker_and_supervisor_contract() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    supervisor = (PROJECT_ROOT / "supervisord.conf").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12-slim\n")
    assert 'VOLUME ["/app/sources", "/app/data"]' in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8080/health" in dockerfile
    assert "python -m uvicorn app.main:app --host 0.0.0.0 --port 8080" in supervisor
    assert "--proxy-headers --forwarded-allow-ips=*" in supervisor
    assert "huey_consumer.py app.tasks.consumer.huey -w 1 -k thread" in supervisor
    assert supervisor.count("stopsignal=TERM") == 2
    assert supervisor.count("stopasgroup=true") == 2
    assert supervisor.count("killasgroup=true") == 2


def test_entrypoint_is_repeatable_and_preserves_database_and_secret(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    source_dir = tmp_path / "knowledge"
    environment = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "DATA_DIR": str(data_dir),
        "SOURCE_DIR": str(source_dir),
    }
    command = [
        "/bin/sh",
        str(PROJECT_ROOT / "entrypoint.sh"),
        sys.executable,
        "-c",
        "print('entrypoint-ready')",
    ]

    first = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "entrypoint-ready" in first.stdout
    for relative in ("md", "index", "tmp", "logs"):
        assert (data_dir / relative).is_dir()
    assert source_dir.is_dir()
    database = data_dir / "app.db"
    secret = data_dir / "app.secret"
    assert database.is_file()
    first_secret = secret.read_bytes()

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE restart_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO restart_marker VALUES ('preserved')")
        connection.commit()

    second = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "entrypoint-ready" in second.stdout
    assert secret.read_bytes() == first_secret
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM restart_marker").fetchone() == (
            "preserved",
        )
        job_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert {"paused_at", "paused_seconds"} <= job_columns
