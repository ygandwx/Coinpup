"""The renderer must not silently replace missing, corrupt or redirected font assets."""

import hashlib
import os
from dataclasses import FrozenInstanceError

import pytest
from coinpup_api.business import invoice_fonts as fonts
from coinpup_api.ledger.errors import MoneyError


@pytest.fixture
def assets(tmp_path, monkeypatch):
    # Byte-reader negative tests use tiny pins; actual full-font builds are checked separately.
    records = []
    for name, value in (("SC.ttf", b"fixed-sc"), ("TC.ttf", b"fixed-tc"), ("LICENSE", b"notice")):
        record = fonts.FontAsset(name, len(value), hashlib.sha256(value).hexdigest())
        (tmp_path / name).write_bytes(value)
        records.append(record)
    monkeypatch.setattr(fonts, "STATIC_FONTS", tuple(records[:2]))
    monkeypatch.setattr(fonts, "FONT_LICENSE", records[2])
    return tmp_path


def unavailable(path):
    with pytest.raises(MoneyError) as caught:
        fonts.load_invoice_fonts(path)
    assert caught.value.code == "invoice_fonts_unavailable"
    assert str(path) not in str(caught.value)


def test_verified_bytes_are_detached_from_later_disk_changes(assets):
    loaded = fonts.load_invoice_fonts(assets)
    assert loaded == (b"fixed-sc", b"fixed-tc")
    (assets / "SC.ttf").write_bytes(b"modified")
    unavailable(assets)
    assert loaded == (b"fixed-sc", b"fixed-tc")


@pytest.mark.parametrize("name", ["SC.ttf", "TC.ttf", "LICENSE"])
@pytest.mark.parametrize(
    "damage", ["missing", "shorter", "longer", "same-size", "directory", "link"]
)
def test_entire_pair_and_license_are_required_without_system_font_fallback(assets, name, damage):
    path = assets / name
    original = path.read_bytes()
    path.unlink()
    if damage == "shorter":
        path.write_bytes(original[:-1])
    elif damage == "longer":
        path.write_bytes(original + b"x")
    elif damage == "same-size":
        path.write_bytes(b"x" * len(original))
    elif damage == "directory":
        path.mkdir()
    elif damage == "link":
        other = assets / "redirected"
        other.write_bytes(original)
        path.symlink_to(other)
    unavailable(assets)


def test_fifo_cannot_block_a_render_process(assets):
    path = assets / "SC.ttf"
    path.unlink()
    os.mkfifo(path)
    unavailable(assets)


def test_replacement_between_stat_and_open_is_rejected(assets, monkeypatch):
    original_open = os.open

    def replace(path, flags):
        # Same bytes and size but a new inode during the validation window.
        if path.name == "SC.ttf":
            old = path.with_suffix(".old")
            path.rename(old)
            path.write_bytes(old.read_bytes())
        return original_open(path, flags)

    monkeypatch.setattr(os, "open", replace)
    unavailable(assets)


def test_template_font_pins_are_immutable():
    with pytest.raises(FrozenInstanceError):
        fonts.STATIC_FONTS[0].filename = "arbitrary.ttf"
    assert len(fonts.SOURCE_FONTS) == len(fonts.STATIC_FONTS) == 2
    assert fonts.SOURCE_REVISION == "523d033d6cb47f4a80c58a35753646f5c3608a78"
