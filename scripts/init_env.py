"""Create a local development configuration and an independently stored development key."""

import json
import os
import secrets
import tempfile
from pathlib import Path

from init_private_key import generate

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    raise SystemExit(".env already exists; it was not modified")

# A fresh checkout gets a distinct key. Bind just the file into the API and keep
# its host directory private even when rootless Docker maps the API to another UID.
config_home = Path.home() / ".config" / "coinpup"
config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
directory = Path(tempfile.mkdtemp(prefix="dev-key-", dir=config_home))
key_file = directory / "account.key"
generate(key_file, "dev")
if os.name == "posix":
    key_file.chmod(0o444)

template = (root / ".env.example").read_text(encoding="utf-8")
content = template.replace("replace-with-a-generated-local-password", secrets.token_hex(24))
content = content.replace("replace-with-private-key-file", json.dumps(key_file.as_posix()))
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    output.write(content)
print("Created local .env and a separate development key. Keep both private.")
