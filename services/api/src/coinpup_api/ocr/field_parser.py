"""Bounded, source-only field recognition. No templates, OCR engines or financial writes."""

import json
import math
import re
import unicodedata
from datetime import date

from .parser_types import ParserLimits, TextPage, Word

_DEFAULT = ParserLimits()
_CURRENCIES = frozenset({"USD", "GBP", "EUR", "HKD", "CNY"})
_LABELS = {
    "document_date": ("date", "document date", "invoice date", "日期", "开票日期", "開票日期"),
    "currency": ("currency", "币种", "幣種"),
    "subtotal": ("subtotal", "小计", "小計"),
    "tax": ("tax", "税额", "稅額"),
    "total": ("total", "合计", "合計"),
    "opening_balance": ("opening balance", "期初余额", "期初餘額"),
    "closing_balance": ("closing balance", "期末余额", "期末餘額"),
    "amount": ("amount", "金额", "金額"),
    "description": ("description", "说明", "說明"),
}


def _label(text):
    return re.sub(r"[\s/]+", "", text).casefold()


_ROLES = {
    _label(left + right): role
    for role, aliases in _LABELS.items()
    for left in ("", *aliases)
    for right in aliases
}


class _Review(Exception):
    def __init__(self, code):
        self.code = code


def _require(condition, code="invalid_input"):
    if not condition:
        raise _Review(code)


def _bound(value, maximum):
    _require(value <= maximum, "parser_limit")


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8"))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _validate(pages, limits):
    _require(type(pages) is tuple and isinstance(limits, ParserLimits))
    _bound(len(pages), limits.max_pages)
    words, size = 0, 0
    for page in pages:
        _require(type(page) is TextPage and type(page.words) is tuple)
        _require(_number(page.width) and _number(page.height))
        _require(0 < page.width <= 1000000 and 0 < page.height <= 1000000)
        _require(type(page.text) is str and "\x00" not in page.text)
        _bound(len(page.text), limits.text_bytes - size)
        size += len(page.text.encode("utf8"))
        _bound(size, limits.text_bytes)
        _bound(len(page.words), limits.words_per_page)
        words += len(page.words)
        _bound(words, limits.max_words)
        for word in page.words:
            _require(type(word) is Word and type(word.text) is str)
            _bound(len(word.text), min(limits.word_bytes, limits.text_bytes - size))
            _require(
                bool(word.text.strip())
                and not any(unicodedata.category(c) == "Cc" for c in word.text)
            )
            length = len(word.text.encode("utf8"))
            _bound(length, limits.word_bytes)
            size += length
            _bound(size, limits.text_bytes)
            _require(type(word.bbox) is tuple and len(word.bbox) == 4)
            _require(all(_number(value) for value in word.bbox))
            x0, y0, x1, y1 = word.bbox
            _require(0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height)


def _box(words):
    return [
        min(word.bbox[0] for word in words),
        min(word.bbox[1] for word in words),
        max(word.bbox[2] for word in words),
        max(word.bbox[3] for word in words),
    ]


def _lines(page):
    """Sort by physical position; never use field values to order or repair rows."""
    lines = []
    for word in sorted(page.words, key=lambda w: (w.bbox[1], w.bbox[0], w.bbox[3])):
        if lines:
            anchor = lines[-1][0].bbox
            overlap = min(anchor[3], word.bbox[3]) - max(anchor[1], word.bbox[1])
            height = min(anchor[3] - anchor[1], word.bbox[3] - word.bbox[1])
            if overlap >= height / 2:
                lines[-1].append(word)
                continue
            _require(overlap <= 0, "ambiguous_layout")
        lines.append([word])
    for line in lines:
        line.sort(key=lambda w: (w.bbox[0], w.bbox[2]))
        _require(
            all(left.bbox[2] <= right.bbox[0] for left, right in zip(line, line[1:], strict=False)),
            "ambiguous_layout",
        )
    return lines


def _text(words):
    return " ".join(word.text for word in words)


