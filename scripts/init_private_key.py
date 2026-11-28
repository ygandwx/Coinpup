"""Create a new Coinpup private account key outside the repository and backup volumes."""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path


def generate(path: Path, key_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id):
        raise ValueError("Invalid key identifier")
    document = {"id": key_id, "key": base64.b64encode(os.urandom(32)).decode("ascii")}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        json.dump(document, output)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("key_id")
    args = parser.parse_args()
    try:
        generate(args.path, args.key_id)
    except (OSError, ValueError):
        print("Could not create a new private account key file", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
