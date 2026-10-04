"""Offline fictional corpus construction. Never invokes recognition or field parsing."""

import argparse
import hashlib
import json
import math
import os
import platform
import re
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

from .fonts import SOURCE_DATE_EPOCH, TRUTH_PATH, font_for, prepare_fonts

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = TRUTH_PATH.with_name("corpus-config.json")
DEVELOPMENT_PATH = TRUTH_PATH.with_name("development.json")
_ROW_KEYS = ("date", "currency", "raw_amount", "description")


class CorpusError(Exception):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def _file(path, root):
    data = path.read_bytes()
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": _sha(data),
        "byte_size": len(data),
    }


def _printed(template):
    return [
        list(page["lines"]) + [" | ".join(row[key] for key in _ROW_KEYS) for row in page["rows"]]
        for page in template["pages"]
    ]


def _edited(template, edits):
    result = deepcopy(template)
    for edit in edits:
        lines = result["pages"][edit["page"]]["lines"]
        if lines.count(edit["match"]) != 1:
            raise CorpusError("literal_edit_invalid")
        index = lines.index(edit["match"])
        if "replace" in edit:
            lines[index] = edit["replace"]
        else:
            lines.insert(index + 1, edit["append"])
    return result


@contextmanager
def _epoch():
    old = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = str(SOURCE_DATE_EPOCH)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = old


def _text_pdf(template, fonts, config):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas

    primary = "TC" if template["language"] == "zh-Hant" else "SC"
    size, margin = config["font_size"], config["margin"]
    width, height = config["page_size"]
    with BytesIO() as stream, _epoch():
        canvas = Canvas(stream, pagesize=(width, height), invariant=1, pageCompression=1)
        canvas.setTitle("FICTIONAL TEST DOCUMENT")
        canvas.setAuthor("Coinpup fictional corpus")

        def draw(text, x, y, align="left", bounds=None):
            name = font_for(text, primary, fonts)["name"]
            length = pdfmetrics.stringWidth(text, name, size)
            left = x - (length if align == "right" else length / 2 if align == "center" else 0)
            low, high = bounds or (margin, width - margin)
            if not (low <= left and left + length <= high and margin <= y <= height - margin):
                raise CorpusError("layout_overflow")
            canvas.setFont(name, size)
            canvas.drawString(left, y, text, shaping=False)

        for page in template["pages"]:
            y = config["first_baseline"]
            for index, text in enumerate(page["lines"]):
                if page["rows"] and index == len(page["lines"]) - 1:
                    titles = [title.strip() for title in text.split("|")]
                    if len(titles) != 4:
                        raise CorpusError("layout_invalid")
                    for title, x in zip(titles, config["column_centers"], strict=True):
                        draw(title, x, y, "center")
                    for x in config["separators"]:
                        draw("|", x, y, "center")
                else:
                    draw(text, margin, y)
                y -= config["line_step"]
            boundaries = [margin, *config["separators"], width - margin]
            for row in page["rows"]:
                for index, (key, align) in enumerate(
                    zip(_ROW_KEYS, ("left", "center", "right", "left"), strict=True)
                ):
                    draw(
                        row[key],
                        config["row_positions"][index],
                        y,
                        align,
                        (boundaries[index] + 2, boundaries[index + 1] - 2),
                    )
                for x in config["separators"]:
                    draw("|", x, y, "center")
                y -= config["line_step"]
            canvas.showPage()
        canvas.save()
        return stream.getvalue()


@contextmanager
def _rasters(data):
    import pypdfium2

    with ExitStack() as stack, pypdfium2.PdfDocument(data) as document:
        images = []
        for index in range(len(document)):
            page = document[index]
            try:
                # Build-only rasterization of our own text PDF is not runtime OCR routing.
                scale = 300 / 72
                width, height = (math.ceil(value * scale) for value in page.get_size())
                if width * height > 20000000 or max(width, height) > 10000:
                    raise CorpusError("raster_limit")
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil() as view:
                        image = view.convert("RGB")
                        stack.callback(image.close)
                        images.append(image)
                finally:
                    bitmap.close()
            finally:
                page.close()
        yield images


def _jpeg(image, config):
    with BytesIO() as stream:
        image.save(stream, format="JPEG", **config["jpeg"], exif=b"", icc_profile=None)
        return stream.getvalue()


