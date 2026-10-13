import hashlib
import html
import io
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from ebooklib import epub

from .errors import PdfToolsError
from .pdfio import open_pdf

MARGIN = 0.08            # доля высоты страницы сверху/снизу, где ищем колонтитулы
MIN_IMAGE_PX = 50        # картинки меньше — декор, пропускаем
HEADING_RATIO = 1.2      # во сколько раз шрифт крупнее основного, чтобы считаться заголовком
H1_RATIO = 1.6
MAX_HEADING_LEN = 150
MAX_HEAD_LEN = 80        # колонтитул — короткий блок
SCAN_COVER = 0.7         # картинка шире этой доли страницы при наличии текста — фон скана
CHUNK_ITEMS = 150        # максимум элементов в одной главе-файле
SENTENCE_END = tuple('.!?:;…»"”)')
PAGE_NUMBER = re.compile(r"[-–—\s]*[0-9]{1,4}[-–—\s]*")
SCAN_MESSAGE = "«{name}»: в PDF нет текста (скан). Распознавание текста пока не поддерживается"
IMAGE_TYPES = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}
CSS = """
body { font-family: serif; line-height: 1.5; }
p { text-indent: 1.5em; margin: 0 0 0.3em; }
h1, h2 { text-align: center; margin: 1em 0 0.6em; }
img { display: block; max-width: 100%; margin: 0.6em auto; }
"""


@dataclass
class Block:
    kind: str  # "text" | "image"
    text: str = ""
    size: float = 0.0
    image: bytes = b""
    ext: str = ""
    in_margin: bool = False


@dataclass
class Item:
    kind: str  # "p" | "h" | "img"
    text: str = ""
    level: int = 0
    image: bytes = b""
    ext: str = ""


def pdf_to_epub(data: bytes, title: str | None = None, author: str | None = None, name: str = "book.pdf") -> bytes:
    """Reflowable EPUB: абзацы, заголовки-главы, картинки; без колонтитулов."""
    doc = open_pdf(name, data)
    try:
        pages = _extract(doc)
        if not any(b.kind == "text" for blocks in pages for b in blocks):
            raise PdfToolsError(SCAN_MESSAGE.format(name=name))
        blocks = _drop_running_heads(pages)
        if not any(b.kind == "text" for b in blocks):
            raise PdfToolsError(SCAN_MESSAGE.format(name=name))
        items = _to_items(blocks)
        meta = doc.metadata or {}
        book_title = title or (meta.get("title") or "").strip() or Path(name).stem
        book_author = author or (meta.get("author") or "").strip() or None
        return _build_epub(items, book_title, book_author)
    except PdfToolsError:
        raise
    except Exception as exc:
        raise PdfToolsError(f"«{name}»: не удалось сконвертировать в EPUB") from exc


def _join(left: str, right: str) -> str:
    if left.endswith("-") and right[:1].islower():
        return left[:-1] + right
    return f"{left} {right}"


def _block_text(raw: dict) -> tuple[str, float]:
    text = ""
    sizes: Counter[float] = Counter()
    for line in raw["lines"]:
        for span in line["spans"]:
            sizes[round(span["size"], 1)] += len(span["text"])
        line_text = "".join(span["text"] for span in line["spans"]).strip()
        if line_text:
            text = _join(text, line_text) if text else line_text
    return text, (sizes.most_common(1)[0][0] if sizes else 0.0)


