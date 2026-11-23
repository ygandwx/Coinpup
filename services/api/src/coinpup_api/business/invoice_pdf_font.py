"""ReportLab font subsets for the optional commercial Invoice renderer."""

import re

from reportlab.pdfbase.ttfonts import TTFont

# In the pinned renderer a supplementary code point is emitted as five or six hex
# digits. PDF ToUnicode destinations must be UTF-16BE (ISO 32000-1, 9.10.3).
_SCALAR_ROW = re.compile(r"^(<[0-9A-Fa-f]{2}> )<([0-9A-Fa-f]{5,6})>$", re.MULTILINE)


class InvoiceFont(TTFont):
    def addObjects(self, doc):
        # Keep ReportLab's original subset, widths, glyph outlines and BMP maps. Only
        # adjust this document's affected lookup streams, before compression and hashing.
        state = self.state.get(doc)
        affected = (
            [
                self.getSubsetInternalName(i, doc)[1:]
                for i, subset in enumerate(state.subsets)
                if any(code > 0xFFFF for code in subset)
            ]
            if state is not None
            else []
        )
        super().addObjects(doc)
        for name in affected:
            font = doc.idToObject[name]
            stream = doc.idToObject[font.ToUnicode.name]
            stream.content = _SCALAR_ROW.sub(_utf16_destination, stream.content)


def _utf16_destination(match):
    char = chr(int(match[2], 16))
    return f"{match[1]}<{char.encode('utf-16-be').hex().upper()}>"
