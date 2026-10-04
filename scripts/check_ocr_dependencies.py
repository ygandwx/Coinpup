"""Audit installed optional wheels without disclosing host paths or license contents."""

import hashlib
import json
import re
import sys
from importlib import metadata

EXPECTED = {
    "charset-normalizer": "3.5.2",
    "cryptography": "50.0.2",
    "pdfminer.six": "20260107",
    "pdfplumber": "0.11.10",
    "Pillow": "12.3.0",
    "pypdf": "6.19.0",
    "pypdfium2": "5.13.0",
}
EXCLUDED = {"pymupdf", "fitz", "ocrmypdf"}
AGPL = re.compile(r"\b(?:Affero|AGPL)\b", re.IGNORECASE)


def audit():
    installed = {
        re.sub(r"[-_.]+", "-", dist.metadata.get("Name", "").lower())
        for dist in metadata.distributions()
    }
    if installed & EXCLUDED:
        raise ValueError("Excluded PDF dependency is installed.")
    packages = []
    for name, expected_version in EXPECTED.items():
        dist = metadata.distribution(name)
        if dist.version != expected_version:
            raise ValueError("An OCR dependency differs from its locked version.")
        declarations = {
            "license_expression": dist.metadata.get("License-Expression"),
            "license": dist.metadata.get("License"),
        }
        if any(AGPL.search(value or "") for value in declarations.values()):
            raise ValueError("Excluded license declaration is present.")
        notices = []
        for path in dist.files or ():
            if not (
                path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT"))
                or any(part.lower() == "licenses" for part in path.parts)
            ):
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Invalid relative license path.")
            raw = dist.locate_file(path).read_bytes()
            if not raw.strip() or AGPL.search(raw.decode("utf-8", errors="replace")):
                raise ValueError("License notice is empty or declares an excluded license.")
            notices.append({"path": path.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
        if not notices:
            raise ValueError("Installed wheel has no license notices.")
        wheel = dist.read_text("WHEEL")
        if wheel is None:
            raise ValueError("Installed distribution has no wheel metadata.")
        packages.append(
            {
                "name": name,
                "version": dist.version,
                **declarations,
                "tags": [line[5:] for line in wheel.splitlines() if line.startswith("Tag: ")],
                "notice_count": len(notices),
                "notices": sorted(notices, key=lambda item: item["path"]),
            }
        )
    return {
        "platform": sys.platform,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "packages": packages,
    }


def main():
    try:
        result = audit()
    except (OSError, ValueError, metadata.PackageNotFoundError):
        print(json.dumps({"error": "ocr_dependency_audit_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