def _scan_pdf(jpegs, dimensions=(420, 595)):
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        EncodedStreamObject,
        NameObject,
        NumberObject,
    )

    with PdfWriter() as writer, BytesIO() as stream:
        writer.add_metadata({"/Title": "FICTIONAL TEST DOCUMENT"})
        for data in jpegs:
            with BytesIO(data) as image_stream, Image.open(image_stream) as image:
                width, height = image.size
            image_object = EncodedStreamObject()
            image_object._data = data
            image_object.update(
                {
                    NameObject(key): value
                    for key, value in {
                        "/Type": NameObject("/XObject"),
                        "/Subtype": NameObject("/Image"),
                        "/Width": NumberObject(width),
                        "/Height": NumberObject(height),
                        "/ColorSpace": NameObject("/DeviceRGB"),
                        "/BitsPerComponent": NumberObject(8),
                        "/Filter": NameObject("/DCTDecode"),
                    }.items()
                }
            )
            page = writer.add_blank_page(*dimensions)
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im0"): writer._add_object(image_object)}
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(
                f"q {dimensions[0]} 0 0 {dimensions[1]} 0 0 cm /Im0 Do Q\n".encode("ascii")
            )
            page[NameObject("/Contents")] = writer._add_object(content)
        writer.write(stream)
        return stream.getvalue()


def _montage(images):
    from PIL import Image

    result = Image.new(
        "RGB",
        (max(image.width for image in images), sum(image.height for image in images)),
        "white",
    )
    offset = 0
    for image in images:
        result.paste(image, (0, offset))
        offset += image.height
    return result


def _perspective(image, specification):
    from PIL import Image

    width, height = image.size
    px, py = (
        math.ceil(width * specification["padding"]),
        math.ceil(height * specification["padding"]),
    )
    ix, iy = width * specification["inset_x"], height * specification["inset_y"]
    target = [
        (px + ix, py + iy),
        (px + width - ix, py),
        (px + width, py + height - iy),
        (px, py + height),
    ]
    source = [(0, 0), (width, 0), (width, height), (0, height)]
    matrix = []
    for (x, y), (u, v) in zip(target, source, strict=True):
        matrix.extend(
            ([x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v])
        )
    for column in range(8):
        pivot = max(range(column, 8), key=lambda row: abs(matrix[row][column]))
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        divisor = matrix[column][column]
        matrix[column] = [value / divisor for value in matrix[column]]
        for row in range(8):
            if row != column:
                factor = matrix[row][column]
                matrix[row] = [
                    a - factor * b for a, b in zip(matrix[row], matrix[column], strict=True)
                ]
    return image.transform(
        (width + 2 * px, height + 2 * py),
        Image.Transform.PERSPECTIVE,
        [row[-1] for row in matrix],
        resample=Image.Resampling.BICUBIC,
        fillcolor="white",
    )


def _degrade(image, specification):
    from PIL import Image, ImageEnhance, ImageFilter

    match specification["kind"]:
        case "rotate":
            return image.rotate(
                specification["angle"],
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor="white",
            )
        case "contrast":
            return ImageEnhance.Contrast(image).enhance(specification["factor"])
        case "blur":
            return image.filter(ImageFilter.GaussianBlur(specification["radius"]))
        case "perspective":
            return _perspective(image, specification)
        case _:
            raise CorpusError("perturbation_invalid")


def _reference(template, printed, carrier, specification=None):
    photo = carrier == "photo"
    fields = {"header." + key: value for key, value in template["header"].items()}
    pages, rows, cumulative = [], [], 0
    for source_page, page in enumerate(template["pages"]):
        physical = 0 if photo else source_page
        pages.append(
            {
                "source_page": source_page,
                "page": physical,
                "offset": [0, source_page * 2480 if photo else 0],
                # PDFium maps the page to these integer destination dimensions.
                "scale": [[1751, 420], [2480, 595]] if photo else [[1, 1], [1, 1]],
            }
        )
        for source_row, row in enumerate(page["rows"]):
            ordinal = cumulative + source_row if photo else source_row
            rows.append(
                {
                    "source_page": source_page,
                    "source_row": source_row,
                    "page": physical,
                    "table": 0,
                    "row": ordinal,
                }
            )
            for key in ("date", "currency", "amount"):
                fields[f"rows.{physical}.0.{ordinal}.{key}"] = row[key]
        cumulative += len(page["rows"])
    return {
        "reference_text": "\n\n".join("\n".join(lines) for lines in _printed(printed)),
        "expected_fields": fields,
        "expected_review_paths": [
            edit["review_path"] for edit in (specification or {}).get("literal_edits", [])
        ],
        "source_mapping": {
            "units": "px" if photo else "pt",
            "pages": pages,
            "rows": rows,
            "space": "before_transform" if specification else "carrier",
            "transform": specification,
        },
    }


