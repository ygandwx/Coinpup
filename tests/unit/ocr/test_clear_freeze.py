"""New fictional truth is fixed before recognition; frozen evidence cannot be refreshed."""

import json
import re
from copy import deepcopy
from datetime import date

import pytest

from scripts.ocr_benchmark import formal_freeze as freeze


def cents(value):
    return int(value.replace(",", "").replace(".", ""))


def test_new_seed_changes_every_document_and_preserves_independent_integer_equations():
    before = freeze._json(freeze.TRUTH_PATH)
    original = deepcopy(before)
    sampled = freeze._sample_truth(before, freeze.CLEAR_SEED)
    assert before == original and sampled["sampling_seed"] != 20261004
    assert sampled == freeze._sample_truth(before, freeze.CLEAR_SEED)
    for old, new in zip(before["templates"], sampled["templates"], strict=True):
        assert old != new and old["id"] == new["id"]
        assert date.fromisoformat(new["header"]["document_date"])
        if "total" in new["header"]:
            assert cents(new["header"]["subtotal"]) + cents(new["header"]["tax"]) == cents(
                new["header"]["total"]
            )
            assert new["header"]["total"] != old["header"]["total"]
        rows = [row for page in new["pages"] for row in page["rows"]]
        for row in rows:
            assert date.fromisoformat(row["date"])
            assert cents(row["raw_amount"]) == cents(row["amount"])
        for key, opening in new["header"].items():
            if key.startswith("opening_balance_"):
                currency = key.rsplit("_", 1)[1]
                movements = sum(
                    cents(r["amount"]) for r in rows if r["currency"].lower() == currency
                )
                assert cents(opening) + movements == cents(
                    new["header"]["closing_balance_" + currency]
                )
        assert sum(len(p["rows"]) for p in old["pages"]) == len(rows)
        assert "FICTIONAL TEST DOCUMENT" in new["pages"][0]["lines"][0]


@pytest.mark.parametrize("seed", [True, False, 0, 20261004, "2026100501", freeze.CLEAR_SEED + 1])
def test_another_seed_cannot_silently_reuse_this_acceptance_protocol(seed):
    with pytest.raises(freeze.FormalFreezeError, match="clear_seed_invalid"):
        freeze._sample_truth(freeze._json(freeze.TRUTH_PATH), seed)


def test_sampling_preserves_difficult_raw_formats_instead_of_simplifying_them():
    truth = {
        "templates": [
            {
                "id": "S01",
                "header": {"document_date": "2026-09-01", "total": "1200.00"},
                "pages": [
                    {
                        "lines": ["FICTIONAL TEST DOCUMENT", "Total: +001,200.00", "Zero: -000.00"],
                        "rows": [],
                    }
                ],
            }
        ]
    }
    sampled = freeze._sample_truth(truth, freeze.CLEAR_SEED)["templates"][0]
    assert re.fullmatch(r"Total: \+00[234],[0-9]{3}\.00", sampled["pages"][0]["lines"][1])
    assert sampled["pages"][0]["lines"][2] == "Zero: -000.00"


