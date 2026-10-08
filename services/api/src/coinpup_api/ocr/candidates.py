"""Map bounded recognition to review drafts; never infer a financial command."""

import re
from copy import deepcopy

from pydantic import ValidationError

from coinpup_api.ledger.service import LedgerError

from .contracts import RESULT_BYTES, Candidate, Completion, canonical_json, prepare_completion
from .runtime import field_review

_ROW = re.compile(
    r"rows\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(date|currency|amount)\Z"
)
_HEADER = re.compile(r"header\.[a-z_]+\Z")


def _require(value):
    if not value:
        raise ValueError


def _index(value):
    _require(type(value) is int and 0 <= value < 1000000)
    return value


def _validate(result):
    _require(type(result) is dict and type(result["version"]) is int and result["version"] == 1)
    _require(result["status"] in ("processed", "manual"))
    _require(type(result["raw_text"]) is str and type(result["pages"]) is list)
    parsed = result["parsed"]
    _require(type(parsed) is dict and parsed["status"] in ("parsed", "review"))
    _require(all(type(parsed[name]) is list for name in ("fields", "rows", "diagnostics")))
    _require(all(type(item) is str for item in parsed["diagnostics"]))
    pages = {}
    for page in result["pages"]:
        index = _index(page["page_index"])
        _require(index not in pages and type(page["text"]) is str)
        _require(
            (page["layer"], page["route"])
            in {
                ("present", "extract"),
                ("absent", "render"),
                ("unknown", "manual"),
                ("present", "manual"),
                ("absent", "manual"),
            }
        )
        pages[index] = page
    _require(sorted(pages) == list(range(len(pages))))
    rows = {}
    for row in parsed["rows"]:
        identity = tuple(_index(row[name]) for name in ("page", "table", "row"))
        _require(identity not in rows and identity[0] in pages)
        _require(type(row["description"]) is str)
        rows[identity] = row
    fields = {}
    for field in parsed["fields"]:
        path = field["path"]
        _require(type(path) is str and path not in fields)
        match = _ROW.fullmatch(path)
        _require(bool(match) or bool(_HEADER.fullmatch(path)))
        if match:
            _require(tuple(map(int, match.groups()[:3])) in rows)
        _require(field["value"] is None or type(field["value"]) is str)
        _require(field["status"] in ("certain", "review"))
        _require(field["status"] != "certain" or field["value"] is not None)
        _require(type(field["evidence"]) is list)
        for evidence in field["evidence"]:
            _require(_index(evidence["page"]) in pages and type(evidence["raw"]) is str)
        fields[path] = field
    for page, table, row in rows:
        _require(
            all(
                f"rows.{page}.{table}.{row}.{role}" in fields
                for role in ("date", "currency", "amount")
            )
        )
    return pages, rows, fields


def completion_from_result(result: dict) -> Completion:
    """Freeze original evidence once; physical row identity survives equal amounts/retries."""
    try:
        canonical_json(result, RESULT_BYTES)
        result = deepcopy(result)
        pages, rows, fields = _validate(result)
        # Recompute in the parent; a child-supplied prefill flag is not authorization.
        reviews = {item["path"]: item for item in field_review(result)}
        _require(result["field_review"] == list(reviews.values()))
        candidates = []

        def add(key, kind, paths, page_indices, description=""):
            selected = [fields[path] for path in paths]
            candidates.append(
                Candidate(
                    source_key=key,
                    recognized={
                        "version": 1,
                        "kind": kind,
                        "fields": selected,
                        "description": description,
                        "diagnostics": result["parsed"]["diagnostics"],
                    },
                    evidence={"version": 1, "pages": sorted(page_indices)},
                    fields={
                        "version": 1,
                        "review": [reviews[path] for path in paths],
                        "confirmed": [],
                    },
                )
            )

        if rows:
            for (page, table, row), metadata in rows.items():
                prefix = f"rows.{page}.{table}.{row}."
                add(
                    f"page:{page}/table:{table}/row:{row}",
                    "statement_row",
                    [path for path in fields if path.startswith(prefix)],
                    [page],
                    metadata["description"],
                )
            row_pages = {identity[0] for identity in rows}
            for index in pages:
                if index not in row_pages:
                    add(f"page:{index}/manual", "manual_page", [], [index])
        else:
            add("document:0", "document", list(fields), pages)
        completion = Completion(
            summary={
                "pages": len(pages),
                "manual_pages": sum(page["route"] == "manual" for page in pages.values()),
                # JobView returns only counts. Authenticated draft reads select evidence here.
                "recognition": result,
            },
            candidates=tuple(candidates),
        )
        prepare_completion(completion)
        return completion
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, ValidationError):
        raise LedgerError(
            "ocr_invalid_payload", 422, "OCR result is invalid or exceeds its limits."
        ) from None
