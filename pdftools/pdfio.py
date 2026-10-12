import pymupdf

from .errors import PdfToolsError


def open_pdf(name: str, data: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise PdfToolsError(f"«{name}»: файл повреждён или это не PDF") from exc
    if doc.needs_pass:
        raise PdfToolsError(f"«{name}»: PDF защищён паролем")
    if doc.page_count == 0:
        raise PdfToolsError(f"«{name}»: в PDF нет страниц")
    return doc
