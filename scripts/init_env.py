"""Create a local, ignored development configuration without printing secrets."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
password = secrets.token_hex(24)
template = (root / ".env.example").read_text(encoding="utf-8")
try:
    with path.open("x", encoding="utf-8") as output:
        output.write(template.replace("replace-with-a-generated-local-password", password))
except FileExistsError:
    raise SystemExit(".env already exists; it was not modified") from None
print("Created ignored .env for local development. Keep this file private.")
