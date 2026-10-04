"""Explicit font acquisition and offline, reproducible corpus-only font builds."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

ASSETS_PATH = Path(__file__).resolve().parents[2] / "tests/fixtures/ocr/font-assets.json"
TRUTH_PATH = ASSETS_PATH.with_name("truth.json")
SOURCE_DATE_EPOCH = 1791072000  # 2026-10-04 00:00:00 UTC, not the build clock.
DEV_CHARS = "虚构开发样本繁體測試收據獨立發票金額日期幣種合計小計稅額"
_NAMES = {"SC": "NotoSansSC-VF.ttf", "TC": "NotoSansTC-VF.ttf", "license": "LICENSE"}


class FontBuildError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise FontBuildError(code) from None


def _manifest():
    manifest = json.loads(ASSETS_PATH.read_text(encoding="utf8"))
    if (
        manifest["version"] != 1
        or len(manifest["files"]) != 3
        or {item["role"]: item["name"] for item in manifest["files"]} != _NAMES
    ):
        _fail("font_source_invalid")
    return manifest


def _verify(path, item):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item["byte_size"]:
        _fail("font_source_invalid")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != item["sha256"]:
        _fail("font_source_invalid")


def verify_sources(source_dir):
    """Verify all exact source bytes, including the license, without importing fontTools."""
    try:
        manifest = _manifest()
        for item in manifest["files"]:
            _verify(Path(source_dir) / item["name"], item)
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        _fail("font_source_invalid")


def fetch_sources(source_dir):
    """Network is opt-in. An existing incorrect cache file is never overwritten."""
    import urllib.request

    temporary = None
    try:
        manifest = _manifest()
        directory = Path(source_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for item in manifest["files"]:
            target = directory / item["name"]
            if target.exists() or target.is_symlink():
                _verify(target, item)
                continue
            # URLs come only from the committed asset manifest, never a CLI URL.
            if not item["url"].startswith(
                "https://raw.githubusercontent.com/notofonts/noto-cjk/"
                + manifest["source_revision"]
                + "/"
            ):
                _fail("font_source_invalid")
            descriptor, temporary = tempfile.mkstemp(prefix=".font-", dir=directory)
            with os.fdopen(descriptor, "wb") as output:
                with urllib.request.urlopen(item["url"], timeout=60) as response:
                    remaining = item["byte_size"]
                    while data := response.read(min(65536, remaining + 1)):
                        remaining -= len(data)
                        if remaining < 0:
                            _fail("font_source_invalid")
                        output.write(data)
            _verify(Path(temporary), item)
            # link publishes without replacing a concurrently created cache entry.
            os.link(temporary, target)
            Path(temporary).unlink()
            temporary = None
        return verify_sources(directory)
    except (OSError, ValueError, KeyError, TypeError):
        _fail("font_fetch_failed")
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _characters(characters):
    result = set(chr(code) for code in range(32, 127)) | set(DEV_CHARS)
    try:
        for character in characters:
            if (
                type(character) is not str
                or len(character) != 1
                or not character.isprintable()
                or len(result) >= 4096
            ):
                _fail("font_input_invalid")
            result.add(character)
    except TypeError:
        _fail("font_input_invalid")
    return sorted(map(ord, result))


def truth_characters(truth_path=TRUTH_PATH):
    """Only printed lines and raw row cells; canonical expected values are not a source."""
    truth = json.loads(Path(truth_path).read_text(encoding="utf8"))
    strings = []
    for template in truth["templates"]:
        for page in template["pages"]:
            strings.extend(page["lines"])
            for row in page["rows"]:
                strings.extend(
                    row[key] for key in ("date", "currency", "raw_amount", "description")
                )
    return set("".join(strings))


def _cmap(font):
    return {
        code: name
        for code, name in (font.getBestCmap() or {}).items()
        if font.getGlyphID(name) != 0
    }


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
    # The fully static subset has no named variation instances or shaping features.
    # Preserve author/license records; discard their now-unused variation names.
    if "STAT" in font:
        del font["STAT"]
    font["name"].names = [
        record
        for record in font["name"].names
        if record.nameID not in replacements and record.nameID < 256
    ]
    for name_id, value in replacements.items():
        font["name"].setName(value, name_id, 3, 1, 0x409)
    font["head"].created = font["head"].modified = SOURCE_DATE_EPOCH + 2082844800
    font.recalcTimestamp = False


def prepare_fonts(source_dir, output_dir, characters):
    """Build static, covered subsets offline; returned filenames are output-relative."""
    manifest = verify_sources(source_dir)
    codepoints = _characters(characters)
    directory = Path(output_dir)
    records, covered = [], set()
    try:
        from fontTools import __version__, subset
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables.otBase import USE_HARFBUZZ_REPACKER
        from fontTools.varLib.instancer import instantiateVariableFont

        if directory.is_symlink():
            _fail("font_input_invalid")
        directory.mkdir(parents=True, exist_ok=True)
        for filename in ("CoinpupCorpusSC.ttf", "CoinpupCorpusTC.ttf", "LICENSE", "fonts.json"):
            if (directory / filename).is_symlink():
                _fail("font_input_invalid")
        for item in manifest["files"]:
            if item["role"] == "license":
                continue
            with TTFont(Path(source_dir) / item["name"], recalcTimestamp=False) as font:
                font.cfg[USE_HARFBUZZ_REPACKER] = False
                selected = sorted(set(codepoints) & _cmap(font).keys())
                covered.update(selected)
                protected = [
                    (
                        record.nameID,
                        record.platformID,
                        record.platEncID,
                        record.langID,
                        record.string,
                    )
                    for record in font["name"].names
                    if record.nameID in (0, 13, 14)
                ]
                if (
                    "glyf" not in font
                    or "fvar" not in font
                    or "wght" not in {axis.axisTag for axis in font["fvar"].axes}
                ):
                    _fail("font_build_failed")
                axes = {
                    axis.axisTag: 400 if axis.axisTag == "wght" else axis.defaultValue
                    for axis in font["fvar"].axes
                }
                options = subset.Options()
                options.harfbuzz_repacker = False
                options.layout_features = []
                options.name_IDs = ["*"]
                options.name_languages = ["*"]
                options.name_legacy = True
                # Subset before instancing to avoid computing unused CJK variations.
                subsetter = subset.Subsetter(options=options)
                subsetter.populate(unicodes=selected)
                subsetter.subset(font)
                instantiateVariableFont(font, axes, inplace=True, static=True)
                name = "CoinpupCorpus" + item["role"]
                _rename(font, name)
                filename = name + ".ttf"
                font.save(directory / filename, reorderTables=True)
            with TTFont(directory / filename, recalcTimestamp=False) as built:
                actual = sorted(_cmap(built))
                retained = [
                    (
                        record.nameID,
                        record.platformID,
                        record.platEncID,
                        record.langID,
                        record.string,
                    )
                    for record in built["name"].names
                    if record.nameID in (0, 13, 14)
                ]
                if "fvar" in built or actual != selected or sorted(protected) != sorted(retained):
                    _fail("font_build_failed")
            data = (directory / filename).read_bytes()
            records.append(
                {
                    "role": item["role"],
                    "name": name,
                    "filename": filename,
                    "source_sha256": item["sha256"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data),
                    "codepoints": actual,
                }
            )
        if set(codepoints) - covered:
            _fail("font_coverage")
        license_item = next(item for item in manifest["files"] if item["role"] == "license")
        (directory / "LICENSE").write_bytes((Path(source_dir) / "LICENSE").read_bytes())
        result = {
            "version": 1,
            "source_revision": manifest["source_revision"],
            "fonttools_version": __version__,
            "source_date_epoch": SOURCE_DATE_EPOCH,
            "character_sha256": hashlib.sha256(
                "".join(map(chr, codepoints)).encode("utf8")
            ).hexdigest(),
            "fonts": records,
            "license": {
                "filename": "LICENSE",
                "sha256": license_item["sha256"],
                "byte_size": license_item["byte_size"],
            },
        }
        (directory / "fonts.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf8",
            newline="\n",
        )
        return result
    except FontBuildError:
        raise
    except Exception:
        _fail("font_build_failed")


def font_for(text, primary, metadata):
    """Choose a whole-line font only with complete glyph coverage; never glyph substitution."""
    if type(text) is not str or primary not in ("SC", "TC"):
        _fail("font_input_invalid")
    wanted = set(map(ord, text))
    for role in dict.fromkeys((primary, "SC")):
        for record in metadata["fonts"]:
            if record["role"] == role and wanted <= set(record["codepoints"]):
                return record
    _fail("font_coverage")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Explicitly download the pinned font sources")
    fetch.add_argument("--source-dir", type=Path, required=True)
    build = commands.add_parser("build", help="Verify and build offline")
    build.add_argument("--source-dir", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--truth", type=Path, default=TRUTH_PATH)
    args = parser.parse_args()
    try:
        result = (
            fetch_sources(args.source_dir)
            if args.command == "fetch"
            else prepare_fonts(args.source_dir, args.output_dir, truth_characters(args.truth))
        )
    except (OSError, ValueError, KeyError, TypeError, FontBuildError) as error:
        parser.exit(
            1, (error.code if isinstance(error, FontBuildError) else "font_input_invalid") + "\n"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
