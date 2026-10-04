"""Audit the real PDF corpus builder, including its existing optional PDF dependencies."""

import json
import sys
from importlib import metadata

from check_ocr_dependencies import EXPECTED, audit


def main():
    try:
        result = audit(
            {**EXPECTED, "reportlab": "5.0.1", "fonttools": "4.66.1"},
            additional_notices=(
                "reportlab/fonts/00readme.txt",
                "reportlab/fonts/DarkGarden-readme.txt",
                "reportlab/fonts/bitstream-vera-license.txt",
            ),
        )
    except (OSError, ValueError, metadata.PackageNotFoundError):
        print(json.dumps({"error": "corpus_dependency_audit_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