def _amount(raw):
    _require(len(raw) <= 64, "invalid_amount")
    _require(
        re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", raw),
        "invalid_amount",
    )
    unsigned = raw.lstrip("+-").replace(",", "")
    integer, _, fraction = unsigned.partition(".")
    integer = integer.lstrip("0") or "0"
    _require(len(integer) <= 20 and len(fraction) <= 18, "invalid_amount")
    negative = raw.startswith("-") and int(integer + fraction) != 0
    fraction = fraction.rstrip("0").ljust(2, "0")
    return ("-" if negative else "") + integer + "." + fraction


def _value(role, raw):
    raw = raw.strip()
    _require(bool(raw), "missing_field")
    if role in ("document_date", "date"):
        _require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw), "invalid_date")
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise _Review("invalid_date") from None
    if role == "currency":
        _require(raw in _CURRENCIES, "unknown_currency")
        return raw
    return _amount(raw)


def _equal(left, right):
    def decimal_key(value):
        if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            return value.rstrip("0").rstrip(".") if "." in value else value
        return value

    return decimal_key(left) == decimal_key(right)


def _header(words):
    raw = _text(words)
    separated = re.split(r"[:：]", raw, maxsplit=1)
    if len(separated) == 2:
        label, value = separated
    else:
        splits = [(_text(words[:i]), _text(words[i:])) for i in range(1, min(len(words), 8))]
        found = next(((label, value) for label, value in reversed(splits) if _role(label)), None)
        if found is None:
            return None
        label, value = found
    role = _role(label)
    return (role, value) if role else None


def _role(label):
    role = _ROLES.get(_label(label))
    if role in ("amount", "description"):
        return None
    if role:
        return role
    match = re.fullmatch(r"(.*?)\s*([A-Z]{3})", label.strip())
    if match:
        role = _ROLES.get(_label(match[1]))
        if role in ("opening_balance", "closing_balance"):
            return role + "_" + match[2].lower()
    return None


def _columns(words):
    result, index = [], 0
    while index < len(words):
        if words[index].text.strip() == "|":
            index += 1
            continue
        found = None
        for count in range(min(4, len(words) - index), 0, -1):
            span = words[index : index + count]
            role = _ROLES.get(_label(_text(span)))
            if role in ("document_date", "currency", "amount", "description"):
                found = ("date" if role == "document_date" else role, _box(span), count)
                break
        if found is None:
            return None
        role, box, count = found
        result.append((role, (box[0] + box[2]) / 2))
        index += count
    roles = [role for role, _ in result]
    if not {"date", "currency", "amount"} <= set(roles) or len(set(roles)) != len(roles):
        return None
    return result


def _literal_columns(words):
    """Recognize delimiters in one genuine source box, without inventing column positions."""
    if len(words) != 1 or "|" not in words[0].text:
        return None
    roles = [_ROLES.get(_label(cell)) for cell in words[0].text.split("|")]
    roles = ["date" if role == "document_date" else role for role in roles]
    allowed = {"date", "currency", "amount", "description"}
    if (
        all(role in allowed for role in roles)
        and {"date", "currency", "amount"} <= set(roles)
        and len(set(roles)) == len(roles)
    ):
        return roles
    # An incomplete or conflicting visible header must not reuse a previous table's roles.
    return [] if sum(role in allowed for role in roles) >= 2 else None


def _same_literal_table(table, roles, box, width):
    return table["literal"] == roles and all(
        abs(box[index] - table["header_box"][index]) <= width / 100 for index in (0, 2)
    )


def _marker(text):
    compact = re.sub(r"\s+", "", text).casefold()
    patterns = (
        r"(?:第([0-9]+)[页頁]/)?page([0-9]+)(?:of|/)([0-9]+)",
        r"第([0-9]+)[页頁][，,]?共([0-9]+)[页頁]",
    )
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, compact)
        if match:
            groups = match.groups()
            current, total = map(int, groups[-2:])
            if index == 0 and groups[0] is not None:
                _require(int(groups[0]) == current, "ambiguous_pagination")
            _require(1 <= current <= total <= 50, "ambiguous_pagination")
            return current, total
    return None


