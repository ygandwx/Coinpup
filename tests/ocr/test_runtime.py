"""Real PDF libraries/isolated text; mixed-page adapter is explicitly fictional."""

import hashlib
import json
import sys
from io import BytesIO
from uuid import uuid4

import pytest
from coinpup_api.ocr import processor, runtime
from coinpup_api.ocr.isolation import ProcessBudget, run_isolated

from tests.ocr.pdf_fixtures import document, image_document
from tests.ocr.test_recognize import TEXT, FakeAdapter

pytestmark = pytest.mark.ocr


def request(tmp_path, data):
    path = tmp_path / (uuid4().hex + ".pdf")
    tmp_path.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return {
        "version": 1,
        "action": "recognize_document",
        "source": {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
        },
        "media_type": "application/pdf",
        "processing": runtime.processing_configuration(),
    }


def assert_text(result):
    assert result["status"] == "processed"
    assert result["raw_text"] == "Total: -001,234.00"
    assert result["field_review"] == [
        {
            "path": result["parsed"]["fields"][0]["path"],
            "source": "text",
            "candidate_value": "-1234.00",
            "suggested_value": "-1234.00",
            "requires_confirmation": False,
        }
    ]


def test_real_text_dispatch_never_opens_models(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_verify_models", lambda: pytest.fail("Text loaded models"))
    assert_text(processor.process(request(tmp_path, document(TEXT)), tmp_path))


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux process limits required")
def test_real_isolated_text_dispatch_without_models(tmp_path):
    value = request(tmp_path, document(TEXT))
    result = run_isolated(value, ProcessBudget(20, 10, 512 * 1024**2, 1048576, 128, 1048576))
    assert result.stderr_bytes == 0
    assert_text(json.loads(result.output))


def test_real_mixed_pdf_shared_field_cannot_inherit_text_prefill(tmp_path, monkeypatch):
    from pypdf import PdfReader, PdfWriter

    with PdfWriter() as writer, BytesIO() as output:
        for data in (document(TEXT), image_document()):
            with BytesIO(data) as stream:
                writer.add_page(PdfReader(stream).pages[0])
        writer.write(output)
        value = request(tmp_path, output.getvalue())
    adapter = FakeAdapter()
    adapter.close = lambda: None
    monkeypatch.setattr(runtime, "_LazyAdapter", lambda: adapter)
    result = processor.process(value, tmp_path)
    assert len(adapter.calls) == 1
    assert [page["route"] for page in result["pages"]] == ["extract", "render"]
    field, review = result["parsed"]["fields"][0], result["field_review"][0]
    assert {item["page"] for item in field["evidence"]} == {0, 1}
    assert field["status"] == "certain" and field["value"] == "-1234.00"
    assert review["source"] == "ocr" and review["candidate_value"] == "-1234.00"
    assert review["suggested_value"] is None and review["requires_confirmation"] is True
