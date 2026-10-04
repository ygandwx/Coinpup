"""Finite image admission and live RGBX preparation inside the isolated child."""

import warnings
import zlib
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, fields
from io import BytesIO

from .pdf_prepare import PrepareLimits, _bound, _json_bytes
from .pdf_probe import MAX_SOURCE_BYTES, _Uncertain, manual_result

MEDIA_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


@dataclass(frozen=True)
class ImageLimits:
    working_bytes: int = 268435456
    metadata_bytes: int = 65536
    max_chunks: int = 128

    def __post_init__(self):
        if any(
            type(value := getattr(self, field.name)) is not int or not 1 <= value <= field.default
            for field in fields(self)
        ):
            raise ValueError("Invalid image preparation limits.")


_DEFAULT, _IMAGE_DEFAULT = PrepareLimits(), ImageLimits()


def _invalid(condition):
    if condition:
        raise _Uncertain("invalid_image")


def _unsupported(condition):
    if condition:
        raise _Uncertain("unsupported_image")


def _png(data, options):
    _invalid(not data.startswith(b"\x89PNG\r\n\x1a\n"))
    position, count, metadata, size, ended = 8, 0, 0, None, False
    while position < len(data):
        count += 1
        _bound(count, options.max_chunks)
        _invalid(position + 12 > len(data))
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        _invalid(end > len(data) or ended)
        _invalid(
            zlib.crc32(memoryview(data)[position + 4 : end - 4])
            != int.from_bytes(data[end - 4 : end], "big")
        )
        payload = data[position + 8 : end - 4]
        if kind == b"IHDR":
            _invalid(count != 1 or size is not None or length != 13)
            size = (int.from_bytes(payload[:4], "big"), int.from_bytes(payload[4:8], "big"))
            _unsupported(payload[8] not in (1, 2, 4, 8))
        elif kind == b"IEND":
            _invalid(length != 0 or end != len(data))
            ended = True
        elif kind == b"PLTE":
            _invalid(not 1 <= length <= 768 or length % 3 != 0)
        elif kind not in (b"IDAT", b"PLTE"):
            _unsupported(
                kind not in (b"eXIf", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"pHYs", b"sBIT", b"bKGD")
            )
            metadata += length
            _bound(metadata, options.metadata_bytes)
        position = end
    _invalid(size is None or not ended)
    return size, 4


def _jpeg(data, options):
    _invalid(not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"))
    position, count, metadata, size, components = 2, 0, 0, None, None
    while position < len(data):
        count += 1
        _bound(count, options.max_chunks)
        _invalid(data[position] != 255)
        while position < len(data) and data[position] == 255:
            position += 1
        _invalid(position + 3 > len(data))
        marker = data[position]
        length = int.from_bytes(data[position + 1 : position + 3], "big")
        end = position + 1 + length
        _invalid(length < 2 or end > len(data))
        payload = data[position + 3 : end]
        if marker in (0xC0, 0xC2):
            _invalid(size is not None or len(payload) < 6)
            _unsupported(payload[0] != 8 or payload[5] not in (1, 3, 4))
            _invalid(len(payload) != 6 + 3 * payload[5])
            size = (int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[1:3], "big"))
            components = payload[5]
        elif marker == 0xDA:
            _invalid(size is None)
            return size, components
        elif marker in (0xE0, 0xE1, 0xEE):
            _unsupported(marker == 0xE1 and not payload.startswith(b"Exif\x00\x00"))
            metadata += len(payload)
            _bound(metadata, options.metadata_bytes)
        else:
            _unsupported(marker not in (0xC4, 0xDB, 0xDD))
        position = end
    raise _Uncertain("invalid_image")


