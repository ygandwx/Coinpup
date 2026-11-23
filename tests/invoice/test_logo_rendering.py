"""Bounded real Logo decoding and bilingual PDFs from explicitly fictional files."""

import hashlib
import io
from dataclasses import FrozenInstanceError

import pytest
from coinpup_api.business.invoice_logo import (
    MAX_LOGO_BYTES,
    decode_invoice_logo,
)
from coinpup_api.business.invoice_pdf import render_invoice_text_pdf
from coinpup_api.business.invoice_render_process import render_invoice_pair
from coinpup_api.ledger.errors import MoneyError

from tests.invoice.test_pdf_render_core import fonts as fonts
from tests.invoice.test_pdf_render_core import inspect
from tests.unit.business.test_invoice_render_snapshot import freeze, inputs

pytestmark = pytest.mark.invoice


def image_bytes(kind="PNG", mode="RGBA", size=(64, 32), **save):
    from PIL import Image

    output = io.BytesIO()
    with Image.new("RGB" if mode == "P" else mode, size) as picture:
        picture.paste("red", (0, 0, size[0] // 2, size[1]))
        if mode == "RGBA":
            picture.paste((0, 0, 255, 80), (size[0] // 2, 0, size[0], size[1]))
        if mode == "P":
            with picture.convert("P") as palette:
                palette.save(output, format=kind, **save)
        else:
            picture.save(output, format=kind, **save)
    return output.getvalue()


def identity(raw, media="image/png"):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw), "media_type": media}


def frozen_logo(raw, media="image/png", count=1, *, long_lines=False):
    args = inputs(count, with_logo=True)
    if long_lines:
        for index, row in enumerate(args[0].document.lines):
            row.description = f"ROW-{index:03d} " + "虚构測試" * 498
            assert len(row.description) == 2000
    logo = args[3].model_copy(
        update={
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "detected_media_type": media,
        }
    )
    return freeze(*args[:3], logo)


def render(raw, frozen, fonts):
    return render_invoice_pair(
        frozen.content, expected_sha256=frozen.sha256, font_directory=fonts, logo_content=raw
    )


@pytest.mark.parametrize(
    "kind,mode,media",
    [
        ("PNG", "RGBA", "image/png"),
        ("PNG", "P", "image/png"),
        ("PNG", "L", "image/png"),
        ("PNG", "LA", "image/png"),
        ("JPEG", "RGB", "image/jpeg"),
        ("JPEG", "CMYK", "image/jpeg"),
        ("WEBP", "RGBA", "image/webp"),
    ],
)
def test_supported_frozen_pixels_and_real_child_pdfs(fonts, kind, mode, media):
    raw = image_bytes(kind, mode)
    logo = decode_invoice_logo(raw, identity(raw, media))
    assert (logo.width, logo.height) == (64, 32)
    assert len(logo.pixels) == 64 * 32 * 4 and "pixels=" not in repr(logo)
    with pytest.raises(FrozenInstanceError):
        logo.width = 1
    frozen = frozen_logo(raw, media)
    pair = render(raw, frozen, fonts)
    from pypdf import PdfReader

    for result in pair:
        assert "Fictional" in inspect(result.content)[0]
        reader = PdfReader(io.BytesIO(result.content), strict=True)
        images = reader.pages[0]["/Resources"]["/XObject"]
        assert len(images) == 1
        image = next(iter(images.values())).get_object()
        assert (image["/Width"], image["/Height"]) == (64, 32)
        assert image["/ColorSpace"] == "/DeviceRGB"
        assert result.content == render_invoice_text_pdf(
            frozen.content,
            expected_sha256=frozen.sha256,
            language=result.language,
            font_directory=fonts,
            logo_content=raw,
        )
        if mode == "RGBA":
            assert "/SMask" in image
    assert hashlib.sha256(raw).hexdigest() == frozen.decode()["logo"]["sha256"]


def test_orientation_and_alpha_keep_original_visual_facts_without_metadata():
    from PIL import Image

    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Fictional private metadata must not be copied"
    raw = image_bytes(exif=exif)
    logo = decode_invoice_logo(raw, identity(raw))
    assert (logo.width, logo.height) == (32, 64)
    with Image.frombytes("RGBA", (logo.width, logo.height), logo.pixels) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 0, 0, 255)
        assert decoded.getpixel((0, 63)) == (0, 0, 255, 80)
        assert decoded.info == {}


@pytest.mark.parametrize("change", ["sha256", "byte_size", "media_type"])
def test_bytes_cannot_rebind_the_frozen_logo_identity(fonts, change):
    raw = image_bytes()
    facts = identity(raw)
    facts[change] = {"sha256": "f" * 64, "byte_size": len(raw) + 1, "media_type": "image/jpeg"}[
        change
    ]
    with pytest.raises(MoneyError) as caught:
        decode_invoice_logo(raw, facts)
    assert caught.value.code == "invoice_render_integrity"
    frozen = frozen_logo(raw)
    with pytest.raises(MoneyError) as caught:
        render(raw + b"changed", frozen, fonts)
    assert caught.value.code == "invoice_render_integrity"


def test_no_logo_is_optional_but_missing_or_extraneous_bytes_are_not_ignored(fonts):
    raw = image_bytes()
    assert decode_invoice_logo(None, None) is None
    for content, facts, code in [
        (None, identity(raw), "invoice_render_logo_unavailable"),
        (raw, None, "invoice_render_integrity"),
        ("https://fictional.invalid/logo.png", identity(raw), "invoice_render_integrity"),
    ]:
        with pytest.raises(MoneyError) as caught:
            decode_invoice_logo(content, facts)
        assert caught.value.code == code
    with pytest.raises(MoneyError) as caught:
        render(raw, freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_integrity"


@pytest.mark.parametrize("size", [(4097, 1), (1, 4097), (2001, 2000)])
def test_dimensions_and_pixel_product_fail_before_pixel_decode(size):
    raw = image_bytes(size=size)
    with pytest.raises(MoneyError) as caught:
        decode_invoice_logo(raw, identity(raw))
    assert caught.value.code == "invoice_render_limit"


def test_full_pixel_bound_survives_the_actual_limited_child(fonts):
    raw = image_bytes(size=(2000, 2000))
    logo = decode_invoice_logo(raw, identity(raw))
    assert len(logo.pixels) == 16_000_000
    pair = render(raw, frozen_logo(raw, count=200, long_lines=True), fonts)
    assert len(pair) == 2
    for item in pair:
        text, pages = inspect(item.content)
        assert pages > 100
        assert all(text.count(f"ROW-{index:03d}") == 1 for index in range(200))


@pytest.mark.parametrize("size", [0, MAX_LOGO_BYTES + 1])
def test_encoded_byte_bound_rejects_before_decode_or_child(fonts, size):
    raw = b"x" * size
    with pytest.raises(MoneyError) as caught:
        decode_invoice_logo(raw, identity(raw))
    assert caught.value.code == "invoice_render_limit"
    with pytest.raises(MoneyError) as caught:
        render(raw, freeze(*inputs()), fonts)
    assert caught.value.code == "invoice_render_limit"


@pytest.mark.parametrize("kind,media", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_animation_is_rejected_instead_of_silently_selecting_a_frame(fonts, kind, media):
    from PIL import Image

    with Image.new("RGBA", (64, 32), "blue") as second:
        raw = image_bytes(kind, save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(MoneyError) as caught:
        render(raw, frozen_logo(raw, media), fonts)
    assert caught.value.code == "invoice_render_logo_invalid"


@pytest.mark.parametrize(
    "kind,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_truncated_content_never_returns_a_pdf_pair(fonts, kind, media):
    raw = image_bytes(kind, "RGB")[:-15]
    with pytest.raises(MoneyError) as caught:
        render(raw, frozen_logo(raw, media), fonts)
    assert caught.value.code == "invoice_render_logo_invalid"
    assert "PIL" not in str(caught.value)


def test_unsupported_bit_depth_does_not_silently_clip_colors(fonts):
    from PIL import Image

    output = io.BytesIO()
    with Image.new("I;16", (4, 4), 32768) as source:
        source.save(output, format="PNG")
    raw = output.getvalue()
    with pytest.raises(MoneyError) as caught:
        render(raw, frozen_logo(raw), fonts)
    assert caught.value.code == "invoice_render_logo_invalid"


def test_valid_png_header_with_enormous_dimensions_fails_before_allocation(fonts):
    import struct
    import zlib

    payload = b"IHDR" + struct.pack(">IIBBBBB", 1000000, 1000000, 8, 6, 0, 0, 0)
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + payload
    raw += struct.pack(">I", zlib.crc32(payload))
    for kind, data in [(b"IDAT", zlib.compress(b"\x00")), (b"IEND", b"")]:
        raw += struct.pack(">I", len(data)) + kind + data
        raw += struct.pack(">I", zlib.crc32(kind + data))
    with pytest.raises(MoneyError) as caught:
        render(raw, frozen_logo(raw), fonts)
    assert caught.value.code == "invoice_render_limit"