class _Parser:
    def __init__(self, limits):
        self.limits, self.fields, self.rows, self.diagnostics = limits, {}, [], []
        self.evidence_size, self.line_count = 0, 0

    def diagnostic(self, code):
        if code not in self.diagnostics:
            self.diagnostics.append(code)

    def field(self, path, role, raw, words, page, *, ambiguous=False):
        evidence = {"page": page, "bbox": _box(words), "raw": raw}
        self.evidence_size += _bytes(evidence)
        _bound(self.evidence_size, self.limits.evidence_bytes)
        try:
            _require(not ambiguous, "ambiguous_layout")
            if role.startswith(("opening_balance", "closing_balance")):
                _require(role.rsplit("_", 1)[-1].upper() in _CURRENCIES, "unknown_currency")
            value, reason = _value(role, raw), None
            if role not in ("date", "document_date", "currency") and len(value.split(".")[1]) > 2:
                reason = "excessive_precision"
        except _Review as error:
            value, reason = None, error.code
        incoming = {
            "path": path,
            "value": value,
            "status": "review" if reason else "certain",
            "reason": reason,
            "evidence": [evidence],
        }
        old = self.fields.get(path)
        if old is not None:
            old["evidence"].append(evidence)
            if old["value"] is None or value is None or not _equal(old["value"], value):
                old.update(value=None, status="review", reason="conflicting_field")
                self.diagnostic("conflicting_field")
        else:
            self.fields[path] = incoming
            _bound(len(self.fields), self.limits.max_fields)
        if reason:
            self.diagnostic(reason)

    def page(self, page, page_index):
        lines = _lines(page)
        self.line_count += len(lines)
        _bound(self.line_count, self.limits.max_lines)
        table, marker, active, count = None, None, False, 0
        for words in lines:
            box = _box(words)
            new_marker = _marker(_text(words))
            if new_marker:
                marker, active = new_marker, False
                continue
            literal = _literal_columns(words)
            if literal == []:
                active = False
                self.diagnostic("ambiguous_layout")
                continue
            columns = _columns(words) if literal is None else None
            if columns or literal:
                same = (
                    table is not None
                    and literal is None
                    and table["literal"] is None
                    and len(columns) == len(table["columns"])
                    and all(
                        left[0] == right[0] and abs(left[1] - right[1]) <= page.width / 100
                        for left, right in zip(columns, table["columns"], strict=True)
                    )
                )
                if table is not None and literal is not None:
                    same = _same_literal_table(table, literal, box, page.width)
                continuation = (
                    same
                    and marker
                    and table["marker"]
                    and (marker == (table["marker"][0] + 1, table["marker"][1]))
                    and "conflicting_field" not in self.diagnostics
                )
                if not continuation:
                    table = {"index": count, "row": 0, "columns": columns, "literal": literal}
                    count += 1
                    _bound(count, self.limits.max_tables)
                table.update(marker=marker, box=box, header_box=box)
                active = True
                continue
            header = _header(words)
            if header:
                role, value = header
                self.field("header." + role, role, value, words, page_index)
                active = False
                continue
            if not active:
                continue
            if box[1] - table["box"][3] > 2 * (table["box"][3] - table["box"][1]):
                active = False
                self.diagnostic("ambiguous_layout")
                continue
            if table["literal"] is not None:
                self.literal_row(words, table, page_index)
                table.update(row=table["row"] + 1, box=box)
                continue
            cells = {role: [] for role, _ in table["columns"]}
            centers = [position for _, position in table["columns"]]
            boundaries = [
                (left + right) / 2 for left, right in zip(centers, centers[1:], strict=False)
            ]
            ambiguous = any(
                word.bbox[0] < boundary < word.bbox[2]
                for word in words
                if word.text.strip() != "|"
                for boundary in boundaries
            )
            for word in words:
                if word.text.strip() == "|":
                    continue
                center = (word.bbox[0] + word.bbox[2]) / 2
                column = sum(center >= boundary for boundary in boundaries)
                cells[table["columns"][column][0]].append(word)
            self.rows.append(
                {
                    "page": page_index,
                    "table": table["index"],
                    "row": table["row"],
                    "description": _text(cells.get("description", [])),
                }
            )
            _bound(len(self.rows), self.limits.max_rows)
            for role in ("date", "currency", "amount"):
                path = f"rows.{page_index}.{table['index']}.{table['row']}.{role}"
                self.field(
                    path,
                    role,
                    _text(cells[role]),
                    cells[role] or words,
                    page_index,
                    ambiguous=ambiguous,
                )
            table.update(row=table["row"] + 1, box=box)

    def literal_row(self, words, table, page_index):
        raw = _text(words)
        parts = raw.split("|")
        valid_shape = len(words) == 1 and len(parts) == len(table["literal"])
        cells = dict(zip(table["literal"], parts, strict=True)) if valid_shape else {}
        self.rows.append(
            {
                "page": page_index,
                "table": table["index"],
                "row": table["row"],
                "description": cells.get("description", "").strip(),
            }
        )
        _bound(len(self.rows), self.limits.max_rows)
        for role in ("date", "currency", "amount"):
            self.field(
                f"rows.{page_index}.{table['index']}.{table['row']}.{role}",
                role,
                cells.get(role, raw),
                words,
                page_index,
                ambiguous=not valid_shape,
            )

    def arithmetic(self):
        certain = {
            path: field["value"]
            for path, field in self.fields.items()
            if field["status"] == "certain"
        }

        def equal_sum(values, expected):
            parts = [value.partition(".") for value in [*values, expected]]
            scale = max(len(part[2]) for part in parts)
            units = [int(whole + fraction.ljust(scale, "0")) for whole, _, fraction in parts]
            return sum(units[:-1]) == units[-1]

        roles = ["header." + role for role in ("subtotal", "tax", "total")]
        if all(role in certain for role in roles) and not equal_sum(
            [certain[roles[0]], certain[roles[1]]], certain[roles[2]]
        ):
            self.diagnostic("arithmetic_mismatch")
        prefixes = [f"rows.{row['page']}.{row['table']}.{row['row']}." for row in self.rows]
        if not all(
            prefix + role in certain for prefix in prefixes for role in ("currency", "amount")
        ):
            return
        for currency in sorted(_CURRENCIES):
            start, end = (
                "header." + role + "_" + currency.lower()
                for role in ("opening_balance", "closing_balance")
            )
            if start in certain and end in certain:
                amounts = [
                    certain[prefix + "amount"]
                    for prefix in prefixes
                    if certain[prefix + "currency"] == currency
                ]
                if not equal_sum([certain[start], *amounts], certain[end]):
                    self.diagnostic("arithmetic_mismatch")

    def result(self):
        self.arithmetic()
        if not self.fields:
            self.diagnostic("no_fields")
        return {
            "version": 1,
            "status": "review" if self.diagnostics else "parsed",
            "reason": self.diagnostics[0] if self.diagnostics else None,
            "fields": list(self.fields.values()),
            "rows": self.rows,
            "diagnostics": self.diagnostics,
        }


def parse_document(pages: tuple[TextPage, ...], *, limits: ParserLimits = _DEFAULT) -> dict:
    """Parse only visible text/geometry; certainty never authorizes financial posting."""
    try:
        _validate(pages, limits)
        parser = _Parser(limits)
        for index, page in enumerate(pages):
            parser.page(page, index)
        result = parser.result()
        _bound(_bytes(result), limits.output_bytes)
        return result
    except (_Review, UnicodeError, ValueError, OverflowError) as error:
        reason = error.code if isinstance(error, _Review) else "invalid_input"
        result = {
            "version": 1,
            "status": "review",
            "reason": reason,
            "fields": [],
            "rows": [],
            "diagnostics": [reason],
        }
        if isinstance(limits, ParserLimits) and _bytes(result) > limits.output_bytes:
            result.update(reason="parser_limit", diagnostics=["parser_limit"])
        return result
