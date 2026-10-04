"""Real fictional PDF objects prove conservative routing, not recognition quality."""

import hashlib
import json
import sys
import zlib
from dataclasses import replace
from uuid import uuid4

import pytest
from coinpup_api.ocr.isolation import ProcessBudget, run_isolated
from coinpup_api.ocr.pdf_probe import ProbeLimits, probe_pdf
from coinpup_api.ocr.processor import process

from tests.ocr.pdf_fixtures import document, form, raw_document, stream_bytes

pytestmark = pytest.mark.ocr
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux no-follow/isolation")
TEXT = b"BT /F1 12 Tf 10 10 Td (FICTIONAL TEST) Tj ET"


def page_result(result, layer, route):
    assert set(result) == {"version", "page_count", "pages", "reason_code"}
    assert result["version"] == 1 and result["page_count"] == 1
    assert result["reason_code"] is None
    assert result["pages"] == [
        {"page_index": 0, "layer": layer, "route": route, "reason_code": None}
    ]


def manual(result, reason):
    assert result["version"] == 1
    reasons = [result["reason_code"], *(page["reason_code"] for page in result["pages"])]
    assert reason in reasons
    assert all(page["route"] == "manual" for page in result["pages"])
    assert all(page["layer"] in {"present", "unknown"} for page in result["pages"])


def xobject(page, value, name="/F"):
    from pypdf.generic import DictionaryObject, NameObject

    page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject(name): value})


@pytest.mark.parametrize(
    "show",
    [
        b"(FICTIONAL) Tj",
        b"[(FICTIONAL) 1 ( TEST)] TJ",
        b"(FICTIONAL) '",
        b'0 0 (FICTIONAL) "',
        b"() Tj",
        b"<FF00FF> Tj",
    ],
)
def test_all_show_operators_include_empty_or_garbled_layers(show):
    page_result(probe_pdf(document(b"BT /F1 12 Tf " + show + b" ET")), "present", "extract")


@pytest.mark.parametrize("contents", [b"", b"q 1 0 0 1 0 0 cm Q"])
def test_blank_or_graphics_only_pages_are_provably_absent(contents):
    page_result(probe_pdf(document(contents)), "absent", "render")


def test_image_only_page_has_no_text_operator():
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

    image = DecodedStreamObject()
    image.set_data(b"\xff")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        }
    )
    data = document(b"q /F Do Q", mutate=lambda page, _: xobject(page, image))
    page_result(probe_pdf(data), "absent", "render")


@pytest.mark.parametrize("invoked", [False, True])
def test_only_invoked_forms_affect_text_presence(invoked):
    data = document(b"/F Do" if invoked else b"", mutate=lambda page, _: xobject(page, form(TEXT)))
    page_result(
        probe_pdf(data), "present" if invoked else "absent", "extract" if invoked else "render"
    )


def test_content_arrays_preserve_a_later_text_layer():
    page_result(probe_pdf(document([b"q Q", TEXT])), "present", "extract")


@pytest.mark.parametrize(
    "contents,layer",
    [
        (b"BT", "unknown"),
        (b"q", "unknown"),
        (b"/Missing Do", "unknown"),
        (b"FictionalOperator", "unknown"),
        (b"1 2", "unknown"),
        (b"q BT Q ET", "unknown"),
        (b"q BT (FICTIONAL) Tj Q ET", "present"),
        (b"/Missing gs", "unknown"),
        (b"/Missing sh", "unknown"),
        (b"BI /W 1 /H 1 /BPC 8 /CS /G ID \x80 EI", "unknown"),
    ],
)
def test_incomplete_or_unknown_content_never_requests_render(contents, layer):
    result = probe_pdf(document(contents))
    assert result["pages"][0]["route"] == "manual"
    assert result["pages"][0]["layer"] == layer


@pytest.mark.parametrize("kind", ["annotation", "acroform", "xfa", "pattern", "smask"])
def test_uninspected_text_capable_structures_never_request_render(kind):
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

    def mutate(page, writer):
        if kind == "annotation":
            page[NameObject("/Annots")] = ArrayObject([DictionaryObject()])
        elif kind in {"acroform", "xfa"}:
            writer.root_object[NameObject("/AcroForm")] = DictionaryObject(
                {NameObject("/XFA" if kind == "xfa" else "/Fields"): ArrayObject()}
            )
        elif kind == "pattern":
            page["/Resources"][NameObject("/Pattern")] = DictionaryObject(
                {NameObject("/P"): DictionaryObject({NameObject("/PatternType"): NumberObject(1)})}
            )
        else:
            page["/Resources"][NameObject("/ExtGState")] = DictionaryObject(
                {NameObject("/GS"): DictionaryObject({NameObject("/SMask"): DictionaryObject()})}
            )

    manual(probe_pdf(document(mutate=mutate)), "unsupported_pdf")


