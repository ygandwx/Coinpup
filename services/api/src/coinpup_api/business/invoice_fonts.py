"""Read the fixed commercial-invoice-v1 fonts; never fetch or substitute host fonts."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from coinpup_api.ledger.errors import MoneyError


@dataclass(frozen=True, slots=True)
class FontAsset:
    filename: str
    byte_size: int
    sha256: str


SOURCE_REVISION = "523d033d6cb47f4a80c58a35753646f5c3608a78"
SOURCE_FONTS = (
    FontAsset(
        "NotoSansSC-VF.ttf",
        17773132,
        "d68bafcb48a2707749396aa12bbbd833cb70401f3a9a689fd2902c7e0d295964",
    ),
    FontAsset(
        "NotoSansTC-VF.ttf",
        11942800,
        "ac091cc8cd19e848202afc8fe6d3809b4526c8fdbdb4be82da20c4f785949591",
    ),
)
STATIC_FONTS = (
    FontAsset(
        "CoinpupInvoiceSCV1.ttf",
        10596068,
        "11ea1da09ae36720199dda9023c64ac59c5b893368c3e3190fbf91b9276147b4",
    ),
    FontAsset(
        "CoinpupInvoiceTCV1.ttf",
        7149316,
        "a5df4a6b1935a78e5d15cf010d24b13a6a7f66e33aedfebe1994f3c5ee27a85e",
    ),
)
FONT_LICENSE = FontAsset(
    "LICENSE", 4301, "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
)


def _unavailable():
    return MoneyError("invoice_fonts_unavailable", "The fixed Invoice fonts are unavailable.")


def checked_font_bytes(directory, asset):
    """Return bounded verified bytes, not a path which can change after validation."""
    descriptor = None
    try:
        path = Path(directory) / asset.filename
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != asset.byte_size:
            raise _unavailable()
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or opened.st_size != asset.byte_size
        ):
            raise _unavailable()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(asset.byte_size + 1)
        if len(content) != asset.byte_size or hashlib.sha256(content).hexdigest() != asset.sha256:
            raise _unavailable()
        return content
    except (OSError, TypeError, ValueError):
        raise _unavailable() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_invoice_fonts(directory):
    """Require the license and both pinned complete fonts before rendering can begin."""
    checked_font_bytes(directory, FONT_LICENSE)
    return tuple(checked_font_bytes(directory, asset) for asset in STATIC_FONTS)
