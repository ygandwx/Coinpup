"""Private plain-text parser inputs; coordinates are never financial quantities."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Word:
    text: str
    bbox: tuple[int | float, int | float, int | float, int | float]


@dataclass(frozen=True)
class TextPage:
    width: int | float
    height: int | float
    words: tuple[Word, ...]
    text: str = ""


@dataclass(frozen=True)
class ParserLimits:
    max_pages: int = 50
    words_per_page: int = 2000
    max_words: int = 5000
    text_bytes: int = 131072
    word_bytes: int = 2048
    max_lines: int = 5000
    max_tables: int = 16
    max_rows: int = 200
    max_fields: int = 1000
    evidence_bytes: int = 524288
    output_bytes: int = 786432

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            minimum = 128 if item.name == "output_bytes" else 1
            if type(value) is not int or not minimum <= value <= item.default:
                raise ValueError("Invalid parser limits.")