def _area(bbox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _extract(doc: pymupdf.Document) -> list[list[Block]]:
    pages = []
    for page in doc:
        height = page.rect.height
        page_area = page.rect.width * height
        blocks = []
        raws = page.get_text("dict", sort=True)["blocks"]
        has_text = any(r["type"] == 0 and _block_text(r)[0] for r in raws)
        for raw in raws:
            y0, y1 = raw["bbox"][1], raw["bbox"][3]
            in_margin = y1 <= height * MARGIN or y0 >= height * (1 - MARGIN)
            if raw["type"] == 0:
                text, size = _block_text(raw)
                if text:
                    blocks.append(Block("text", text=text, size=size, in_margin=in_margin))
            elif has_text and _area(raw["bbox"]) > page_area * SCAN_COVER:
                continue  # скан-подложка под распознанным текстом
            elif raw.get("width", 0) >= MIN_IMAGE_PX and raw.get("height", 0) >= MIN_IMAGE_PX:
                blocks.append(Block("image", image=raw["image"], ext=raw["ext"]))
        pages.append(blocks)
    return pages


def _signature(text: str) -> str:
    return re.sub(r"[0-9]+", "#", text.casefold()).strip()


def _drop_running_heads(pages: list[list[Block]]) -> list[Block]:
    """Убирает колонтитулы (повторяются в полях на многих страницах) и номера страниц."""
    counts: Counter[str] = Counter()
    for blocks in pages:
        counts.update({_signature(b.text) for b in blocks if b.kind == "text" and b.in_margin and len(b.text) <= MAX_HEAD_LEN})
    # настоящие книги чередуют колонтитулы (название книги / название главы), поэтому порог низкий
    threshold = max(3, math.ceil(len(pages) * 0.05)) if len(pages) >= 3 else 2
    kept = []
    for blocks in pages:
        for b in blocks:
            if b.kind == "text" and b.in_margin and (
                (len(b.text) <= MAX_HEAD_LEN and counts[_signature(b.text)] >= threshold) or PAGE_NUMBER.fullmatch(b.text)
            ):
                continue
            kept.append(b)
    return kept


def _heading_level(block: Block, body_size: float) -> int:
    if len(block.text) > MAX_HEADING_LEN or block.size < body_size * HEADING_RATIO:
        return 0
    return 1 if block.size >= body_size * H1_RATIO else 2


def _to_items(blocks: list[Block]) -> list[Item]:
    sizes: Counter[float] = Counter()
    for b in blocks:
        if b.kind == "text":
            sizes[b.size] += len(b.text)
    body_size = sizes.most_common(1)[0][0]

    items: list[Item] = []
    seen_images = set()
    for b in blocks:
        if b.kind == "image":
            digest = hashlib.sha256(b.image).hexdigest()
            if digest not in seen_images:
                seen_images.add(digest)
                items.append(Item("img", image=b.image, ext=b.ext))
            continue
        level = _heading_level(b, body_size)
        if level:
            items.append(Item("h", text=b.text, level=level))
            continue
        prev = items[-1] if items else None
        # абзац, разорванный страницей или блоком: нет точки в конце, продолжение с маленькой буквы
        if prev and prev.kind == "p" and not prev.text.endswith(SENTENCE_END) and b.text[:1].islower():
            prev.text = _join(prev.text, b.text)
        else:
            items.append(Item("p", text=b.text))
    return items


def _language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum(1 for c in letters if "а" <= c.lower() <= "я" or c in "ёЁ")
    return "ru" if letters and cyrillic / len(letters) > 0.3 else "en"


def _image_item(item: Item, n: int) -> epub.EpubItem | None:
    ext, data = item.ext.lower(), item.image
    if ext not in IMAGE_TYPES:  # jpx, jbig2 и т.п. читалки не понимают — переводим в PNG
        try:
            data, ext = pymupdf.Pixmap(data).tobytes("png"), "png"
        except Exception:
            return None
    return epub.EpubItem(
        uid=f"img{n}", file_name=f"images/img_{n:03}.{ext}", media_type=IMAGE_TYPES[ext], content=data
    )


def _build_epub(items: list[Item], title: str, author: str | None) -> bytes:
    lang = _language(" ".join(i.text for i in items[:300]))
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    css = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=CSS)
    book.add_item(css)

    h1 = sum(1 for i in items if i.kind == "h" and i.level == 1)
    h2 = sum(1 for i in items if i.kind == "h" and i.level == 2)
    split_level = 1 if h1 >= 2 else 2 if h2 >= 2 else 0
    sections: list[tuple[str, list[str]]] = [(title, [])]
    base_title, part = title, 1
    image_n = 0

    def add(markup: str) -> None:
        nonlocal part
        if len(sections[-1][1]) >= CHUNK_ITEMS:
            part += 1
            sections.append((f"{base_title} — часть {part}", []))
        sections[-1][1].append(markup)

    for item in items:
        if item.kind == "h":
            if item.level == split_level:
                if sections[-1][1]:
                    sections.append((item.text, []))
                else:
                    sections[-1] = (item.text, [])
                base_title, part = item.text, 1
            add(f"<h{item.level}>{html.escape(item.text)}</h{item.level}>")
        elif item.kind == "p":
            add(f"<p>{html.escape(item.text)}</p>")
        else:
            image_n += 1
            image = _image_item(item, image_n)
            if image:
                book.add_item(image)
                add(f'<img src="{image.file_name}" alt=""/>')

    chapters = []
    for n, (chapter_title, parts) in enumerate(sections, 1):
        if not parts:
            continue
        chapter = epub.EpubHtml(title=chapter_title, file_name=f"chapter_{n:03}.xhtml", lang=lang)
        chapter.content = "\n".join(parts)
        chapter.add_item(css)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = chapters
    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
