"""Decode an authorized frozen Logo only inside the bounded Invoice child."""

import hashlib
import hmac
import io
import warnings
from dataclasses import dataclass, field

from coinpup_api.ledger.errors import MoneyError

MAX_LOGO_BYTES = 8 * 1024 * 1024
MAX_LOGO_DIMENSION = 4096
MAX_LOGO_PIXELS = 4_000_000
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


@dataclass(frozen=True, slots=True)
class DecodedInvoiceLogo:
    width: int
    height: int
    pixels: bytes = field(repr=False)


def _error(code="invoice_render_logo_invalid"):
    return MoneyError(code, "The frozen Invoice Logo could not be decoded safely.")


def decode_invoice_logo(content, identity):
    """Consume identity from the verified frozen snapshot, never a URL or filesystem path.

    Original bytes remain immutable; returned RGBA pixels carry no source metadata.
    The fixed worker must also impose CPU, memory and wall-clock limits.
    """
    if identity is None:
        if content is not None:
            raise _error("invoice_render_integrity")
        return None
    if content is None:
        raise _error("invoice_render_logo_unavailable")
    if type(content) is not bytes:
        raise _error("invoice_render_integrity")
    if not 1 <= len(content) <= MAX_LOGO_BYTES:
        raise _error("invoice_render_limit")
    if (
        len(content) != identity["byte_size"]
        or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), identity["sha256"])
        or identity["media_type"] not in _FORMATS
    ):
        raise _error("invoice_render_integrity")
    try:
        from PIL import Image, ImageFile, ImageOps
    except ImportError:
        raise _error("invoice_render_unavailable") from None

    def check(image):
        if image.format != _FORMATS[identity["media_type"]]:
            raise _error("invoice_render_integrity")
        width, height = image.size
        if (
            not 1 <= width <= MAX_LOGO_DIMENSION
            or not 1 <= height <= MAX_LOGO_DIMENSION
            or width * height > MAX_LOGO_PIXELS
        ):
            raise _error("invoice_render_limit")
        if getattr(image, "n_frames", 1) != 1 or image.mode not in {
            "1",
            "L",
            "LA",
            "P",
            "RGB",
            "RGBA",
            "CMYK",
        }:
            raise _error()

    try:
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            raise _error("invoice_render_unavailable")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=list(_FORMATS.values())) as source:
                check(source)
                source.verify()
            # verify() does not decode pixels and invalidates some image streams.
            with Image.open(io.BytesIO(content), formats=list(_FORMATS.values())) as source:
                check(source)
                source.load()
                with ImageOps.exif_transpose(source) as oriented:
                    with oriented.convert("RGBA") as pixels:
                        return DecodedInvoiceLogo(pixels.width, pixels.height, pixels.tobytes())
    except MoneyError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _error("invoice_render_limit") from None
    except (OSError, ValueError, TypeError, SyntaxError, OverflowError):
        raise _error() from None
