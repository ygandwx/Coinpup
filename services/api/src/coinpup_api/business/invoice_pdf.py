"""Deterministic rendering core; call only in the bounded Invoice worker.

No number allocation, database access, original-file writes or issuance endpoint. Logo bytes
must match the immutable snapshot and pass bounded decoding; never silently omit a Logo.
"""

import io

from coinpup_api.business.invoice_fonts import STATIC_FONTS, load_invoice_fonts
from coinpup_api.business.invoice_logo import decode_invoice_logo
from coinpup_api.business.invoice_snapshot_reads import read_invoice_render_input
from coinpup_api.ledger.errors import MoneyError

MAX_INVOICE_PDF_BYTES = 8 * 1024 * 1024
MAX_INVOICE_PDF_PAGES = 256

_LABELS = {
    "zh": {
        "title": "商业发票",
        "issuer": "发行方",
        "customer": "客户",
        "name": "名称",
        "legal_name": "法定名称",
        "country_code": "国家",
        "region_code": "地区",
        "company_type": "公司类型",
        "email": "电子邮件",
        "phone": "电话",
        "address": "地址",
        "tax_identifier": "税务标识",
        "issued": "开票日期",
        "due": "到期日期",
        "asset": "资产",
        "network": "网络",
        "token": "代币标识",
        "quantity": "数量",
        "unit_price": "单价",
        "discount_amount": "折扣",
        "tax_rate_percent": "税率 %",
        "net_amount": "净额",
        "tax_amount": "税额",
        "total_amount": "含税额",
        "totals": "合计",
        "notes": "备注",
        "payment": "收款说明",
        "page": "页",
    },
    "en": {
        "title": "COMMERCIAL INVOICE",
        "issuer": "Issuer",
        "customer": "Customer",
        "name": "Name",
        "legal_name": "Legal name",
        "country_code": "Country",
        "region_code": "Region",
        "company_type": "Company type",
        "email": "Email",
        "phone": "Phone",
        "address": "Address",
        "tax_identifier": "Tax identifier",
        "issued": "Issued",
        "due": "Due",
        "asset": "Asset",
        "network": "Network",
        "token": "Token reference",
        "quantity": "Quantity",
        "unit_price": "Unit price",
        "discount_amount": "Discount",
        "tax_rate_percent": "Tax rate %",
        "net_amount": "Net",
        "tax_amount": "Tax",
        "total_amount": "Gross",
        "totals": "Totals",
        "notes": "Notes",
        "payment": "Payment instructions",
        "page": "Page",
    },
}


def _error(code="invoice_render_invalid"):
    return MoneyError(code, "The frozen Invoice could not be rendered safely.")


class _RenderLimit(ValueError):
    """ReportLab may reconstruct an exception with an annotated message."""


class _RenderGlyph(ValueError):
    """Keep the failure type stable without exposing library diagnostics to callers."""


