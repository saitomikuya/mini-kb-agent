#!/bin/sh
set -eu

python - <<'PY'
from app.config import get_settings
from app.services.auth import load_or_create_root_secret

settings = get_settings()
for directory in (
    settings.source_dir,
    settings.data_dir,
    settings.markdown_dir,
    settings.index_dir,
    settings.data_dir / "tmp",
    settings.data_dir / "logs",
):
    directory.mkdir(parents=True, exist_ok=True)

# Load and validate an existing secret, or create it exactly once.
load_or_create_root_secret(settings.secret_path)
PY

alembic upgrade head
exec "$@"
