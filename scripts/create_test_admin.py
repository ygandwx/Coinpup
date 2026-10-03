"""Explicit fixture for disposable CI databases, never an application startup hook."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api/src"))

from coinpup_api.admin import create_admin  # noqa: E402
from coinpup_api.config import Settings  # noqa: E402
from coinpup_api.database import Database  # noqa: E402

if os.environ.get("COINPUP_CREATE_TEST_ADMIN") != "1":
    raise SystemExit("Set COINPUP_CREATE_TEST_ADMIN=1 only for a disposable test database")
settings = Settings()
if settings.environment != "test":
    raise SystemExit("This fixture requires COINPUP_ENVIRONMENT=test")
password = os.environ.get("E2E_PASSWORD")
if not password:
    raise SystemExit("E2E_PASSWORD is required")
database = Database(settings)
try:
    create_admin(database.engine, os.environ.get("E2E_USERNAME", "admin-e2e"), password)
finally:
    database.close()
print("Created disposable test administrator")
