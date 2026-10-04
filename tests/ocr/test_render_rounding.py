"""Real A5/300 native rendering and allocation limits, without corpus-build tools."""

from dataclasses import replace

import pytest
from coinpup_api.ocr.pdf_prepare import PrepareLimits, prepare_pdf

from tests.ocr.pdf_fixtures import image_document

pytestmark = pytest.mark.ocr


def _a5(rotation=0):
    from pypdf.generic import NameObject, NumberObject

    def geometry(page, writer):
        page.mediabox.upper_right = (420, 595)
        page[NameObject("/Rotate")] = NumberObject(rotation)

    return image_document(encoding="jpeg", mutate=geometry)


@pytest.mark.parametrize("rotation,size", [(0, (1751, 2480)), (90, (2480, 1751))])
def test_actual_a5_pdfium_boundary_rounding_is_accepted(rotation, size):
    result = prepare_pdf(_a5(rotation), PrepareLimits(dpi=300))
    assert result["reason_code"] is None and result["page_count"] == 1
    page = result["pages"][0]
    assert page["layer"] == "absent" and page["route"] == "render"
    assert page["reason_code"] is None
    assert page["raster"] == {
        "width": size[0],
        "height": size[1],
        "stride": size[0] * 4,
        "format": "RGBX",
        "dpi": 300,
    }


@pytest.mark.parametrize(
    "field,maximum", [("page_pixels", 1750 * 2480), ("bitmap_bytes", 1750 * 2480 * 4)]
)
def test_extra_native_boundary_column_is_budgeted_before_any_allocation(
    monkeypatch, field, maximum
):
    import pypdfium2

    calls, original = [], pypdfium2.raw.FPDFBitmap_CreateEx

    def observe(*arguments):
        calls.append(arguments)
        return original(*arguments)

    monkeypatch.setattr(pypdfium2.raw, "FPDFBitmap_CreateEx", observe)
    result = prepare_pdf(_a5(), replace(PrepareLimits(dpi=300), **{field: maximum}))
    page = result["pages"][0]
    assert page["route"] == "manual" and page["reason_code"] == "prepare_limit"
    assert page["raster"] is None and calls == []
