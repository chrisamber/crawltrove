"""Tiny dependency-free PDF fixtures for document parser tests."""


def make_pdf(*page_texts: str, title: str = "") -> bytes:
    """Build a basic PDF 1.4 document with one Helvetica text stream per page."""
    if not page_texts:
        page_texts = ("",)

    page_count = len(page_texts)
    font_obj = 3 + page_count * 2
    info_obj = font_obj + 1
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Count {page_count} /Kids ["
            + " ".join(f"{3 + i * 2} 0 R" for i in range(page_count))
            + "] >>"
        ).encode("ascii"),
        font_obj: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        info_obj: f"<< /Title ({_escape(title)}) >>".encode("ascii"),
    }
    for i, text in enumerate(page_texts):
        page_obj = 3 + i * 2
        stream_obj = page_obj + 1
        stream = f"BT /F1 12 Tf 72 700 Td ({_escape(text)}) Tj ET".encode("ascii")
        objects[page_obj] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {stream_obj} 0 R >>"
        ).encode("ascii")
        objects[stream_obj] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, info_obj + 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(objects[number])
        result.extend(b"\nendobj\n")

    xref = len(result)
    result.extend(f"xref\n0 {info_obj + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {info_obj + 1} /Root 1 0 R /Info {info_obj} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
