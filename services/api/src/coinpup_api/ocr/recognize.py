"""Shared live preparation and recognition; no identities, templates or financial writes."""

import hashlib
import json
import time

from .engine_adapters import EngineError
from .field_parser import parse_document
from .parser_types import TextPage
from .pdf_prepare import PrepareLimits, prepare_pdf
from .pdf_probe import MAX_SOURCE_BYTES, _Uncertain

_LIMITS = PrepareLimits(dpi=300)
_ENGINE_REASONS = {
    "engine_unavailable",
    "engine_profile_invalid",
    "engine_initialization_failed",
    "engine_recognition_failed",
    "engine_result_invalid",
    "engine_limit",
}


def failed_result(reason, *, page_indices=()):
    return {
        "version": 1,
        "status": "failed",
        "reason": reason,
        "parsed": parse_document(()),
        "raw_text": "",
        "pages": [
            {
                "page_index": index,
                "width": None,
                "height": None,
                "geometry": None,
                "text": "",
                "words": [],
                "raster_rgb_sha256": None,
                "layer": "unknown",
                "route": "manual",
                "reason_code": reason,
            }
            for index in sorted(page_indices)
        ],
        "timings_ns": {"prepare": 0, "recognize": 0, "parse": 0},
    }


def recognize_document(data, media_type, adapter, *, page_done=None, _observe_native=None):
    """Consume each genuine raster while alive; manual pages retain their physical indices."""
    if (
        type(data) is not bytes
        or not 1 <= len(data) <= MAX_SOURCE_BYTES
        or media_type not in ("application/pdf", "image/jpeg", "image/png", "image/webp")
    ):
        return failed_result("request_invalid")
    started = time.monotonic_ns()
    slots, pages, errors, completed = {}, {}, {}, set()
    recognition_ns = 0

    def done(index):
        slots.setdefault(index, TextPage(1, 1, ()))
        if index not in completed:
            if page_done is not None:
                page_done(index)
            completed.add(index)

    def consume(index, source):
        nonlocal recognition_ns
        units = "pt" if isinstance(source, TextPage) else "px"
        digest = None
        if isinstance(source, TextPage):
            text_page = source
        else:
            # PdfBitmap.to_pil is a live view. RGB conversion and the adapter finish
            # before either the view or the native bitmap can be released.
            view = source.to_pil() if media_type == "application/pdf" else source
            try:
                with view.convert("RGB") as rgb:
                    rgb_bytes = rgb.tobytes()
                    width, height = rgb.size
                    digest = hashlib.sha256(rgb_bytes).hexdigest()
                    before = time.monotonic_ns()
                    try:
                        text_page = adapter.recognize(rgb_bytes, width, height)
                        if type(text_page) is not TextPage or (
                            text_page.width,
                            text_page.height,
                        ) != (width, height):
                            raise EngineError("engine_result_invalid")
                    except EngineError as error:
                        reason = (
                            error.reason
                            if error.reason in _ENGINE_REASONS
                            else "engine_result_invalid"
                        )
                        errors[index] = reason
                        text_page = TextPage(width, height, ())
                    except MemoryError:
                        raise
                    except Exception:
                        errors[index] = "engine_recognition_failed"
                        text_page = TextPage(width, height, ())
                    finally:
                        recognition_ns += time.monotonic_ns() - before
            finally:
                try:
                    if _observe_native is not None:
                        _observe_native()
                finally:
                    if media_type == "application/pdf":
                        view.close()
        slots[index] = text_page
        pages[index] = {
            "page_index": index,
            "width": text_page.width,
            "height": text_page.height,
            "geometry": {"width": text_page.width, "height": text_page.height, "units": units},
            "text": text_page.text,
            "words": [{"text": word.text, "bbox": list(word.bbox)} for word in text_page.words],
            "raster_rgb_sha256": digest,
        }

    if media_type == "application/pdf":
        prepared = prepare_pdf(
            data,
            _LIMITS,
            _page_consumer=consume,
            _page_done=done,
        )
    else:
        from .image_prepare import prepared_image

        reason = None
        try:
            with prepared_image(data, media_type, _LIMITS) as raster:
                consume(0, raster)
        except MemoryError:
            raise
        except _Uncertain as error:
            reason = error.reason
        except Exception:
            reason = "preparation_failed"
        finally:
            done(0)
        prepared = {
            "reason_code": None,
            "pages": [
                {
                    "page_index": 0,
                    "layer": "absent",
                    "route": "manual" if reason else "render",
                    "reason_code": reason,
                }
            ],
        }
    prepare_ns = time.monotonic_ns() - started - recognition_ns
    document_reason = prepared["reason_code"]
    manual_reason = document_reason
    metadata = {page["page_index"]: page for page in prepared["pages"]}
    for index in sorted(slots):
        information = metadata.get(index, {})
        page = pages.setdefault(
            index,
            {
                "page_index": index,
                "width": None,
                "height": None,
                "geometry": None,
                "text": "",
                "words": [],
                "raster_rgb_sha256": None,
            },
        )
        page.update({key: information.get(key) for key in ("layer", "route", "reason_code")})
        reason = document_reason or (
            information.get("reason_code") if information.get("route") == "manual" else None
        )
        if reason:
            manual_reason = manual_reason or reason
            slots[index] = TextPage(1, 1, ())
            page.update(text="", words=[], route="manual", reason_code=reason)
        elif index in errors:
            page["reason_code"] = errors[index]
    before = time.monotonic_ns()
    parsed = parse_document(tuple(slots[index] for index in sorted(slots)))
    parse_ns = time.monotonic_ns() - before
    result = {
        "version": 1,
        "status": "failed" if errors else "manual" if manual_reason else "processed",
        "reason": errors[min(errors)] if errors else manual_reason,
        "parsed": parsed,
        "raw_text": "\n\n".join(pages[index]["text"] for index in sorted(pages)),
        "pages": [pages[index] for index in sorted(pages)],
        "timings_ns": {"prepare": prepare_ns, "recognize": recognition_ns, "parse": parse_ns},
    }
    if (
        len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8"))
        > _LIMITS.output_bytes
    ):
        result.update(
            status="failed", reason="engine_limit", parsed=parse_document(()), raw_text=""
        )
        for page in result["pages"]:
            page.update(text="", words=[])
    return result
