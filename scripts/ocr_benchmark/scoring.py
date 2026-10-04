"""Frozen exact-field scoring and Unicode CER, independent of extraction and engines."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction

_ROLE = r"[a-z][a-z0-9_]*"
_ORDINAL = r"(?:0|[1-9][0-9]*)"
_PATH = re.compile(rf"(?:header\.{_ROLE}|rows\.{_ORDINAL}\.{_ORDINAL}\.{_ORDINAL}\.{_ROLE})")


@dataclass(frozen=True)
class FieldScore:
    correct: int
    expected: int
    extra: int

    def __post_init__(self):
        if (
            any(
                type(value) is not int or value < 0
                for value in (self.correct, self.expected, self.extra)
            )
            or self.correct > self.expected
        ):
            raise ValueError("Invalid field score.")

    @property
    def incorrect(self):
        return self.expected - self.correct

    @property
    def denominator(self):
        return self.expected + self.extra

    @property
    def accuracy(self):
        return Fraction(self.correct, self.denominator) if self.denominator else None


def _path(value):
    return type(value) is str and _PATH.fullmatch(value) is not None


def score_fields(expected: dict[str, str], predicted: list[dict]) -> FieldScore:
    """Compare prestructured values literally; never normalize or search for a better row.

    Repeated paths invalidate that expected slot even when values agree. Each subsequent
    occurrence is extra; every occurrence of an unknown path is extra. No later duplicate
    can rescue an earlier prediction. Input records have exactly path/value/status keys.
    """
    if type(expected) is not dict or any(
        not _path(path) or type(value) is not str for path, value in expected.items()
    ):
        raise ValueError("Invalid expected fields.")
    if type(predicted) is not list:
        raise ValueError("Invalid predicted fields.")
    counts, first = Counter(), {}
    for field in predicted:
        if (
            type(field) is not dict
            or field.keys() != {"path", "value", "status"}
            or not _path(field["path"])
            or (field["value"] is not None and type(field["value"]) is not str)
            or field["status"] not in ("certain", "review")
        ):
            raise ValueError("Invalid predicted fields.")
        counts[field["path"]] += 1
        first.setdefault(field["path"], field)
    correct = sum(
        counts[path] == 1 and first[path]["status"] == "certain" and first[path]["value"] == value
        for path, value in expected.items()
    )
    extra = sum(count - (path in expected) for path, count in counts.items())
    return FieldScore(correct, len(expected), extra)


def aggregate_scores(scores) -> FieldScore:
    scores = tuple(scores)
    return FieldScore(
        sum(score.correct for score in scores),
        sum(score.expected for score in scores),
        sum(score.extra for score in scores),
    )


def passes(score: FieldScore, percent: int = 95) -> bool:
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("Invalid percentage threshold.")
    return score.expected > 0 and 100 * score.correct >= percent * score.denominator


def within_two_points(left: FieldScore, right: FieldScore) -> bool:
    return bool(
        left.denominator
        and right.denominator
        and 100 * abs(left.correct * right.denominator - right.correct * left.denominator)
        <= 2 * left.denominator * right.denominator
    )


@dataclass(frozen=True)
class Selection:
    selected: str | None
    stop: bool
    reason: str


def select_engine(text: FieldScore, paddle: FieldScore, tesseract: FieldScore) -> Selection:
    if not passes(text, 100):
        return Selection(None, True, "text_gate_failed")
    paddle_ok, tesseract_ok = passes(paddle), passes(tesseract)
    if not paddle_ok and not tesseract_ok:
        return Selection(None, True, "neither_qualified")
    if not paddle_ok:
        return Selection("tesseract", False, "tesseract_only")
    if not tesseract_ok:
        return Selection("paddle", False, "paddle_only")
    if within_two_points(paddle, tesseract):
        return Selection("tesseract", False, "within_two_points")
    higher = (
        "paddle"
        if paddle.correct * tesseract.denominator > tesseract.correct * paddle.denominator
        else "tesseract"
    )
    return Selection(higher, False, "higher_accuracy")


@dataclass(frozen=True)
class CharacterScore:
    edits: int
    reference_chars: int

    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in (self.edits, self.reference_chars)):
            raise ValueError("Invalid character score.")

    @property
    def ratio(self):
        return Fraction(self.edits, self.reference_chars) if self.reference_chars else None


def character_errors(reference: str, prediction: str) -> CharacterScore:
    """Only NFC and CRLF→LF; rolling rows use O(min(lengths)) working memory."""
    if type(reference) is not str or type(prediction) is not str:
        raise ValueError("Invalid character text.")
    reference, prediction = (
        unicodedata.normalize("NFC", text.replace("\r\n", "\n")) for text in (reference, prediction)
    )
    reference_chars = len(reference)
    short, long = sorted((reference, prediction), key=len)
    previous = list(range(len(short) + 1))
    for row, right in enumerate(long, 1):
        current = [row]
        for column, left in enumerate(short, 1):
            current.append(
                min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left != right))
            )
        previous = current
    return CharacterScore(previous[-1], reference_chars)


def aggregate_cer(scores) -> dict:
    """Empty references retain their insertion errors; no fabricated CER denominator."""
    scores = tuple(scores)
    edits, reference_chars = (
        sum(score.edits for score in scores),
        sum(score.reference_chars for score in scores),
    )
    nonempty = [score.ratio for score in scores if score.reference_chars]
    empty = [score for score in scores if not score.reference_chars]
    return {
        "documents": len(scores),
        "edits": edits,
        "reference_chars": reference_chars,
        "micro": Fraction(edits, reference_chars) if reference_chars else None,
        "macro": sum(nonempty, Fraction()) / len(nonempty) if nonempty else None,
        "empty_reference_documents": len(empty),
        "false_positive_documents": sum(score.edits > 0 for score in empty),
        "false_positive_chars": sum(score.edits for score in empty),
    }
