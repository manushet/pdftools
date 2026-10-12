from collections.abc import Callable
from pathlib import Path
import os

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
    paths = list_files(folder, IMAGE_EXTS)
    return _write_combined(folder, paths, images_to_pdf, _combined_name(folder))


def folder_merge(folder: Path) -> list[Path]:
    folder = Path(folder).resolve()
    paths = list_files(folder, PDF_EXTS)
    return _write_combined(folder, paths, merge_pdfs, _combined_name(folder))


def folder_compress(folder: Path, level: str = "medium") -> list[Path]:
    if level not in LEVELS:
        raise PdfToolsError(f"Неизвестный уровень сжатия: {level}")
    return _each_pdf(Path(folder).resolve(), ".pdf", lambda src, data: compress_pdf(data, level, src.name))


def folder_to_epub(folder: Path) -> list[Path]:
    return _each_pdf(Path(folder).resolve(), ".epub", lambda src, data: pdf_to_epub(data, name=src.name))


def _read(paths: list[Path]) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Read files, catching OSError and returning (items, read_errors)."""
    items = []
    errors = []
    for p in paths:
        try:
            items.append((p.name, p.read_bytes()))
        except OSError:
            errors.append(f"«{p.name}»: не удалось прочитать файл")
    return items, errors


def _combined_name(folder: Path) -> str:
    return f"{folder.name or 'результат'}.pdf"


def _write_combined(folder: Path, paths: list[Path], combine: Callable, output_name: str) -> list[Path]:
    """Combine files with per-file error detection on failure."""
    items, read_errors = _read(paths)
    if read_errors:
        raise PdfToolsError(_format_errors(read_errors))

    try:
        result = combine(items)
    except PdfToolsError as exc:
        # Try each file individually to identify per-file errors
        individual_errors = []
        for item in items:
            try:
                combine([item])
            except PdfToolsError as e:
                individual_errors.append(str(e))

        if individual_errors:
            raise PdfToolsError(_format_errors(individual_errors)) from exc
        # If no individual file failed, use original error
        raise

    return _write(folder, {output_name: result})


def _write(folder: Path, outputs: dict[str, bytes]) -> list[Path]:
    """Write outputs to temp files, then atomically replace them."""
    out = folder / OUTPUT_DIR
    temp_files = []

    try:
        out.mkdir(exist_ok=True)
    except OSError as exc:
        raise PdfToolsError(f"Не удалось сохранить результат: {exc}") from exc

    try:
        for name, data in outputs.items():
            target = out / name
            temp_path = out / (name + ".tmp")
            try:
                temp_path.write_bytes(data)
                temp_files.append(temp_path)
                os.replace(str(temp_path), str(target))
            except OSError as exc:
                # Delete temp files on failure
                for tf in temp_files:
                    try:
                        tf.unlink()
                    except OSError:
                        pass
                raise PdfToolsError(f"Не удалось сохранить результат: {exc}") from exc

        return [out / name for name in outputs.keys()]
    except PdfToolsError:
        raise
    except Exception as exc:
        # Clean up any remaining temp files
        for tf in temp_files:
            try:
                tf.unlink()
            except OSError:
                pass
        raise PdfToolsError(f"Не удалось сохранить результат: {exc}") from exc


def _each_pdf(folder: Path, suffix: str, convert: Callable[[Path, bytes], bytes]) -> list[Path]:
    """Обрабатывает все PDF в памяти; если хоть один упал — ничего не пишет."""
    outputs: dict[str, bytes] = {}
    errors: list[str] = []
    for src in list_files(folder, PDF_EXTS):
        try:
            outputs[f"{src.stem}{suffix}"] = convert(src, src.read_bytes())
        except OSError:
            errors.append(f"«{src.name}»: не удалось прочитать файл")
        except PdfToolsError as exc:
            errors.append(str(exc))
    if errors:
        raise PdfToolsError(_format_errors(errors))
    return _write(folder, outputs)


def _format_errors(errors: list[str]) -> str:
    """Format error list for display."""
    return "Обработка отменена, ничего не сохранено. Проблемные файлы:\n" + "\n".join(errors)
