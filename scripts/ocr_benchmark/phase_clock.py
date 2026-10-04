"""Bounded raw phase boundaries; incomplete phases survive a killed candidate process."""

import json
import os
import re
from pathlib import Path

PHASES = frozenset(
    {
        "model_verify",
        "engine_init",
        "source",
        "prepare_total",
        "rgb_materialize",
        "sdk_recognize",
        "parse",
        "pdf_render",
        "image_decode",
    }
)


class PhaseClockError(RuntimeError):
    def __init__(self):
        super().__init__("phase_clock_invalid")


class PhaseJournal:
    def __init__(self, path):
        self._records, self._descriptor, self.failed = [], None, False
        try:
            path = Path(path)
            if path.parent.resolve(strict=True) != path.parent:
                raise ValueError
            self._descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644
            )
            if hasattr(os, "fchmod"):
                os.fchmod(self._descriptor, 0o644)
            else:
                os.chmod(path, 0o644)
        except Exception:
            self.close()
            raise PhaseClockError() from None

    @property
    def events(self):
        return [record | {"details": dict(record["details"])} for record in self._records]

    def __call__(self, name, page_index, edge, at_ns, details=None):
        try:
            if self.failed or self._descriptor is None or len(self._records) >= 500:
                raise ValueError
            if name not in PHASES or edge not in ("start", "end"):
                raise ValueError
            if page_index is not None and (type(page_index) is not int or not 0 <= page_index < 50):
                raise ValueError
            if (
                type(at_ns) is not int
                or not 0 <= at_ns < 2**63
                or (self._records and at_ns < self._records[-1]["at_ns"])
            ):
                raise ValueError
            details = {} if details is None else details
            if type(details) is not dict:
                raise ValueError
            if edge == "end":
                if details.keys() != {"outcome"} or details["outcome"] not in ("passed", "failed"):
                    raise ValueError
            elif name == "sdk_recognize":
                if (
                    details.keys() != {"raster_rgb_sha256", "width", "height"}
                    or type(details["raster_rgb_sha256"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", details["raster_rgb_sha256"]) is None
                ):
                    raise ValueError
                width, height = details["width"], details["height"]
                if (
                    any(
                        type(value) is not int or not 1 <= value <= 10000
                        for value in (width, height)
                    )
                    or width * height > 20000000
                ):
                    raise ValueError
            elif details:
                raise ValueError
            record = {
                "phase": name,
                "page_index": page_index,
                "edge": edge,
                "at_ns": at_ns,
                "details": dict(details),
            }
            packet = (json.dumps(record, allow_nan=False) + "\n").encode("utf8")
            while packet:
                written = os.write(self._descriptor, packet)
                if written <= 0:
                    raise OSError
                packet = packet[written:]
            self._records.append(record)
        except Exception:
            self.failed = True
            raise PhaseClockError() from None

    def close(self):
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
