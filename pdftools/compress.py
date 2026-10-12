import pymupdf

from .errors import PdfToolsError
from .pdfio import open_pdf

# уровень → (максимальный DPI картинок, качество JPEG)
LEVELS = {"light": (200, 85), "medium": (150, 70), "strong": (100, 50)}


def compress_pdf(data: bytes, level: str = "medium", name: str = "PDF") -> bytes:
    """Пережимает картинки внутри PDF; текст и вектор не трогает."""
    if level not in LEVELS:
        raise PdfToolsError(f"Неизвестный уровень сжатия: {level}")
    dpi, quality = LEVELS[level]
    doc = open_pdf(name, data)
    try:
        # уменьшаем только картинки заметно плотнее целевого DPI
        doc.rewrite_images(dpi_threshold=int(dpi * 1.25), dpi_target=dpi, quality=quality)
        result = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    except Exception as exc:
        raise PdfToolsError(f"«{name}»: не удалось сжать файл") from exc
    return result if len(result) < len(data) else data