def test_form_cycle_is_unknown_without_recursive_render():
    data = raw_document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] "
            b"/Resources << /XObject << /F 5 0 R >> >> /Contents 4 0 R >>",
            stream_bytes(b"/F Do"),
            stream_bytes(
                b"/F Do",
                b"/Type /XObject /Subtype /Form /BBox [0 0 240 100] "
                b"/Resources << /XObject << /F 5 0 R >> >> ",
            ),
        ]
    )
    result = probe_pdf(data)
    assert result["pages"][0]["layer"] == "unknown"
    assert result["pages"][0]["route"] == "manual"


def test_form_depth_and_repeated_invocation_budgets():
    from pypdf.generic import DictionaryObject, NameObject

    nested = form(TEXT)
    for _ in range(3):
        nested = form(
            b"/F Do",
            DictionaryObject(
                {NameObject("/XObject"): DictionaryObject({NameObject("/F"): nested})}
            ),
        )
    data = document(b"/F Do", mutate=lambda page, _: xobject(page, nested))
    manual(probe_pdf(data, replace(ProbeLimits(), form_depth=1)), "probe_limit")
    repeated = document(b"/F Do /F Do", mutate=lambda page, _: xobject(page, form(TEXT)))
    manual(probe_pdf(repeated, replace(ProbeLimits(), form_invocations=1)), "probe_limit")


@pytest.mark.parametrize(
    "field,contents,value",
    [
        ("stream_bytes", b" " * 200, 100),
        ("operations", b"q Q q Q", 1),
        ("operand_items", b"1 2 3 4 5 6 cm", 2),
        ("operand_depth", b"[[[1]]] FictionalOperator", 1),
        ("content_streams", [b"q Q", TEXT], 1),
        ("objects", TEXT, 1),
        ("resource_entries", TEXT, 1),
    ],
)
def test_content_work_is_bounded_before_absence_is_claimed(field, contents, value):
    manual(probe_pdf(document(contents), replace(ProbeLimits(), **{field: value})), "probe_limit")


def test_compressed_stream_has_a_real_decompressed_limit():
    data = document(b" " * 200_000, compressed=True)
    assert len(data) < 4096  # Small compressed bytes must not hide a large decoded stream.
    manual(probe_pdf(data, replace(ProbeLimits(), stream_bytes=1024)), "probe_limit")


@pytest.mark.parametrize("kind", ["filters", "indirect_hops"])
def test_filter_budget_and_malformed_reference_chains_never_render(kind):
    payload = stream_bytes(
        zlib.compress(zlib.compress(b"q Q")), b"/Filter [/FlateDecode /FlateDecode] "
    )
    contents = [payload] if kind == "filters" else [b"5 0 R", stream_bytes(b"q Q")]
    data = raw_document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] "
            b"/Resources <<>> /Contents 4 0 R >>",
            *contents,
        ]
    )
    # Pinned pypdf rejects serialized indirect aliases before the additional hop guard.
    reason = "probe_limit" if kind == "filters" else "invalid_pdf"
    manual(probe_pdf(data, replace(ProbeLimits(), **{kind: 1})), reason)


def test_total_stream_budget_does_not_reset_between_pages():
    result = probe_pdf(
        document(b" " * 100, pages=2), replace(ProbeLimits(), total_stream_bytes=150)
    )
    assert result["page_count"] == 2
    assert result["pages"][0]["route"] == "render"
    assert result["pages"][1]["route"] == "manual"
    assert result["pages"][1]["reason_code"] == "probe_limit"


@pytest.mark.parametrize("value", ["unknown", "wrong_shape", "decode_params"])
def test_filter_shapes_never_fall_back_to_render(value):
    from pypdf.generic import ArrayObject, NameObject, NumberObject

    def mutate(page, _):
        stream = page["/Contents"]
        stream[NameObject("/Filter")] = (
            NameObject("/FictionalDecode")
            if value == "unknown"
            else ArrayObject([NameObject("/FlateDecode"), NumberObject(1)])
        )
        if value == "decode_params":
            stream[NameObject("/Filter")] = NameObject("/FlateDecode")
            stream[NameObject("/DecodeParms")] = NumberObject(1)

    manual(
        probe_pdf(document(mutate=mutate)),
        "invalid_pdf" if value == "decode_params" else "unsupported_pdf",
    )


