"""Private, fixed dispatcher. Imported after process limits, not by the API."""

import hashlib
import os
import re
import stat
import sys
from pathlib import Path

from .pdf_probe import MAX_SOURCE_BYTES, ProbeLimits, manual_result


def _request(request):
    if type(request) is not dict or not {"version", "action", "source"} <= request.keys():
        raise ValueError
    image = request.get("action") == "prepare_image"
    prepare = request.get("action") in ("prepare_pdf", "prepare_image")
    allowed = {"version", "action", "source"} | (
        {"media_type", "prepare_limits", "image_limits"}
        if image
        else {"limits"} | ({"prepare_limits"} if prepare else set())
    )
    if request.keys() - allowed:
        raise ValueError
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValueError
    if request["action"] not in ("inspect_pdf", "prepare_pdf", "prepare_image"):
        raise ValueError
    source, options = request["source"], request.get("limits", {})
    if type(source) is not dict or source.keys() != {"path", "sha256", "byte_size"}:
        raise ValueError
    if type(options) is not dict:
        raise ValueError
    limits = ProbeLimits(**options)
    preparation, image_limits = None, None
    if prepare:
        from .pdf_prepare import PrepareLimits

        options = request.get("prepare_limits", {})
        if type(options) is not dict:
            raise ValueError
        preparation = PrepareLimits(**options)
    suffix = "pdf"
    if image:
        from .image_prepare import ImageLimits

        suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(
            request.get("media_type")
        )
        options = request.get("image_limits", {})
        if suffix is None or type(options) is not dict:
            raise ValueError
        image_limits = ImageLimits(**options)
    if type(source["path"]) is not str or type(source["sha256"]) is not str:
        raise ValueError
    path = Path(source["path"])
    if not path.is_absolute() or re.fullmatch(r"[0-9a-f]{32}\." + suffix, path.name) is None:
        raise ValueError
    if re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None:
        raise ValueError
    if type(source["byte_size"]) is not int or not 1 <= source["byte_size"] <= MAX_SOURCE_BYTES:
        raise ValueError
    return source, path, limits, preparation, image_limits


def _read_source(source, path):
    # All path components must already refer to the trusted private staging location.
    if path.resolve(strict=True) != path or path.is_symlink():
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if sys.platform == "linux":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(directory)
            if parent.st_mode & 0o077 or parent.st_uid != os.getuid():
                raise ValueError
            descriptor = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != source["byte_size"]:
            raise ValueError
        if sys.platform == "linux" and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
            raise ValueError
        data = stream.read(source["byte_size"] + 1)
        after = os.fstat(stream.fileno())
        if len(data) != source["byte_size"] or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError
    return data


def process(request: dict, workdir: Path) -> dict:
    """No database, OCR engine, subprocess command or external entry selection."""
    try:
        source, path, limits, preparation, image_limits = _request(request)
    except (ValueError, TypeError, OSError, OverflowError):
        return manual_result("request_invalid")
    try:
        data = _read_source(source, path)
    except (ValueError, OSError, RuntimeError):
        return manual_result("source_invalid")
    if image_limits is not None:
        from .image_prepare import prepare_image

        return prepare_image(data, request["media_type"], preparation, image_limits)
    if preparation is not None:
        from .pdf_prepare import prepare_pdf

        return prepare_pdf(data, preparation, limits)
    from .pdf_probe import probe_pdf

    return probe_pdf(data, limits)
