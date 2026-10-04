"""Tiny explicitly fictional PDFs; no recognition answers or real documents."""

from io import BytesIO


def document(contents=b"", *, mutate=None, pages=1, compressed=False, password=None):
    if isinstance(contents, list):
        if mutate is not None or pages != 1 or compressed or password is not None:
            raise ValueError("Unsupported fictional array fixture options")
        references = " ".join(f"{index} 0 R" for index in range(4, 4 + len(contents)))
        return raw_document(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] /Resources << "
                b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
                + f"/Contents [{references}] >>".encode(),
                *(stream_bytes(content) for content in contents),
            ]
        )
    from pypdf import PageObject, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    with PdfWriter() as writer, BytesIO() as output:
        writer.add_metadata({"/Title": "FICTIONAL PDF PROBE TEST"})
        for _ in range(pages):
            page = PageObject.create_blank_page(width=240, height=100)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            stream.set_data(contents)
            page[NameObject("/Contents")] = stream.flate_encode() if compressed else stream
            if mutate is not None:
                mutate(page, writer)
            writer.add_page(page)
        if password is not None:
            writer.encrypt(password)
        writer.write(output)
        return output.getvalue()


def form(contents=b"", resources=None):
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    stream = DecodedStreamObject()
    stream.set_data(contents)
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 240, 100)]),
            NameObject("/Resources"): resources or DictionaryObject(),
        }
    )
    return stream


def raw_document(objects):
    """Handwritten object references enable deliberately cyclic/malformed structures."""
    output = bytearray(b"%PDF-1.7\n% FICTIONAL PDF PROBE TEST\n")
    offsets = [0]
    for index, payload in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + payload + b"\nendobj\n")
    start = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(output)


def stream_bytes(content, extra=b""):
    return (
        f"<< /Length {len(content)} ".encode() + extra + b">>\nstream\n" + content + b"\nendstream"
    )


def image_document(*, encoding="flate", mode="RGB", declared_size=None, mutate=None):
    """A real 4x3 fictional image, with optional deliberately inconsistent metadata."""
    from PIL import Image
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    with Image.new(mode, (4, 3), (220, 40, 60) if mode == "RGB" else 140) as image:
        with BytesIO() as output:
            image.save(output, format="JPEG")
            jpeg = output.getvalue()
        pixels = image.tobytes()
    stream = DecodedStreamObject()
    stream.set_data(jpeg if encoding == "jpeg" else pixels)
    width, height = declared_size or (4, 3)
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB" if mode == "RGB" else "/DeviceGray"),
        }
    )
    if encoding == "jpeg":
        stream[NameObject("/Filter")] = NameObject("/DCTDecode")
    elif encoding == "flate":
        stream = stream.flate_encode()
    else:
        raise ValueError("Unsupported fictional image encoding")

    def install(page, writer):
        page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/I"): stream})
        if mutate is not None:
            mutate(page, writer)

    return document(b"q 60 0 0 40 10 10 cm /I Do Q", mutate=install)


def geometry_document(*, inherited=b"", page_attributes=b""):
    return raw_document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 " + inherited + b">>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] /Resources <<>> "
            b"/Contents 4 0 R " + page_attributes + b">>",
            stream_bytes(b"q Q"),
        ]
    )
