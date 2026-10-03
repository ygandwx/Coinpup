"""Generate/check the real implemented API contract. Run from the repository root."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api/src"))

from coinpup_api.config import Settings  # noqa: E402
from coinpup_api.main import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
app = create_app(Settings(_env_file=None, environment="test"))
rendered = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
destination = ROOT / "contracts/openapi.json"
if args.check:
    if not destination.exists() or destination.read_text(encoding="utf-8") != rendered:
        raise SystemExit("OpenAPI contract is stale. Run python scripts/export_openapi.py")
    print("OpenAPI contract matches the application")
else:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    print("Wrote contracts/openapi.json")