def test_prepare_snapshots_fresh_truth_and_render_configuration_before_generation(
    tmp_path, monkeypatch
):
    corpus = tmp_path / "new-clear"
    seen = []

    def generate(source, output, *, truth_path, config_path):
        truth, config = freeze._json(truth_path), freeze._json(config_path)
        assert not output.exists()
        assert truth == freeze._sample_truth(freeze._json(freeze.TRUTH_PATH), freeze.CLEAR_SEED)
        assert config["degraded"] == config["errors"] == []
        assert config["seed"] == 20261004  # fixed renderer seed, not the truth sampling seed
        seen.append((source, output))
        return {"seed": config["seed"], "cases": []}

    monkeypatch.setattr(freeze, "generate_corpus", generate)
    freeze.prepare_clear(tmp_path / "fonts", corpus)
    assert seen == [(tmp_path / "fonts", corpus)]
    with pytest.raises(FileExistsError):
        freeze.prepare_clear(tmp_path / "fonts", corpus)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "changed", ["source", "models", "truth", "manifest", "DEV", "schedule", "seed"]
)
def test_clear_snapshot_is_write_once_and_each_drift_fails_without_refresh(
    tmp_path, monkeypatch, changed
):
    snapshot = {
        "version": 2,
        "source": "fixed source",
        "models": "fixed models",
        "truth": "fixed truth",
        "manifest": "fixed manifest",
        "DEV": "fixed DEV",
        "schedule": 3,
        "seed": freeze.CLEAR_SEED,
    }
    monkeypatch.setattr(freeze, "_clear_snapshot", lambda *_: deepcopy(snapshot))
    path = tmp_path / "frozen.json"
    args = (tmp_path, tmp_path / "corpus", tmp_path / "assets", tmp_path / "selection.json")
    freeze.freeze_clear(*args, path)
    original = path.read_bytes()
    assert freeze.load_clear(*args, path) == snapshot
    with pytest.raises(FileExistsError):
        freeze.freeze_clear(*args, path)
    snapshot[changed] = "changed after freeze"
    with pytest.raises(freeze.FormalFreezeError, match="formal_configuration_drift"):
        freeze.load_clear(*args, path)
    assert path.read_bytes() == original


def test_unknown_frozen_fields_do_not_create_a_permissive_override(tmp_path, monkeypatch):
    snapshot = {"version": 2, "cases": freeze.CLEAR_CASES}
    monkeypatch.setattr(freeze, "_clear_snapshot", lambda *_: snapshot)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps({**snapshot, "ignore_quality": True}), encoding="utf8")
    with pytest.raises(freeze.FormalFreezeError, match="formal_configuration_drift"):
        freeze.load_clear(tmp_path, tmp_path, tmp_path, tmp_path, path)


@pytest.mark.parametrize(
    "changed",
    ["missing_field", "extra_field", "answer", "reference", "mapping", "identity", "group"],
)
def test_manifest_cannot_drop_difficult_fields_or_rewrite_reference_before_freeze(
    tmp_path, monkeypatch, changed
):
    corpus = tmp_path / "corpus"
    inputs = freeze._clear_inputs(corpus)
    inputs.mkdir()
    truth = freeze._sample_truth(freeze._json(freeze.TRUTH_PATH), freeze.CLEAR_SEED)
    config = {**freeze._json(freeze.RENDER_CONFIG_PATH), "degraded": [], "errors": []}
    for name, value in (("truth.json", truth), ("configuration.json", config)):
        (inputs / name).write_text(json.dumps(value), encoding="utf8")
    cases = [
        {
            "id": template["id"] + "-" + carrier,
            "template_id": template["id"],
            "group": "text" if carrier == "text" else "ocr",
            "carrier": carrier,
            **freeze._reference(template, template, "photo" if carrier == "photo" else "scan"),
        }
        for template in truth["templates"]
        for carrier in ("text", "scan", "photo")
    ]
    manifest = {
        "cases": cases,
        "truth_sha256": freeze._identity(inputs / "truth.json")["sha256"],
        "configuration_sha256": freeze._identity(inputs / "configuration.json")["sha256"],
    }
    first = cases[0]
    if changed == "missing_field":
        first["expected_fields"].pop("header.total")
    elif changed == "extra_field":
        first["expected_fields"]["header.invented"] = "5.00"
    elif changed == "answer":
        first["expected_fields"]["header.total"] = "0.00"
    elif changed == "reference":
        first["reference_text"] = "recognized text substituted for independent truth"
    elif changed == "mapping":
        first["source_mapping"] = {"rows": []}
    elif changed == "identity":
        first["id"] = "wrong-sample-text"
    else:
        first["group"] = "ocr"
    monkeypatch.setattr(freeze.engine_assets, "verify_assets", lambda _: None)
    monkeypatch.setattr(freeze, "verify_corpus", lambda _: manifest)
    with pytest.raises(freeze.FormalFreezeError, match="formal_corpus_drift"):
        freeze._clear_snapshot(
            tmp_path, corpus, tmp_path / "assets", tmp_path / "dev/selection.json"
        )
