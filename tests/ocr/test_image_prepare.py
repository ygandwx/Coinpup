"""Fictional pixels exercise real image codecs and preallocation boundaries, never OCR."""

import hashlib
import json
import struct
import sys
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest
from coinpup_api.ocr.isolation import ProcessBudget, run_isolated
from coinpup_api.ocr.pdf_prepare import PrepareLimits
from coinpup_api.ocr.processor import process

pytestmark = pytest.mark.ocr
COLORS = [
    (240, 10, 20),
    (20, 220, 30),
    (20, 30, 240),
    (50, 60, 70),
    (100, 110, 120),
    (150, 160, 170),
]
MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def encoded(format="PNG", *, orientation=None, animated=False, mode="RGB", lossless=True):
    from PIL import Image

    with Image.new(mode, (3, 2)) as image, BytesIO() as stream:
        image.putdata(
            COLORS if mode == "RGB" else [(255, 0, 0, 255), (0, 0, 0, 0), (0, 255, 0, 128)] * 2
        )
        options = {"lossless": lossless} if format == "WEBP" else {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        if animated:
            with Image.new(mode, image.size, "white") as other:
                image.save(stream, format, save_all=True, append_images=[other], **options)
        else:
            image.save(stream, format, **options)
        return stream.getvalue()


def prepare(data, media_type="image/png", *, image_limits=None, **limits):
    from coinpup_api.ocr.image_prepare import ImageLimits, prepare_image

    return prepare_image(
        data, media_type, replace(PrepareLimits(), **limits), image_limits or ImageLimits()
    )


def page(result):
    assert result["version"] == 1 and result["page_count"] == 1 and result["reason_code"] is None
    assert len(result["pages"]) == 1
    return result["pages"][0]


def manual(result, reason):
    current = page(result)
    assert current == {
        "page_index": 0,
        "layer": "absent",
        "route": "manual",
        "reason_code": reason,
        "text": None,
        "words": [],
        "raster": None,
    }


@pytest.mark.parametrize("format", MIMES)
def test_real_single_frame_codecs_decode_to_live_rgbx_and_safe_metadata(format):
    from coinpup_api.ocr.image_prepare import prepared_image

    data = encoded(format)
    with prepared_image(data, MIMES[format]) as image:
        assert image.mode == "RGBX" and image.size == (3, 2)
        assert (
            image.getpixel((0, 0))[:3] == COLORS[0]
            if format != "JPEG"
            else image.getpixel((0, 0))[:3] != (0, 0, 0)
        )
        assert not image.info
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
    assert page(prepare(data, MIMES[format]))["raster"] == {
        "width": 3,
        "height": 2,
        "stride": 12,
        "format": "RGBX",
        "dpi": None,
    }


def test_real_simple_lossy_webp_and_its_pre_native_canvas_budget(monkeypatch):
    from PIL import Image

    data = encoded("WEBP", lossless=False)
    assert data[12:16] == b"VP8 " and page(prepare(data, "image/webp"))["route"] == "render"
    calls, original = [], Image.open

    def opened(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", opened)
    # Final 4-byte RGBX fits; the two native WebP canvases need eight bytes per pixel.
    manual(prepare(data, "image/webp", image_sample_bytes=7 * 6), "prepare_limit")
    assert calls == []


@pytest.mark.parametrize(
    "orientation,indices,size",
    [
        (1, [0, 1, 2, 3, 4, 5], (3, 2)),
        (2, [2, 1, 0, 5, 4, 3], (3, 2)),
        (3, [5, 4, 3, 2, 1, 0], (3, 2)),
        (4, [3, 4, 5, 0, 1, 2], (3, 2)),
        (5, [0, 3, 1, 4, 2, 5], (2, 3)),
        (6, [3, 0, 4, 1, 5, 2], (2, 3)),
        (7, [5, 2, 4, 1, 3, 0], (2, 3)),
        (8, [2, 5, 1, 4, 0, 3], (2, 3)),
    ],
)
def test_exif_orientation_is_applied_once_to_actual_lossless_pixels(orientation, indices, size):
    from coinpup_api.ocr.image_prepare import prepared_image

    with prepared_image(encoded(orientation=orientation), "image/png") as image:
        assert image.size == size
        assert [
            image.getpixel((x, y))[:3] for y in range(image.height) for x in range(image.width)
        ] == [COLORS[index] for index in indices]
        assert not image.getexif() and not image.info


@pytest.mark.parametrize("mode", ["RGBA", "P", "RGB", "L"])
def test_alpha_is_composited_on_white_without_losing_visible_pixels(mode):
    from coinpup_api.ocr.image_prepare import prepared_image
    from PIL import Image

    data = encoded(mode="RGBA")
    expected = [(255, 0, 0), (255, 255, 255), (127, 255, 127)]
    if mode == "P":
        with Image.new("P", (3, 2)) as indexed, BytesIO() as stream:
            indexed.putpalette([255, 0, 0, 0, 0, 0, 0, 255, 0] + [0] * (768 - 9))
            indexed.putdata([0, 1, 2] * 2)
            indexed.save(stream, "PNG", transparency=bytes([255, 0, 128]))
            data = stream.getvalue()
    elif mode in {"RGB", "L"}:
        values = [(255, 0, 0), (0, 0, 0), (0, 255, 0)] if mode == "RGB" else [100, 0, 200]
        with Image.new(mode, (3, 2)) as keyed, BytesIO() as stream:
            keyed.putdata(values * 2)
            keyed.save(stream, "PNG", transparency=values[1])
            data = stream.getvalue()
        expected = (
            [values[0], (255, 255, 255), values[2]]
            if mode == "RGB"
            else [(100,) * 3, (255,) * 3, (200,) * 3]
        )
    with prepared_image(data, "image/png") as image:
        assert [image.getpixel((x, 0))[:3] for x in range(3)] == expected


@pytest.mark.parametrize("format", ["JPEG", "PNG"])
@pytest.mark.parametrize(
    "field",
    [
        "max_side",
        "page_pixels",
        "document_pixels",
        "image_pixels",
        "document_image_pixels",
        "bitmap_bytes",
        "image_sample_bytes",
    ],
)
def test_header_dimensions_and_working_budget_precede_pixel_load(monkeypatch, format, field):
    from PIL import Image

    data, calls, opens, original, original_open = (
        encoded(format),
        [],
        [],
        Image.Image.load,
        Image.open,
    )

    def observe(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    def opened(*args, **kwargs):
        opens.append(True)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", observe)
    monkeypatch.setattr(Image, "open", opened)
    manual(prepare(data, MIMES[format], **{field: 1}), "prepare_limit")
    assert calls == [] and opens == []


def chunk(name, body):
    return name + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def riff(body):
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


@pytest.mark.parametrize(
    "case,reason",
    [
        ("canvas", "prepare_limit"),
        ("intrinsic", "prepare_limit"),
        ("mismatch", "invalid_image"),
        ("animation", "unsupported_image"),
        ("length", "invalid_image"),
        ("chunk_length", "invalid_image"),
        ("unknown", "unsupported_image"),
    ],
)
def test_malicious_webp_is_rejected_before_pillow_native_canvas(monkeypatch, case, reason):
    from PIL import Image

    data = encoded("WEBP")
    body = data[12:]
    if case in {"canvas", "mismatch", "animation"}:
        width, height = (16384, 16384) if case == "canvas" else (4 if case == "mismatch" else 3, 2)
        if case == "canvas":
            payload = body[8 : 8 + struct.unpack("<I", body[4:8])[0]]
            body = chunk(
                b"VP8L", payload[:1] + struct.pack("<I", 16383 | (16383 << 14)) + payload[5:]
            )
        extended = bytes([2 if case == "animation" else 0]) + b"\0" * 3
        extended += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        data = riff(chunk(b"VP8X", extended) + body)
    elif case == "intrinsic":
        assert body[:4] == b"VP8L" and body[8] == 47
        payload = body[8 : 8 + struct.unpack("<I", body[4:8])[0]]
        data = riff(
            chunk(b"VP8L", payload[:1] + struct.pack("<I", 16383 | (16383 << 14)) + payload[5:])
        )
    elif case == "length":
        data = data[:4] + struct.pack("<I", len(data)) + data[8:]
    elif case == "chunk_length":
        data = riff(body[:4] + struct.pack("<I", len(body) + 100) + body[8:])
    else:
        data = riff(chunk(b"XXXX", b"fictional") + body)
    calls, original = [], Image.open

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", observe)
    manual(prepare(data, "image/webp"), reason)
    assert calls == []


@pytest.mark.parametrize("field", ["working_bytes", "metadata_bytes", "max_chunks"])
@pytest.mark.parametrize("invalid", [True, 0, "above"])
def test_image_admission_budgets_have_strict_bounds(field, invalid):
    from coinpup_api.ocr.image_prepare import ImageLimits

    value = getattr(ImageLimits(), field) + 1 if invalid == "above" else invalid
    with pytest.raises(ValueError, match="Invalid image preparation limits"):
        replace(ImageLimits(), **{field: value})


@pytest.mark.parametrize("format", MIMES)
@pytest.mark.parametrize("field", ["working_bytes", "metadata_bytes", "max_chunks"])
def test_image_working_metadata_and_chunk_limits_precede_pillow(monkeypatch, format, field):
    from coinpup_api.ocr.image_prepare import ImageLimits
    from PIL import Image

    data, calls, original = encoded(format, orientation=1), [], Image.open

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", observe)
    manual(
        prepare(data, MIMES[format], image_limits=replace(ImageLimits(), **{field: 1})),
        "prepare_limit",
    )
    assert calls == []


@pytest.mark.parametrize("case", ["type", "empty", "large"])
def test_direct_source_data_is_bounded_before_decoder(monkeypatch, case):
    from PIL import Image

    data = (
        bytearray(b"fictional")
        if case == "type"
        else b""
        if case == "empty"
        else b"x" * (20 * 1024 * 1024 + 1)
    )
    calls, original = [], Image.open

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", observe)
    manual(prepare(data), "invalid_image")
    assert calls == []


@pytest.mark.parametrize(
    "case,reason",
    [("animation", "unsupported_image"), ("corrupt", "invalid_image"), ("mime", "invalid_image")],
)
def test_unsupported_frames_corruption_and_format_mismatch_are_manual(case, reason):
    data = (
        encoded(animated=True)
        if case == "animation"
        else b"fictional corrupt image"
        if case == "corrupt"
        else encoded("JPEG")
    )
    manual(prepare(data), reason)


@pytest.mark.parametrize("throws", [False, True])
def test_all_real_image_copies_and_source_stream_close_even_if_consumer_raises(monkeypatch, throws):
    from coinpup_api.ocr.image_prepare import prepared_image
    from PIL import Image

    data = encoded(mode="RGBA", orientation=6)
    streams, handles, original_open, original_close = [], {}, Image.open, Image.Image.close

    def opened(stream, *args, **kwargs):
        streams.append(stream)
        return original_open(stream, *args, **kwargs)

    def closed(self, *args, **kwargs):
        handles[id(self)] = self
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(Image, "open", opened)
    monkeypatch.setattr(Image.Image, "close", closed)

    def consume():
        with prepared_image(data, "image/png") as image:
            assert image.size == (2, 3) and image.getpixel((0, 0))
            if throws:
                raise RuntimeError("fictional consumer exception")

    if throws:
        with pytest.raises(RuntimeError, match="fictional consumer exception"):
            consume()
    else:
        consume()
    assert streams and all(stream.closed for stream in streams) and len(handles) >= 3
    for image in handles.values():
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def source_request(tmp_path, data, format="PNG"):
    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[format]
    path = tmp_path / f"{uuid4().hex}.{extension}"
    path.write_bytes(data)
    if sys.platform == "linux":
        tmp_path.chmod(0o700)
        path.chmod(0o600)
    return {
        "version": 1,
        "action": "prepare_image",
        "media_type": MIMES[format],
        "source": {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
        },
    }


def test_real_dispatcher_and_wire_budget_preserve_complete_results(tmp_path):
    data = encoded()
    request = source_request(tmp_path, data)
    expected = process(request, tmp_path)
    assert page(expected)["raster"]["width"] == 3
    request.update(image_limits={"working_bytes": 200}, prepare_limits={"bitmap_bytes": 24})
    assert process(request, tmp_path) == expected
    wire = json.dumps(expected, ensure_ascii=False, allow_nan=False).encode("utf8")
    assert prepare(data, output_bytes=len(wire)) == expected
    rejected = prepare(data, output_bytes=len(wire) - 1)
    assert rejected["pages"] == [] and rejected["reason_code"] == "prepare_limit"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("hash", "source_invalid"),
        ("size", "source_invalid"),
        ("name", "request_invalid"),
        ("mime", "request_invalid"),
        ("extra", "request_invalid"),
        ("limits", "request_invalid"),
        ("source_extra", "request_invalid"),
        ("unsupported_mime", "request_invalid"),
        ("media_shape", "request_invalid"),
        ("image_limits", "request_invalid"),
        ("prepare_limits", "request_invalid"),
    ],
)
def test_source_and_dispatcher_shapes_reject_without_leaking_input(tmp_path, case, reason):
    request = source_request(tmp_path, encoded())
    if case == "hash":
        request["source"]["sha256"] = "0" * 64
    elif case == "size":
        request["source"]["byte_size"] += 1
    elif case == "name":
        request["source"]["path"] = str(tmp_path / "fictional.png")
    elif case == "mime":
        request["media_type"] = "image/jpeg"
    elif case == "extra":
        request["untrusted"] = "fictional-secret"
    elif case == "limits":
        request["limits"] = {}
    elif case == "unsupported_mime":
        request["media_type"] = "image/gif"
    elif case == "media_shape":
        request["media_type"] = []
    elif case == "image_limits":
        request["image_limits"] = {"max_chunks": True}
    elif case == "prepare_limits":
        request["prepare_limits"] = {"output_bytes": 127}
    else:
        request["source"]["untrusted"] = "fictional-secret"
    result = process(request, tmp_path)
    assert result["reason_code"] == reason and result["pages"] == []
    assert "fictional-secret" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Requires real Linux isolated image preparation"
)
def test_real_bootstrap_decodes_an_image_under_os_limits(tmp_path):
    request = source_request(tmp_path, encoded("WEBP"), "WEBP")
    budget = ProcessBudget(
        wall_seconds=10,
        cpu_seconds=5,
        address_space_bytes=512 * 1024 * 1024,
        file_bytes=1024 * 1024,
        open_files=64,
        output_bytes=512 * 1024,
    )
    assert page(json.loads(run_isolated(request, budget).output))["raster"] == {
        "width": 3,
        "height": 2,
        "stride": 12,
        "format": "RGBX",
        "dpi": None,
    }
