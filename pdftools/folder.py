from collections.abc import Callable
from pathlib import Path

from .compress import LEVELS, compress_pdf
from .errors import PdfToolsError
from .images_to_pdf import images_to_pdf
from .merge import merge_pdfs
from .naming import natural_key
from .pdf_to_epub import pdf_to_epub

OUTPUT_DIR = "готово"
IMAGE_EXTS = {".jpg", ".jpeg", ".gif", ".png", ".webp"}
PDF_EXTS = {".pdf"}


def list_files(folder: Path, exts: set[str]) -> list[Path]:
    """Файлы верхнего уровня с нужными расширениями, в естественном порядке."""
    if not folder.is_dir():
        raise PdfToolsError(f"Папка не найдена: {folder}")
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        raise PdfToolsError(f"В папке нет подходящих файлов ({', '.join(sorted(exts))})")
    return sorted(files, key=lambda p: natural_key(p.name))


def folder_images_to_pdf(folder: Path) -> list[Path]:
    folder = Path(folder).resolve()
    return _write(folder, {_combined_name(folder): images_to_pdf(_read(list_files(folder, IMAGE_EXTS)))})


def folder_merge(folder: Path) -> list[Path]:
    folder = Path(folder).resolve()
    return _write(folder, {_combined_name(folder): merge_pdfs(_read(list_files(folder, PDF_EXTS)))})


def folder_compress(folder: Path, level: str = "medium") -> list[Path]:
    if level not in LEVELS:
        raise PdfToolsError(f"Неизвестный уровень сжатия: {level}")
    return _each_pdf(Path(folder).resolve(), ".pdf", lambda src, data: compress_pdf(data, level, src.name))


def folder_to_epub(folder: Path) -> list[Path]:
    return _each_pdf(Path(folder).resolve(), ".epub", lambda src, data: pdf_to_epub(data, name=src.name))


def _read(paths: list[Path]) -> list[tuple[str, bytes]]:
    return [(p.name, p.read_bytes()) for p in paths]


def _combined_name(folder: Path) -> str:
    return f"{folder.name or 'результат'}.pdf"


def _write(folder: Path, outputs: dict[str, bytes]) -> list[Path]:
    out = folder / OUTPUT_DIR
    out.mkdir(exist_ok=True)
    created = []
    for name, data in outputs.items():
        target = out / name
        target.write_bytes(data)
        created.append(target)
    return created


def _each_pdf(folder: Path, suffix: str, convert: Callable[[Path, bytes], bytes]) -> list[Path]:
    """Сначала обрабатывает все PDF в памяти; если хоть один упал — ничего не пишет."""
    outputs: dict[str, bytes] = {}
    errors: list[str] = []
    for src in list_files(folder, PDF_EXTS):
        try:
            outputs[f"{src.stem}{suffix}"] = convert(src, src.read_bytes())
        except PdfToolsError as exc:
            errors.append(str(exc))
    if errors:
        raise PdfToolsError("Обработка отменена, ничего не сохранено. Проблемные файлы:\n" + "\n".join(errors))
    return _write(folder, outputs)