@pytest.mark.parametrize(
    "field,value", [("max_pages", 1), ("page_tree_entries", 1), ("page_tree_depth", 1)]
)
def test_page_tree_work_is_bounded(field, value):
    data = document(pages=2)
    if field == "page_tree_depth":
        data = raw_document(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 3 0 R /MediaBox [0 0 240 100] /Resources <<>> >>",
            ]
        )
    result = probe_pdf(data, replace(ProbeLimits(), **{field: value}))
    assert result["page_count"] is None and result["pages"] == []
    assert result["reason_code"] == "probe_limit"


def test_page_tree_cycle_is_not_an_empty_document():
    data = raw_document(
        [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>"]
    )
    result = probe_pdf(data)
    assert result["pages"] == [] and result["reason_code"] in {"unsupported_pdf", "invalid_pdf"}


def test_bad_xref_encryption_and_invalid_bytes_have_no_render_candidates():
    data = document()
    start = data.rfind(b"startxref\n") + len(b"startxref\n")
    end = data.index(b"\n", start)
    damaged = data[:start] + b"0" + data[end:]
    for payload, allowed in [
        (damaged, {"parser_warning", "invalid_pdf"}),
        (document(password="fictional-test-password"), {"encrypted_pdf"}),
        (b"not a PDF: FICTIONAL TEST", {"invalid_pdf"}),
    ]:
        result = probe_pdf(payload)
        assert result["reason_code"] in allowed and result["pages"] == []


def test_real_parser_warning_prevents_render_and_is_not_logged(caplog):
    data = raw_document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] "
            b"/Resources <<>> /Contents 99 0 R >>",
        ]
    )
    manual(probe_pdf(data), "parser_warning")
    assert "Object 99" not in caplog.text


def source_request(tmp_path, data):
    path = tmp_path / f"{uuid4().hex}.pdf"
    path.write_bytes(data)
    if sys.platform == "linux":
        path.chmod(0o600)
        tmp_path.chmod(0o700)
    return {
        "version": 1,
        "action": "inspect_pdf",
        "source": {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
        },
    }


@pytest.mark.parametrize("change", ["hash", "size", "name", "extra", "version", "limits"])
def test_source_identity_and_request_shape_fail_without_disclosing_paths(tmp_path, change):
    request = source_request(tmp_path, document(TEXT))
    if change == "hash":
        request["source"]["sha256"] = "0" * 64
    elif change == "size":
        request["source"]["byte_size"] += 1
    elif change == "name":
        request["source"]["path"] = request["source"]["path"].replace(".pdf", ".txt")
    elif change == "extra":
        request["unexpected"] = "fictional"
    elif change == "version":
        request["version"] = True
    else:
        request["limits"] = {"operations": True}
    result = process(request, tmp_path)
    assert result["pages"] == [] and result["reason_code"] in {"source_invalid", "request_invalid"}
    assert str(tmp_path) not in json.dumps(result)


@LINUX
def test_symlink_and_nonprivate_parent_are_rejected(tmp_path):
    request = source_request(tmp_path, document())
    link = tmp_path / f"{uuid4().hex}.pdf"
    link.symlink_to(request["source"]["path"])
    request["source"]["path"] = str(link)
    assert process(request, tmp_path)["reason_code"] == "source_invalid"
    request["source"]["path"] = str(link.resolve())
    tmp_path.chmod(0o755)
    assert process(request, tmp_path)["reason_code"] == "source_invalid"


@LINUX
def test_real_bootstrap_probes_under_limits_without_a_render_or_ocr_pipeline(tmp_path):
    request = source_request(tmp_path, document(TEXT))
    budget = ProcessBudget(
        wall_seconds=10,
        cpu_seconds=5,
        address_space_bytes=256 * 1024 * 1024,
        file_bytes=1024 * 1024,
        open_files=64,
        output_bytes=16 * 1024,
    )
    result = json.loads(run_isolated(request, budget).output)
    page_result(result, "present", "extract")
    assert "FICTIONAL TEST" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)
