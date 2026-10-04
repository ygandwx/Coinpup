"""Bounded preparation in the isolated child; no OCR engine or financial interpretation."""

import json
import logging
import math
import re
import unicodedata
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, fields
from io import BytesIO

from .pdf_probe import ProbeLimits, _Uncertain, _Warnings, manual_result, probe_pdf


@dataclass(frozen=True)
class PrepareLimits:
    dpi: int = 200
    max_side: int = 10000
    page_pixels: int = 20000000
    bitmap_bytes: int = 80000000
    document_pixels: int = 100000000
    image_pixels: int = 20000000
    image_sample_bytes: int = 134217728
    document_image_pixels: int = 100000000
    page_chars: int = 20000
    document_chars: int = 100000
    page_text_bytes: int = 32768
    document_text_bytes: int = 131072
    page_words: int = 2000
    document_words: int = 5000
    page_evidence_bytes: int = 131072
    document_evidence_bytes: int = 524288
    output_bytes: int = 786432

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            low, high = (72, 300) if field.name == "dpi" else (1, field.default)
            if field.name == "output_bytes":
                low = 128  # Always leave room for a fixed manual-result envelope.
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid PDF preparation limits.")


_DEFAULT = PrepareLimits()
_PROBE_DEFAULT = ProbeLimits()


def _json_bytes(value):
    # Match the isolated bootstrap's wire representation, including separator spaces.
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")


def _bound(value, maximum):
    if value > maximum:
        raise _Uncertain("prepare_limit")


def _charge(totals, key, value, maximum):
    totals[key] = totals.get(key, 0) + value
    _bound(totals[key], maximum)


def _geometry(page, probe):
    def number(value):
        value = probe.resolve(value)
        if not isinstance(value, (probe.g.NumberObject, probe.g.FloatObject)) or not math.isfinite(
            value
        ):
            raise _Uncertain("invalid_geometry")
        return float(value)

    def box(value):
        value = probe.resolve(value)
        if not isinstance(value, list) or len(value) != 4:
            raise _Uncertain("invalid_geometry")
        coords = tuple(number(item) for item in value)
        if coords[0] >= coords[2] or coords[1] >= coords[3]:
            raise _Uncertain("invalid_geometry")
        return coords

    if "/UserUnit" in page and number(page.get("/UserUnit")) != 1:
        raise _Uncertain("invalid_geometry")
    rotation = probe.resolve(page.get("/Rotate", 0))
    if not isinstance(rotation, int) or rotation not in (0, 90, 180, 270):
        raise _Uncertain("invalid_geometry")
    media = box(page.get("/MediaBox"))
    crop = box(page.get("/CropBox", page.get("/MediaBox")))
    width = min(media[2], crop[2]) - max(media[0], crop[0])
    height = min(media[3], crop[3]) - max(media[1], crop[1])
    if min(width, height) <= 0:
        raise _Uncertain("invalid_geometry")
    return (height, width) if rotation in (90, 270) else (width, height)


