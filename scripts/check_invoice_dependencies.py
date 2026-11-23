"""Audit separately installed Invoice rendering wheels without starting OCR or the API."""

import json
import sys
from importlib import metadata

from check_ocr_dependencies import audit

EXPECTED = {"reportlab": "5.0.1", "Pillow": "12.3.0", "charset-normalizer": "3.5.2"}


def main():
    try:
        result = audit(
            EXPECTED,
            additional_notices=(
                "reportlab/fonts/00readme.txt",
                "reportlab/fonts/DarkGarden-readme.txt",
                "reportlab/fonts/bitstream-vera-license.txt",
            ),
        )
    except (OSError, ValueError, metadata.PackageNotFoundError):
        print(json.dumps({"error": "invoice_dependency_audit_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
