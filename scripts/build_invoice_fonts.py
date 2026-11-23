"""Build full static Invoice fonts offline from separately acquired pinned Noto inputs."""

import argparse
import hashlib
import io
from pathlib import Path

from coinpup_api.business.invoice_fonts import (
    FONT_LICENSE,
    SOURCE_FONTS,
    STATIC_FONTS,
    checked_font_bytes,
)

FONTTOOLS_VERSION = "4.66.1"
SOURCE_DATE_EPOCH = 1791072000


def _rename(font, name):
    replacements = {
        1: name,
        2: "Regular",
        3: name + "-Regular-v1",
        4: name,
        6: name,
        16: name,
        17: "Regular",
        18: name,
        21: name,
        22: "Regular",
        25: name,
    }
    if "STAT" in font:
        del font["STAT"]
    font["name"].names = [
        record
        for record in font["name"].names
        if record.nameID not in replacements and record.nameID < 256
    ]
    for identifier, value in replacements.items():
        font["name"].setName(value, identifier, 3, 1, 0x409)
    font["head"].created = font["head"].modified = SOURCE_DATE_EPOCH + 2082844800
    font.recalcTimestamp = False


def build(source, output):
    """No corpus subsetting, network access, current clock, or existing-output replacement."""
    from fontTools import __version__
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    if __version__ != FONTTOOLS_VERSION:
        raise ValueError("Use the locked font build dependency version")
    directory = Path(output)
    if directory.exists() or directory.is_symlink():
        raise ValueError("The font output directory must be new")
    license_bytes = checked_font_bytes(source, FONT_LICENSE)
    inputs = [checked_font_bytes(source, asset) for asset in SOURCE_FONTS]
    outputs = []
    for content, expected in zip(inputs, STATIC_FONTS, strict=True):
        with TTFont(io.BytesIO(content), recalcTimestamp=False) as font:
            coverage = dict(font.getBestCmap())
            protected = [
                (r.nameID, r.string) for r in font["name"].names if r.nameID in (0, 13, 14)
            ]
            font = instantiateVariableFont(font, {"wght": 400}, inplace=True, static=True)
            _rename(font, Path(expected.filename).stem)
            if "fvar" in font or font.getBestCmap() != coverage:
                raise ValueError("Invoice font coverage changed")
            if protected != [
                (r.nameID, r.string) for r in font["name"].names if r.nameID in (0, 13, 14)
            ]:
                raise ValueError("Invoice font notices changed")
            buffer = io.BytesIO()
            font.save(buffer, reorderTables=True)
            built = buffer.getvalue()
        if len(built) != expected.byte_size or hashlib.sha256(built).hexdigest() != expected.sha256:
            raise ValueError("Invoice font build differs from the pinned version")
        outputs.append((expected.filename, built))
    # All expensive validation finishes first. mkdir and each file use exclusive creation.
    directory.mkdir()
    for name, content in outputs + [(FONT_LICENSE.filename, license_bytes)]:
        with (directory / name).open("xb") as stream:
            stream.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
    print("Both complete static Invoice fonts and license match the pinned hashes.")


if __name__ == "__main__":
    main()
