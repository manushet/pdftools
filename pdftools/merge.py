import pymupdf

from .errors import PdfToolsError
from .pdfio import open_pdf
from .progress import Progress, no_progress


def merge_pdfs(items: list[tuple[str, bytes]], progress: Progress = no_progress) -> bytes:
    """Склеивает PDF в порядке списка, сохраняя закладки."""
    if not items:
        raise PdfToolsError("Нет PDF-файлов для объединения")
    out = pymupdf.open()
    toc = []
    total = len(items)
    for i, (name, data) in enumerate(items):
        progress(i, total, f"Файл {i + 1} из {total}: {name}")
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
    progress(total, total, "Сохранение PDF…")
    return out.tobytes(garbage=3, deflate=True)
