"""Real optional-wheel API smoke, using only a small explicitly fictional PDF."""

import runpy
from contextlib import closing
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import pytest

pytestmark = pytest.mark.ocr


def test_locked_pdf_libraries_read_extract_and_render_a_fictional_document():
    auditor = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/check_ocr_dependencies.py")
    )
    assert len(auditor["audit"]()["packages"]) == 7
    # Imports stay inside the OCR test so default API checks need no optional packages.
    import pdfplumber
    import pypdfium2 as pdfium
    from PIL import Image
    from pypdf import Configuration, PageObject, PdfReader, PdfWriter, apply_configuration
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    assert version("pypdf") == "6.19.0"
    assert version("pdfplumber") == "0.11.10"
    assert version("pypdfium2") == "5.13.0"
    text = "FICTIONAL TEST USD 12.30"
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 60 Td (" + text.encode("ascii") + b") Tj ET")
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page = PageObject.create_blank_page(width=240, height=100)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    page[NameObject("/Contents")] = stream
    with PdfWriter() as writer, BytesIO() as output:
        writer.add_page(page)
        writer.write(output)
        document = output.getvalue()
    assert document.startswith(b"%PDF-") and len(document) < 4096

    limits = Configuration(
        maximum_declared_stream_length=4096,
        array_based_stream_maximum_output_length=4096,
        zlib_maximum_output_length=4096,
        lzw_maximum_output_length=4096,
        run_length_maximum_output_length=4096,
        jbig2dec_binary=None,
    )
    with apply_configuration(limits), PdfReader(BytesIO(document), strict=True) as reader:
        assert len(reader.pages) == 1
        content = reader.pages[0].get_contents()
        assert text.encode("ascii") in content.get_data()
        assert any(
            operator == b"Tj" and operands == [text] for operands, operator in content.operations
        )
        assert reader.pages[0].extract_text() == text

    with pdfplumber.open(BytesIO(document)) as extracted:
        page = extracted.pages[0]
        assert page.extract_text() == text
        assert "".join(char["text"] for char in page.chars) == text
        page.close()

    with pdfium.PdfDocument(document) as rendered, closing(rendered[0]) as page:
        assert page.get_size() == (240, 100)
        with closing(
            page.render(scale=1, grayscale=True, may_draw_forms=False, draw_annots=False)
        ) as bitmap:
            assert (bitmap.width, bitmap.height) == (240, 100)
            assert bitmap.width * bitmap.height <= 24_000
            with bitmap.to_pil() as borrowed:
                # PDFium owns the borrowed pixels. Detach before closing any native handles.
                copied = borrowed.copy()
    try:
        assert copied.mode == "L" and copied.size == (240, 100)
        assert copied.getextrema()[0] < 255
        with BytesIO() as encoded:
            copied.save(encoded, format="PNG")
            png = encoded.getvalue()
        assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 32_768
        with Image.open(BytesIO(png)) as decoded:
            decoded.load()
            assert decoded.size == (240, 100)
    finally:
        copied.close()