def _image_header(image, resources, probe, limits, totals):
    def integer(key):
        value = probe.resolve(image.get(key))
        if not isinstance(value, int) or value <= 0:
            raise _Uncertain("unsupported_image")
        return value

    width, height, bits = integer("/Width"), integer("/Height"), integer("/BitsPerComponent")
    color = probe.resolve(image.get("/ColorSpace"))
    if color not in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK") and isinstance(color, str):
        color = probe.resolve(probe.dictionary(resources.get("/ColorSpace")).get(color))
    components = (
        {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}.get(color)
        if isinstance(color, str)
        else None
    )
    if (
        not components
        or bits not in (1, 2, 4, 8, 16)
        or any(key in image for key in ("/ImageMask", "/Mask", "/Alternates"))
    ):
        raise _Uncertain("unsupported_image")
    _bound(max(width, height), limits.max_side)
    _bound(width * height, limits.image_pixels)
    _bound(((width * components * bits + 7) // 8) * height, limits.image_sample_bytes)
    _charge(totals, "image_pixels", width * height, limits.document_image_pixels)
    filters = probe.resolve(image.get("/Filter"))
    chain = [] if filters is None else filters if isinstance(filters, list) else [filters]
    chain = [probe.resolve(item) for item in chain]
    if chain not in ([], ["/FlateDecode"], ["/DCTDecode"], ["/ASCII85Decode", "/DCTDecode"]):
        raise _Uncertain("unsupported_image")
    if "/DecodeParms" in image:
        raise _Uncertain("unsupported_image")  # Predictors need a separate supported-subset review.
    if "/DCTDecode" not in chain:
        # Bound decoded materialization with the existing pypdf stream and document caps.
        expected = ((width * components * bits + 7) // 8) * height
        if len(probe.stream_data(image)) != expected:
            raise _Uncertain("unsupported_image")
    if "/DCTDecode" in chain:
        from PIL import Image

        # Image.open reads JPEG headers, never load()/convert() its pixels here.
        try:
            with warnings.catch_warnings(), BytesIO(image.get_data()) as stream:
                warnings.simplefilter("error")
                with Image.open(stream) as header:
                    if (
                        header.format != "JPEG"
                        or header.size != (width, height)
                        or bits != 8
                        or len(header.getbands()) != components
                    ):
                        raise _Uncertain("unsupported_image")
        except MemoryError:
            raise
        except Exception:
            raise _Uncertain("unsupported_image") from None


@contextmanager
def rendered_page(document, index, limits=_DEFAULT, *, totals=None, expected_size=None):
    """Yield a live RGBX bitmap; every consumer/view must finish before context exit."""
    import pypdfium2 as pdfium

    totals = {} if totals is None else totals
    page, created = document[index], []
    try:
        size = page.get_size()
        if any(not math.isfinite(value) or value <= 0 for value in size) or (
            expected_size is not None
            and any(
                not math.isclose(a, b, abs_tol=0.001)
                for a, b in zip(size, expected_size, strict=True)
            )
        ):
            raise _Uncertain("invalid_geometry")
        expected = tuple(math.ceil(value * limits.dpi / 72) for value in size)

        def maker(width, height, *, format, rev_byteorder):
            if (
                type(width) is not int
                or type(height) is not int
                or min(width, height) < 1
                or (width, height) != expected
                or format != pdfium.raw.FPDFBitmap_BGRx
                or not rev_byteorder
            ):
                raise _Uncertain("invalid_geometry")
            _bound(max(width, height), limits.max_side)
            _bound(width * height, limits.page_pixels)
            _bound(4 * width * height, limits.bitmap_bytes)
            _charge(totals, "pixels", width * height, limits.document_pixels)
            bitmap = pdfium.PdfBitmap.new_native(
                width, height, format, rev_byteorder=True, stride=4 * width
            )
            created.append(bitmap)
            return bitmap

        yield page.render(
            scale=limits.dpi / 72,
            rotation=0,
            crop=(0, 0, 0, 0),
            bitmap_maker=maker,
            force_bitmap_format=pdfium.raw.FPDFBitmap_BGRx,
            rev_byteorder=True,
            draw_annots=False,
            may_draw_forms=False,
            limit_image_cache=True,
        )
    finally:
        try:
            for bitmap in created:
                bitmap.close()
        finally:
            page.close()


def _extract(page, limits, totals):
    chars = page.chars  # pdfminer materializes this before our count; OS limits remain essential.
    _bound(len(chars), limits.page_chars)
    _charge(totals, "chars", len(chars), limits.document_chars)
    text = page.extract_text() or ""
    if (
        not text.strip()
        or "\ufffd" in text
        or re.search(r"\(cid:\d+\)", text)
        or any(
            unicodedata.category(char) in ("Cs", "Co", "Cn")
            or (unicodedata.category(char) == "Cc" and char not in "\n\r\t")
            for char in text
        )
    ):
        raise _Uncertain("unusable_text")
    words = []
    for word in page.extract_words():
        coords = [word[key] for key in ("x0", "top", "x1", "bottom")]
        if (
            any(not math.isfinite(value) for value in coords)
            or coords[0] > coords[2]
            or coords[1] > coords[3]
        ):
            raise _Uncertain("unusable_text")
        words.append({"text": word["text"], "bbox": coords})
    for key, value in (
        ("text_bytes", len(text.encode("utf8"))),
        ("words", len(words)),
        ("evidence_bytes", len(_json_bytes(words))),
    ):
        _bound(value, getattr(limits, "page_" + key))
        _charge(totals, key, value, getattr(limits, "document_" + key))
    return text, words


def prepare_pdf(
    data: bytes, limits: PrepareLimits = _DEFAULT, probe_limits: ProbeLimits = _PROBE_DEFAULT
) -> dict:
    if not isinstance(limits, PrepareLimits):
        return manual_result("request_invalid")
    totals, plans = {}, {}

    def admission(page, index, images, probe):
        plans[index] = _geometry(page, probe)
        for image, resources in images:
            _image_header(image, resources, probe, limits, totals)

    result = probe_pdf(data, probe_limits, _visitor=admission)
    for page in result["pages"]:
        page.update(text=None, words=[], raster=None)
    text_pages = [page for page in result["pages"] if page["route"] == "extract"]
    render_pages = [page for page in result["pages"] if page["route"] == "render"]
    if text_pages:
        _extract_pages(data, text_pages, limits, totals)
    if render_pages:
        import pypdfium2 as pdfium

        try:
            with pdfium.PdfDocument(data) as document:
                if len(document) != result["page_count"]:
                    raise _Uncertain("invalid_geometry")
                for page in render_pages:
                    try:
                        with rendered_page(
                            document,
                            page["page_index"],
                            limits,
                            totals=totals,
                            expected_size=plans[page["page_index"]],
                        ) as bitmap:
                            page["raster"] = {
                                "width": bitmap.width,
                                "height": bitmap.height,
                                "stride": bitmap.stride,
                                "format": "RGBX",
                                "dpi": limits.dpi,
                            }
                    except MemoryError:
                        raise
                    except Exception as error:
                        _manual(page, error)
        except MemoryError:
            raise
        except Exception as error:
            for page in render_pages:
                _manual(page, error)
    if len(_json_bytes(result)) > limits.output_bytes:
        return manual_result("prepare_limit")
    return result


def _manual(page, error):
    page.update(
        route="manual",
        reason_code=error.reason if isinstance(error, _Uncertain) else "preparation_failed",
        text=None,
        words=[],
        raster=None,
    )


def _extract_pages(data, pages, limits, totals):
    import pdfplumber

    warning = _Warnings()
    logger = logging.getLogger("pdfminer")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate, logger.level = [warning], False, logging.WARNING
    try:
        with (
            BytesIO(data) as stream,
            pdfplumber.open(
                stream,
                pages=[page["page_index"] + 1 for page in pages],
                repair=False,
                unicode_norm=None,
                raise_unicode_errors=True,
            ) as document,
        ):
            actual = {page.page_number - 1: page for page in document.pages}
            for page in pages:
                current = actual.get(page["page_index"])
                try:
                    if current is None:
                        raise _Uncertain("preparation_failed")
                    page["text"], page["words"] = _extract(current, limits, totals)
                except MemoryError:
                    raise
                except Exception as error:
                    _manual(page, error)
                finally:
                    if current is not None:
                        current.close()
    except MemoryError:
        raise
    except Exception as error:
        for page in pages:
            _manual(page, error)
    finally:
        logger.handlers, logger.propagate, logger.level = handlers, propagate, level
    if warning.seen:
        for page in pages:
            _manual(page, _Uncertain("unusable_text"))
