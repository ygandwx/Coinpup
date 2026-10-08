"""Handwritten fictional inputs for the production boundary, never benchmark answers."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.ocr import engine_adapters, processor, recognize, runtime
from coinpup_api.ocr.contracts import prepare_configuration
from coinpup_api.ocr.engine_adapters import EngineError


def request(tmp_path):
    data = b"Fictional source, not a valid PDF"
    path = tmp_path / (uuid4().hex + ".pdf")
    tmp_path.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return {
        "version": 1,
        "action": "recognize_document",
        "source": {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
        },
        "media_type": "application/pdf",
        "processing": runtime.processing_configuration(),
    }


def parsed():
    return {
        "pages": [
            {"page_index": 0, "layer": "present", "route": "extract"},
            {"page_index": 1, "layer": "absent", "route": "render"},
            {"page_index": 2, "layer": "present", "route": "manual"},
        ],
        "parsed": {"fields": []},
    }


@pytest.mark.parametrize(
    "indices,status,source,prefill",
    [
        ([0], "certain", "text", True),
        ([0], "review", "text", False),
        ([1], "certain", "ocr", False),
        ([1], "review", "ocr", False),
        ([0, 1], "certain", "ocr", False),
        ([2], "certain", "unknown", False),
        ([0, 2], "certain", "unknown", False),
        ([9], "certain", "unknown", False),
        ([], "certain", "unknown", False),
    ],
)
def test_actual_field_evidence_controls_prefill_without_mutating_parser(
    indices, status, source, prefill
):
    result = parsed()
    field = {
        "path": "header.total",
        "value": "-1234.00",
        "status": status,
        "evidence": [{"page": index, "raw": "-001,234.00"} for index in indices],
    }
    result["parsed"]["fields"].append(field)
    original = deepcopy(result)
    review = runtime.field_review(result)
    assert result == original
    assert review == [
        {
            "path": "header.total",
            "source": source,
            "candidate_value": "-1234.00",
            "suggested_value": "-1234.00" if prefill else None,
            "requires_confirmation": not prefill,
        }
    ]


def test_configuration_fits_queue_and_is_a_detached_snapshot_of_all_budgets():
    config = runtime.processing_configuration()
    runtime.validate_processing(config)
    frozen, digest = prepare_configuration(
        {"lease_seconds": 120, "retry_seconds": 30, "processing": config}
    )
    assert frozen["processing"] == config and len(digest) == 64
    assert config["prepare"]["dpi"] == 300
    assert config["selection"]["ocr_prefill"] is False
    config["prepare"]["dpi"] = 200
    with pytest.raises(ValueError):
        runtime.validate_processing(config)
    assert runtime.processing_configuration()["prepare"]["dpi"] == 300


@pytest.mark.parametrize("mutation", ["bool", "float", "extra", "source", "profile", "prefill"])
def test_changed_frozen_configuration_is_rejected(mutation):
    config = runtime.processing_configuration()
    if mutation == "bool":
        config["version"] = True
    elif mutation == "float":
        config["version"] = 1.0
    elif mutation == "extra":
        config["unrecognized"] = "Fictional"
    elif mutation == "source":
        config["sources"]["recognize.py"] = "0" * 64
    elif mutation == "profile":
        config["selection"]["profile"]["psm"] = 3
    else:
        config["selection"]["ocr_prefill"] = True
    with pytest.raises(ValueError):
        runtime.validate_processing(config)


@pytest.mark.parametrize("mutation", ["version", "media", "extra", "config", "path", "size"])
def test_fixed_dispatch_rejects_shape_before_reading_source(tmp_path, monkeypatch, mutation):
    value = request(tmp_path)
    if mutation == "version":
        value["version"] = True
    elif mutation == "media":
        value["media_type"] = "image/svg+xml"
    elif mutation == "extra":
        value["engine"] = "paddle"
    elif mutation == "config":
        value["processing"] = {}
    elif mutation == "path":
        value["source"]["path"] = "../fictional.pdf"
    else:
        value["source"]["byte_size"] = True
    monkeypatch.setattr(processor, "_read_source", lambda *_: pytest.fail("Invalid input was read"))
    result = processor.process(value, tmp_path)
    assert result["status"] == "failed" and result["reason"] == "request_invalid"
    assert result["raw_text"] == "" and result["parsed"]["fields"] == []


def test_source_digest_mismatch_cannot_reach_recognition(tmp_path, monkeypatch):
    value = request(tmp_path)
    value["source"]["sha256"] = "0" * 64
    monkeypatch.setattr(
        recognize, "recognize_document", lambda *_: pytest.fail("Bad source consumed")
    )
    result = processor.process(value, tmp_path)
    assert result["reason"] == "source_invalid"
    assert str(tmp_path) not in json.dumps(result)


def test_fixed_dispatch_loads_no_engine_for_text_and_preserves_raw_result(tmp_path, monkeypatch):
    value = request(tmp_path)
    expected = recognize.failed_result("fictional_test")
    expected.update(status="processed", reason=None, raw_text="Fictional 原文")
    monkeypatch.setattr(runtime, "_verify_models", lambda: pytest.fail("Text initialized models"))
    monkeypatch.setattr(recognize, "recognize_document", lambda *_: deepcopy(expected))
    result = processor.process(value, tmp_path)
    assert result == {**expected, "field_review": []}


def test_adapter_is_verified_once_and_closed_on_recognition_error(monkeypatch, tmp_path):
    value, events = request(tmp_path), []
    adapter = SimpleNamespace(
        recognize=lambda *_: events.append("recognize"), close=lambda: events.append("close")
    )
    monkeypatch.setattr(runtime, "_verify_models", lambda: events.append("verify"))

    def create(profile, assets):
        assert profile == {"engine": "tesseract", "model_set": "fast", "psm": 6}
        assert assets == runtime._ASSETS
        events.append("load")
        return adapter

    monkeypatch.setattr(engine_adapters, "create_adapter", create)

    def recognize_twice(data, media, lazy):
        lazy.recognize(b"", 1, 1)
        lazy.recognize(b"", 1, 1)
        raise RuntimeError("Fictional processing exception")

    monkeypatch.setattr(recognize, "recognize_document", recognize_twice)
    with pytest.raises(RuntimeError):
        processor.process(value, tmp_path)
    assert events == ["verify", "load", "recognize", "recognize", "close"]


def test_failed_model_validation_cannot_initialize_an_sdk(monkeypatch):
    def unavailable():
        raise OSError("Fictional private model path")

    monkeypatch.setattr(runtime, "_verify_models", unavailable)
    monkeypatch.setattr(
        engine_adapters, "create_adapter", lambda *_: pytest.fail("Loaded bad models")
    )
    with pytest.raises(EngineError) as caught:
        runtime._LazyAdapter().recognize(b"", 1, 1)
    assert caught.value.reason == "engine_unavailable" and "Fictional" not in str(caught.value)


@pytest.mark.parametrize("damage", [None, "manifest", "size", "digest", "missing"])
def test_model_files_are_bound_to_the_frozen_manifest(monkeypatch, tmp_path, damage):
    files = []
    for language in ("eng", "chi_sim", "chi_tra"):
        name, data = f"tesseract/fast/{language}.traineddata", f"Fictional {language}".encode()
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(
            {"name": name, "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = tmp_path / "engine-assets.json"
    manifest.write_text(json.dumps({"files": files}), encoding="utf8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(runtime, "_ASSETS", tmp_path)
    monkeypatch.setattr(runtime, "selection", lambda: {"model_manifest_sha256": digest})
    path = tmp_path / files[0]["name"]
    if damage == "manifest":
        manifest.write_text("{}", encoding="utf8")
    elif damage == "size":
        path.write_bytes(b"Fictional changed size")
    elif damage == "digest":
        path.write_bytes(b"x" * files[0]["byte_size"])
    elif damage == "missing":
        path.unlink()
    if damage is None:
        runtime._verify_models()
    else:
        with pytest.raises((ValueError, OSError)):
            runtime._verify_models()