class _BoundedOutput(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_INVOICE_PDF_BYTES:
            raise _RenderLimit()
        return super().write(value)


def render_invoice_text_pdf(
    content, *, expected_sha256, language, font_directory, logo_content=None
):
    """Render only verified immutable bytes, with exact amounts and literal user text.

    The trusted worker must additionally impose process CPU, address-space and wall-clock limits.
    Its caller must authorize the saved reservation; a caller-supplied digest is not authorization.
    This core performs no network requests and does not accept HTML, templates or arbitrary fonts.
    """
    frozen = read_invoice_render_input(content, expected_sha256=expected_sha256)
    if type(language) is not str or language not in _LABELS:
        raise _error()
    data = frozen.decode()
    logo = decode_invoice_logo(logo_content, data["logo"])
    font_bytes = load_invoice_fonts(font_directory)
    try:
        return _render(data, language, font_bytes, logo)
    except MoneyError:
        raise
    except ImportError:
        raise _error("invoice_render_unavailable") from None
    except _RenderGlyph:
        raise _error("invoice_render_glyph") from None
    except (ValueError, TypeError, KeyError, OSError):
        raise _error() from None


def _render(data, language, font_bytes, logo):
    # Optional libraries are imported only by the explicit rendering entry point.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import (
        Flowable,
        KeepTogether,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.doctemplate import LayoutError

    from coinpup_api.business.invoice_pdf_font import InvoiceFont

    names, coverage = [], {}
    for asset, raw in zip(STATIC_FONTS, font_bytes, strict=True):
        name = asset.filename.removesuffix(".ttf")
        font = InvoiceFont(name, io.BytesIO(raw), validate=1)
        pdfmetrics.registerFont(font)
        names.append(name)
        coverage[name] = {code for code, glyph in font.face.charToGlyph.items() if glyph}
    glyphs = {}

    def glyph(character, size):
        key = character, size
        if key not in glyphs:
            name = next((name for name in names if ord(character) in coverage[name]), None)
            if name is None or (ord(character) < 32):
                raise _RenderGlyph()
            glyphs[key] = name, pdfmetrics.stringWidth(character, name, size)
        return glyphs[key]

    class LiteralText(Flowable):
        """Wrap original codepoints; no XML parser, whitespace collapsing or numeric coercion."""

        def __init__(self, text, size=9, *, lines=None):
            super().__init__()
            if type(text) is not str:
                raise _error()
            self.text, self.size, self.leading = text, size, size * 1.4
            self.lines = lines
            self.spaceAfter = 5

        def wrap(self, available, height):
            if self.lines is None:
                lines, line, used = [], [], 0
                for character in self.text:
                    if character == "\n":
                        lines.append(line)
                        line, used = [], 0
                        continue
                    name, width = glyph(character, self.size)
                    if width > available:
                        raise _RenderLimit()
                    if line and used + width > available:
                        lines.append(line)
                        line, used = [], 0
                    line.append((name, character, width))
                    used += width
                lines.append(line)
                self.lines = lines
            self.width, self.height = available, len(self.lines) * self.leading
            return self.width, self.height

        def split(self, available, height):
            self.wrap(available, height)
            count = int(height // self.leading)
            if count < 1:
                return []
            return [
                LiteralText("", self.size, lines=self.lines[:count]),
                LiteralText("", self.size, lines=self.lines[count:]),
            ]

        def draw(self):
            self.canv.setFillColor(colors.HexColor("#18332c"))
            y = self.height - self.size
            for line in self.lines:
                x, index = 0, 0
                while index < len(line):
                    name = line[index][0]
                    letters, advance = [], 0
                    while index < len(line) and line[index][0] == name:
                        letters.append(line[index][1])
                        advance += line[index][2]
                        index += 1
                    self.canv.setFont(name, self.size)
                    self.canv.drawString(x, y, "".join(letters))
                    x += advance
                y -= self.leading

    class BoundedCanvas(Canvas):
        def __init__(self, *args, **kwargs):
            kwargs["initialFontName"] = names[0]
            super().__init__(*args, **kwargs)

        def showPage(self):
            if self.getPageNumber() > MAX_INVOICE_PDF_PAGES:
                raise _RenderLimit()
            super().showPage()

    labels = _LABELS[language]
    profile, document = data["profile"], data["document"]["current"]["document"]
    story = [LiteralText(labels["title"], 20), LiteralText(data["number"]["number"], 12)]
    if logo is not None:
        from PIL import Image
        from reportlab.lib.utils import ImageReader

        class LogoGraphic(Flowable):
            def __init__(self):
                super().__init__()
                ratio = min(144 / logo.width, 72 / logo.height)
                self.width, self.height = logo.width * ratio, logo.height * ratio
                self.image = ImageReader(
                    Image.frombytes("RGBA", (logo.width, logo.height), logo.pixels)
                )
                self.spaceAfter = 8

            def draw(self):
                self.canv.drawImage(self.image, 0, 0, self.width, self.height, mask="auto")

        story = [KeepTogether([LogoGraphic(), *story])]

    def field(label, value):
        if value is not None:
            story.append(LiteralText(labels[label] + ": " + value))

    def party(title, snapshot, fields):
        story.append(LiteralText(labels[title], 12))
        if not set(snapshot) <= set(fields) | {"id", "version", "role"}:
            raise _error()
        for key in fields:
            field(key, snapshot.get(key))

    party(
        "issuer",
        document["issuer_snapshot"],
        (
            "name",
            "legal_name",
            "country_code",
            "region_code",
            "company_type",
        ),
    )
    if profile["issuer_extra_text"] is not None:
        story.append(LiteralText(profile["issuer_extra_text"]))
    party(
        "customer",
        document["party_snapshot"],
        (
            "name",
            "legal_name",
            "email",
            "phone",
            "address",
            "tax_identifier",
        ),
    )
    field("issued", document["issue_date"])
    field("due", document["due_date"])
    asset = data["document"]["asset"]
    field("asset", asset["code"])
    field("network", asset["network"])
    field("token", asset["token_reference"])
    story.append(Spacer(1, 12))
    keys = (
        "quantity",
        "unit_price",
        "discount_amount",
        "tax_rate_percent",
        "net_amount",
        "tax_amount",
        "total_amount",
    )
    for position, row in enumerate(document["lines"], 1):
        description = LiteralText(str(position) + ". " + row["description"])
        cells = [LiteralText(labels[key] + ": " + row[key], 8) for key in keys]
        cells.append(LiteralText(""))
        table = Table(
            [cells[i : i + 2] for i in range(0, len(cells), 2)],
            colWidths=[(A4[0] - 88) / 2] * 2,
            hAlign="LEFT",
            splitByRow=0,
        )
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), names[0]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        # Keep ordinary rows and their complete amounts together. An exceptionally long
        # description can split across pages, but the amount table never splits internally.
        story.append(KeepTogether([description, table, Spacer(1, 10)]))
    story.append(LiteralText(labels["totals"], 12))
    for key in ("net_amount", "tax_amount", "total_amount"):
        field(key, document[key])
    field("notes", document["notes"])
    field("payment", profile["payment_instructions"])
    if profile["footer"] is not None:
        story.append(LiteralText(profile["footer"]))

    def footer(canvas, page):
        canvas.saveState()
        canvas.setFont(names[0], 8)
        canvas.drawRightString(A4[0] - 44, 25, labels["page"] + " " + str(page.page))
        canvas.restoreState()

    output = _BoundedOutput()
    template = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=44,
        leftMargin=44,
        topMargin=44,
        bottomMargin=44,
        invariant=1,
        pageCompression=1,
        title="Coinpup commercial Invoice",
        author="Coinpup",
    )
    try:
        template.build(story, onFirstPage=footer, onLaterPages=footer, canvasmaker=BoundedCanvas)
    except (LayoutError, _RenderLimit):
        raise _error("invoice_render_limit") from None
    return output.getvalue()