def _case(
    root, identifier, group, carrier, data, template, printed, specification=None, *, decode=True
):
    from PIL import Image

    suffix, media = ("jpg", "image/jpeg") if carrier == "photo" else ("pdf", "application/pdf")
    path = root / (identifier + "." + suffix)
    path.write_bytes(data)
    hashes = None
    dimensions = [[420, 595] for _ in printed["pages"]]
    if decode and carrier == "photo":
        with BytesIO(data) as stream, Image.open(stream) as image, image.convert("RGB") as rgb:
            dimensions, hashes = [list(rgb.size)], [_sha(rgb.tobytes())]
    elif decode:
        with _rasters(data) as images:
            hashes = [_sha(image.tobytes()) for image in images]
    return {
        "id": identifier,
        "group": group,
        "carrier": carrier,
        "template_id": template["id"],
        **_file(path, root),
        "media_type": media,
        "page_count": len(dimensions),
        "dimensions": dimensions,
        "dimensions_unit": "px" if carrier == "photo" else "pt",
        "raster_sha256": hashes,
        "raster_mode": "RGB",
        "raster_dpi": 300,
        "perturbation": specification,
        "runtime_scenario": None,
        **_reference(template, printed, carrier, specification),
    }


def _error_pdf(kind, scan):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    if kind == "damaged":
        return b"%PDF-1.7\n% FICTIONAL DAMAGED TEST DOCUMENT\n"
    with PdfWriter() as writer, BytesIO() as output:
        writer.add_metadata({"/Title": "FICTIONAL ERROR TEST DOCUMENT"})
        if kind in ("encrypted", "pixel_limit"):
            with BytesIO(scan) as stream:
                writer.append(PdfReader(stream))
            if kind == "encrypted":
                writer.encrypt("fictional-test-only", algorithm="RC4-128")
            else:
                writer.pages[0].mediabox.upper_right = (2000, 2000)
        else:
            for _ in range(51 if kind == "page_limit" else 1):
                page = writer.add_blank_page(420, 595)
                content = DecodedStreamObject()
                if kind == "stream_limit":
                    content.set_data(b" " * (8388608 + 1))
                    content = content.flate_encode()
                else:
                    content.set_data(
                        b"BT /F1 11 Tf 20 555 Td "
                        + (b"<FF00FF>" if kind == "garbled_layer" else b"(FICTIONAL TEST DOCUMENT)")
                        + b" Tj ET"
                    )
                    page[NameObject("/Resources")] = DictionaryObject(
                        {
                            NameObject("/Font"): DictionaryObject(
                                {
                                    NameObject("/F1"): DictionaryObject(
                                        {
                                            NameObject("/Type"): NameObject("/Font"),
                                            NameObject("/Subtype"): NameObject("/Type1"),
                                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                                        }
                                    )
                                }
                            )
                        }
                    )
                page[NameObject("/Contents")] = writer._add_object(content)
        writer.write(output)
        return output.getvalue()


