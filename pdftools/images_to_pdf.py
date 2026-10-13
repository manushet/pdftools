import io

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import PdfToolsError
from .progress import Progress, no_progress

_EXIF_ORIENTATION = 0x0112
_JPEG_FORMATS = ("JPEG", "MPO")


def images_to_pdf(items: list[tuple[str, bytes]], progress: Progress = no_progress) -> bytes:
    """Склеивает картинки в один PDF: страница на картинку, размер страницы = размер картинки."""
    if not items:
        raise PdfToolsError("Нет картинок для конвертации")
    doc = pymupdf.open()
    total = len(items)
    for i, (name, data) in enumerate(items):
        progress(i, total, f"Картинка {i + 1} из {total}: {name}")
        try:
            stream, width, height = _prepare(name, data)
            page = doc.new_page(width=width, height=height)
            page.insert_image(page.rect, stream=stream)
        except PdfToolsError:
            raise
        except Exception as exc:
            raise PdfToolsError(f"«{name}»: не удалось обработать картинку") from exc
    progress(total, total, "Сохранение PDF…")
    return doc.tobytes(garbage=3, deflate=True)


def _prepare(name: str, data: bytes) -> tuple[bytes, int, int]:
    """Возвращает байты картинки, пригодные для PDF, и её размер с учётом поворота."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # у GIF загружается только первый кадр
    except Exception as exc:
        raise PdfToolsError(f"«{name}»: не удалось открыть как картинку") from exc

    source_format = img.format
    orientation = img.getexif().get(_EXIF_ORIENTATION, 1)
    if source_format in _JPEG_FORMATS and img.mode in ("RGB", "L") and orientation == 1:
        return data, img.width, img.height  # без перекодирования — без потерь

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    if source_format in _JPEG_FORMATS:
        img.save(buf, "JPEG", quality=95)
    else:
        img.save(buf, "PNG")
    return buf.getvalue(), img.width, img.height