def _webp(data, options, limits):
    _invalid(len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP")
    _invalid(int.from_bytes(data[4:8], "little") + 8 != len(data))
    position, chunks, metadata = 12, {}, 0
    while position < len(data):
        _bound(len(chunks) + 1, options.max_chunks)
        _invalid(position + 8 > len(data))
        kind = data[position : position + 4]
        length = int.from_bytes(data[position + 4 : position + 8], "little")
        end = position + 8 + length
        _invalid(end + (length & 1) > len(data))
        _unsupported(kind not in (b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"EXIF"))
        _unsupported(kind in chunks)
        _invalid(length & 1 and data[end] != 0)
        chunks[kind] = data[position + 8 : end]
        if kind == b"EXIF":
            metadata += length
            _bound(metadata, options.metadata_bytes)
        position = end + (length & 1)
    _invalid(position != len(data))
    primary = [kind for kind in chunks if kind in (b"VP8 ", b"VP8L")]
    _unsupported(len(primary) != 1)
    kind, payload = primary[0], chunks[primary[0]]
    if kind == b"VP8 ":
        _invalid(len(payload) < 10 or payload[0] & 1 or payload[3:6] != b"\x9d\x01\x2a")
        dimensions = (
            int.from_bytes(payload[6:8], "little"),
            int.from_bytes(payload[8:10], "little"),
        )
        _unsupported(any(value & 0xC000 for value in dimensions))
        size = tuple(value & 0x3FFF for value in dimensions)
    else:
        _invalid(len(payload) < 5 or payload[0] != 0x2F)
        bits = int.from_bytes(payload[1:5], "little")
        _invalid(bits >> 29 != 0)
        size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if b"VP8X" in chunks:
        extended = chunks[b"VP8X"]
        _invalid(next(iter(chunks)) != b"VP8X" or len(extended) != 10)
        _unsupported(extended[0] & 0xE7 or extended[1:4] != b"\x00\x00\x00")
        canvas = (
            int.from_bytes(extended[4:7], "little") + 1,
            int.from_bytes(extended[7:10], "little") + 1,
        )
        _dimensions(canvas, 4, "image/webp", limits, options)
        _invalid(canvas != size or bool(extended[0] & 8) != (b"EXIF" in chunks))
    else:
        _unsupported(len(chunks) != 1)
    if b"ALPH" in chunks:
        alpha = chunks[b"ALPH"]
        _invalid(kind != b"VP8 " or list(chunks).index(b"ALPH") > list(chunks).index(kind))
        _invalid(not alpha or alpha[0] & 0xC0 or alpha[0] & 3 > 1 or (alpha[0] >> 4) & 3 > 1)
        _invalid(alpha[0] & 3 == 0 and len(alpha) != 1 + size[0] * size[1])
        _invalid(not chunks[b"VP8X"][0] & 0x10)
    return size, 4


def _dimensions(size, channels, media_type, limits, options):
    _invalid(any(type(value) is not int or value <= 0 for value in size))
    pixels = size[0] * size[1]
    _bound(max(size), limits.max_side)
    for bound in (
        limits.page_pixels,
        limits.image_pixels,
        limits.document_pixels,
        limits.document_image_pixels,
    ):
        _bound(pixels, bound)
    _bound(4 * pixels, limits.bitmap_bytes)
    _bound(channels * pixels, limits.image_sample_bytes)
    if media_type == "image/webp":
        # WebPAnimDecoder creates two 4-byte canvases inside Image.open(), before .size exists.
        _bound(8 * pixels, limits.image_sample_bytes)
    # Conservative live-buffer estimates, not an OS RSS limit. Source bytes have their own cap.
    _bound((24 if media_type == "image/webp" else 16) * pixels, options.working_bytes)


def _admit(data, media_type, limits, options):
    _invalid(type(data) is not bytes or not 1 <= len(data) <= MAX_SOURCE_BYTES)
    size, channels = (
        _webp(data, options, limits)
        if media_type == "image/webp"
        else {"image/jpeg": _jpeg, "image/png": _png}[media_type](data, options)
    )
    _dimensions(size, channels, media_type, limits, options)
    return size


@contextmanager
def prepared_image(data, media_type, limits=_DEFAULT, image_limits=_IMAGE_DEFAULT):
    """Yield upright white-backed RGBX; the consumer must finish before context exit."""
    if (
        type(media_type) is not str
        or media_type not in MEDIA_FORMATS
        or not isinstance(limits, PrepareLimits)
        or not isinstance(image_limits, ImageLimits)
    ):
        raise _Uncertain("request_invalid")
    size = _admit(data, media_type, limits, image_limits)
    from PIL import Image, ImageFile, ImageOps

    with ExitStack() as resources:
        truncated = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                stream = resources.enter_context(BytesIO(data))
                image = Image.open(stream, formats=[MEDIA_FORMATS[media_type]])
                resources.callback(image.close)
                _invalid(image.format != MEDIA_FORMATS[media_type] or image.size != size)
                _unsupported(
                    getattr(image, "n_frames", 1) != 1
                    or image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK")
                )
                _unsupported(any(key in image.info for key in ("icc_profile", "xmp")))
                exif = image.getexif()
                orientation = exif.get(274, 1)
                _unsupported(type(orientation) is not int or orientation not in range(1, 9))
                ImageOps.exif_transpose(
                    image, in_place=True
                )  # load happens only after all header budgets.
                _invalid(image.size != (size[::-1] if orientation in (5, 6, 7, 8) else size))
                if image.mode in ("LA", "RGBA") or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    resources.callback(rgba.close)
                    alpha = rgba.getchannel("A")
                    resources.callback(alpha.close)
                    opaque = Image.new("RGB", image.size, "white")
                    resources.callback(opaque.close)
                    opaque.paste(rgba, mask=alpha)
                    alpha.close()
                    rgba.close()
                    raster = opaque.convert("RGBX")
                else:
                    raster = image.convert("RGBX")
                resources.callback(raster.close)
                raster.info.clear()
                raster.getexif().clear()
        except MemoryError:
            raise
        except _Uncertain:
            raise
        except Exception:
            raise _Uncertain("invalid_image") from None
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = truncated
        yield raster


def prepare_image(data, media_type, limits=_DEFAULT, image_limits=_IMAGE_DEFAULT):
    if (
        type(media_type) is not str
        or media_type not in MEDIA_FORMATS
        or not isinstance(limits, PrepareLimits)
        or not isinstance(image_limits, ImageLimits)
    ):
        return manual_result("request_invalid")
    page = {
        "page_index": 0,
        "layer": "absent",
        "route": "render",
        "reason_code": None,
        "text": None,
        "words": [],
        "raster": None,
    }
    result = {"version": 1, "page_count": 1, "pages": [page], "reason_code": None}
    try:
        with prepared_image(data, media_type, limits, image_limits) as raster:
            page["raster"] = {
                "width": raster.width,
                "height": raster.height,
                "stride": 4 * raster.width,
                "format": "RGBX",
                "dpi": None,
            }
    except MemoryError:
        raise
    except Exception as error:
        page.update(
            route="manual",
            reason_code=error.reason if isinstance(error, _Uncertain) else "preparation_failed",
        )
    return (
        result
        if len(_json_bytes(result)) <= limits.output_bytes
        else manual_result("prepare_limit")
    )
