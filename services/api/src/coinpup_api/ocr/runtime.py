"""Fixed offline recognition policy; no database, network or caller-selected engine."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .image_prepare import ImageLimits
from .isolation import ProcessBudget
from .parser_types import ParserLimits
from .pdf_prepare import PrepareLimits
from .pdf_probe import ProbeLimits

_PACKAGE = Path(__file__).parent
_ASSETS = Path("/opt/coinpup-ocr")
_PROFILE = {"engine": "tesseract", "model_set": "fast", "psm": 6}
_SOURCES = (
    "runtime.py",
    "processor.py",
    "recognize.py",
    "engine_adapters.py",
    "field_parser.py",
    "parser_types.py",
    "pdf_probe.py",
    "pdf_prepare.py",
    "image_prepare.py",
    "isolation.py",
    "_isolation_child.py",
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def selection():
    value = json.loads((_PACKAGE / "selection.json").read_text(encoding="utf8"))
    if (
        _json(value["profile"]) != _json(_PROFILE)
        or value["ocr_prefill"] is not False
        or value["review_all_ocr_fields"] is not True
        or value["text_certain_prefill"] is not True
    ):
        raise ValueError("Unsupported recognition policy.")
    return value


def processing_configuration():
    """Freeze all current budgets and source identity before a queue intent is stored."""
    return {
        "version": 1,
        "selection": selection(),
        "probe": asdict(ProbeLimits()),
        "prepare": asdict(PrepareLimits(dpi=300)),
        "image": asdict(ImageLimits()),
        "parser": asdict(ParserLimits()),
        "process": asdict(ProcessBudget(300, 240, 4 * 1024**3, 16 * 1024**2, 128, 1048576)),
        "sources": {
            name: hashlib.sha256((_PACKAGE / name).read_bytes()).hexdigest() for name in _SOURCES
        },
    }


def validate_processing(value):
    # JSON comparison preserves bool/int and float/int distinctions as well as missing keys.
    if _json(value) != _json(processing_configuration()):
        raise ValueError("Unsupported frozen recognition configuration.")


def _verify_models():
    """Read only the fixed, read-only mount; verify before loading native libraries."""
    manifest_path = _ASSETS / "engine-assets.json"
    if manifest_path.resolve(strict=True) != manifest_path or not manifest_path.is_file():
        raise ValueError
    with manifest_path.open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != selection()["model_manifest_sha256"]:
        raise ValueError
    entries = {item["name"]: item for item in json.loads(raw)["files"]}
    for language in ("eng", "chi_sim", "chi_tra"):
        name = f"tesseract/fast/{language}.traineddata"
        entry, path = entries[name], _ASSETS / name
        if (
            path.resolve(strict=True) != path
            or not path.is_file()
            or path.stat().st_size != entry["byte_size"]
        ):
            raise ValueError
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError


class _LazyAdapter:
    def __init__(self):
        self.adapter = None

    def recognize(self, rgb, width, height):
        from .engine_adapters import EngineError, create_adapter

        if self.adapter is None:
            try:
                _verify_models()
                self.adapter = create_adapter(deepcopy(_PROFILE), _ASSETS)
            except (OSError, ValueError, KeyError, TypeError):
                raise EngineError("engine_unavailable") from None
        return self.adapter.recognize(rgb, width, height)

    def close(self):
        if self.adapter is not None:
            self.adapter.close()


def field_review(result):
    """Never rewrite raw parser certainty or use a text page to authorize an OCR page."""
    pages = {page["page_index"]: page for page in result["pages"]}
    review = []
    for field in result["parsed"]["fields"]:
        evidence = field["evidence"]
        sources = [pages.get(item["page"], {}) for item in evidence]
        text = bool(sources) and all(
            page.get("layer") == "present" and page.get("route") == "extract" for page in sources
        )
        ocr = any(
            page.get("layer") == "absent" and page.get("route") == "render" for page in sources
        )
        prefill = text and field["status"] == "certain"
        review.append(
            {
                "path": field["path"],
                "source": "text" if text else "ocr" if ocr else "unknown",
                "candidate_value": field["value"],
                "suggested_value": field["value"] if prefill else None,
                "requires_confirmation": not prefill,
            }
        )
    return review


def recognize_request(request):
    from .processor import _read_source, _request
    from .recognize import failed_result, recognize_document

    try:
        if type(request) is not dict or request.keys() != {
            "version",
            "action",
            "source",
            "media_type",
            "processing",
        }:
            raise ValueError
        if request["action"] != "recognize_document":
            raise ValueError
        validate_processing(request["processing"])
        media_type = request["media_type"]
        proxy = {key: request[key] for key in ("version", "source")}
        if media_type == "application/pdf":
            proxy["action"] = "inspect_pdf"
        else:
            proxy.update(action="prepare_image", media_type=media_type)
        source, path, *_ = _request(proxy)
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        return failed_result("request_invalid")
    try:
        data = _read_source(source, path)
    except (ValueError, OSError, RuntimeError):
        return failed_result("source_invalid")
    adapter = _LazyAdapter()
    try:
        result = recognize_document(data, media_type, adapter)
    finally:
        adapter.close()
    result["field_review"] = field_review(result)
    # Adding presentation policy must not exceed the existing output transport budget.
    if len(_json(result).encode("utf8")) > PrepareLimits().output_bytes:
        result = failed_result("engine_limit")
        result["field_review"] = []
    return result
