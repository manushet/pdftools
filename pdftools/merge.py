import pymupdf

from .errors import PdfToolsError
from .pdfio import open_pdf


def merge_pdfs(items: list[tuple[str, bytes]]) -> bytes:
    """Склеивает PDF в порядке списка, сохраняя закладки."""
    if not items:
        raise PdfToolsError("Нет PDF-файлов для объединения")
    out = pymupdf.open()
    toc = []
    for name, data in items:
        try:
            src = open_pdf(name, data)
            offset = out.page_count
            toc.extend([level, title, page + offset] for level, title, page in src.get_toc() if page > 0)
            out.insert_pdf(src)
            src.close()
        except PdfToolsError:
            raise
        except Exception as exc:
            raise PdfToolsError(f"«{name}»: не удалось добавить файл") from exc
    try:
        out.set_toc(toc)
    except ValueError:
        pass  # кривое оглавление в исходнике — объединяем без закладок
    return out.tobytes(garbage=3, deflate=True)