def generate_corpus(source_dir, output_dir, *, truth_path=TRUTH_PATH, config_path=CONFIG_PATH):
    """Generate a new offline directory; manifest is published only after all assets exist."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    truth, development, config = _json(truth_path), _json(DEVELOPMENT_PATH), _json(config_path)
    if config["page_size"] != [420, 595] or config["dpi"] != 300 or config["seed"] != 20261004:
        raise CorpusError("configuration_invalid")
    templates = {template["id"]: template for template in truth["templates"]}
    all_templates = [*templates.values(), *development["templates"]]
    if any(not re.fullmatch(r"[A-Z][0-9]{2}", template["id"]) for template in all_templates):
        raise CorpusError("template_invalid")
    modified = [
        _edited(templates[spec["template"]], spec.get("literal_edits", []))
        for spec in config["degraded"]
    ]
    characters = set(
        "".join(
            line
            for template in [*all_templates, *modified]
            for page in _printed(template)
            for line in page
        )
    )
    root = Path(output_dir)
    if root.exists() or root.is_symlink():
        raise CorpusError("output_exists")
    root.mkdir(parents=True)
    fonts = prepare_fonts(source_dir, root / "fonts", characters)
    for font in fonts["fonts"]:
        pdfmetrics.registerFont(TTFont(font["name"], root / "fonts" / font["filename"]))
    cases, dev_cases = [], []
    for template in all_templates:
        pdf = _text_pdf(template, fonts, config)
        official = template["id"] in templates
        with _rasters(pdf) as images:
            scan = _scan_pdf([_jpeg(image, config) for image in images])
            if official:
                cases.append(
                    _case(
                        root, template["id"] + "-text", "text", "text_pdf", pdf, template, template
                    )
                )
                with _montage(images) as photo:
                    cases.append(
                        _case(
                            root,
                            template["id"] + "-photo",
                            "ocr",
                            "photo",
                            _jpeg(photo, config),
                            template,
                            template,
                        )
                    )
            target = cases if official else dev_cases
            target.append(
                _case(
                    root,
                    template["id"] + "-scan",
                    "ocr" if official else "development",
                    "scan_pdf",
                    scan,
                    template,
                    template,
                )
            )
    for specification, printed in zip(config["degraded"], modified, strict=True):
        template = templates[specification["template"]]
        with (
            _rasters(_text_pdf(printed, fonts, config)) as images,
            _montage(images) as photo,
            _degrade(photo, specification) as degraded,
        ):
            cases.append(
                _case(
                    root,
                    template["id"] + "-" + specification["kind"],
                    "degraded",
                    "photo",
                    _jpeg(degraded, config),
                    template,
                    printed,
                    specification,
                )
            )
    base = next(case for case in cases if case["id"] == "S01-scan")
    for kind in config["errors"]:
        if kind in ("missing_model", "timeout"):
            case = deepcopy(base)
            case.update(
                id="error-" + kind,
                group="error",
                runtime_scenario={"kind": kind, "execution_status": "not_executed"},
            )
        else:
            data = _error_pdf(kind, (root / base["relative_path"]).read_bytes())
            case = _case(
                root,
                "error-" + kind,
                "error",
                "text_pdf"
                if kind in ("garbled_layer", "page_limit", "stream_limit")
                else "scan_pdf",
                data,
                templates["S01"],
                templates["S01"],
                decode=False,
            )
            case.update(
                reference_text="",
                expected_fields={},
                expected_review_paths=[],
                source_mapping={
                    "units": "pt",
                    "pages": [],
                    "rows": [],
                    "space": "carrier",
                    "transform": None,
                },
                page_count=51 if kind == "page_limit" else None,
                dimensions=[],
            )
            case["error_kind"] = kind
        cases.append(case)
    result = {
        "version": 1,
        "seed": config["seed"],
        "truth_sha256": _sha(Path(truth_path).read_bytes()),
        "configuration_sha256": _sha(Path(config_path).read_bytes()),
        "development_sha256": _sha(DEVELOPMENT_PATH.read_bytes()),
        "fonts_sha256": _sha((root / "fonts/fonts.json").read_bytes()),
        "dependency_lock": {
            "relative_path": "requirements-benchmark.lock",
            "sha256": _sha((ROOT / "requirements-benchmark.lock").read_bytes()),
        },
        "generator_sources": [
            _file(ROOT / "scripts/ocr_benchmark" / name, ROOT) for name in ("corpus.py", "fonts.py")
        ],
        "toolchain": {
            "python": platform.python_version(),
            **{
                name: version(name)
                for name in ("reportlab", "fonttools", "pillow", "pypdf", "pypdfium2")
            },
        },
        "cases": cases,
        "development": dev_cases,
        "assets": [_file(path, root) for path in sorted(root.rglob("*")) if path.is_file()],
    }
    (root / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf8",
        newline="\n",
    )
    return result


def verify_corpus(output_dir):
    root = Path(output_dir)
    manifest = _json(root / "manifest.json")
    assets = {}
    for record in manifest["assets"]:
        relative = Path(record["relative_path"])
        if (
            record["relative_path"] in assets
            or relative.is_absolute()
            or ".." in relative.parts
            or (root / relative).is_symlink()
        ):
            raise CorpusError("manifest_invalid")
        path = root / relative
        if path.resolve().is_relative_to(root.resolve()) is False or _file(path, root) != record:
            raise CorpusError("asset_mismatch")
        assets[record["relative_path"]] = record
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != root / "manifest.json"
    }
    if actual != assets.keys():
        raise CorpusError("asset_mismatch")
    for case in [*manifest["cases"], *manifest["development"]]:
        if assets.get(case["relative_path"]) != {
            key: case[key] for key in ("relative_path", "sha256", "byte_size")
        }:
            raise CorpusError("manifest_invalid")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("generate")
    generate.add_argument("--source-dir", type=Path, required=True)
    generate.add_argument("--output-dir", type=Path, required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            generate_corpus(args.source_dir, args.output_dir)
            if args.command == "generate"
            else verify_corpus(args.output_dir)
        )
    except Exception as error:
        parser.exit(1, (str(error) if isinstance(error, CorpusError) else "corpus_failed") + "\n")
    print(
        json.dumps(
            {
                "version": 1,
                "cases": len(result["cases"]),
                "development": len(result["development"]),
                "manifest_sha256": _sha((args.output_dir / "manifest.json").read_bytes()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
