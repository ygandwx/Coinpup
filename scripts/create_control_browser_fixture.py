"""Create explicit fictional control facts only in the opted-in disposable Compose service."""

import json
import os

from check_control_restore import seed_controls
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.models import Ledger
from coinpup_api.models import Administrator
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def guard(settings):
    url = make_url(settings.database_url.get_secret_value())
    if (
        os.environ.get("COINPUP_CREATE_CONTROL_BROWSER_FIXTURE") != "1"
        or settings.environment != "test"
        or (url.host, url.database) != ("postgres", "coinpup")
    ):
        raise SystemExit("Requires explicit disposable Compose control browser opt-in")


def main():
    settings = Settings()
    guard(settings)
    database = Database(settings)
    try:
        with Session(database.engine) as session:
            owner = session.scalar(
                select(Administrator.id).where(Administrator.username == "admin-e2e")
            )
        if owner is None:
            raise SystemExit("Requires the fictional admin-e2e administrator")
        evidence = seed_controls(database.engine, owner)
        with Session(database.engine) as session:
            entity = session.get(Ledger, evidence["ledger"]).entity_id
        print(json.dumps({"entity": str(entity), "ledger": str(evidence["ledger"])}))
    finally:
        database.close()


if __name__ == "__main__":
    main()
