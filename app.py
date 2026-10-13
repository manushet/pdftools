"""Локальный веб-интерфейс PDF Tools. Запуск: python app.py"""
import logging
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from pdftools import folder
from pdftools.compress import compress_pdf
from pdftools.errors import PdfToolsError
from pdftools.images_to_pdf import images_to_pdf
from pdftools.merge import merge_pdfs
from pdftools.pdf_to_epub import pdf_to_epub
from pdftools.progress import Progress

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("pdftools")
app = FastAPI(title="PDF Tools")
_LOCK = threading.Lock()  # PyMuPDF не потокобезопасен

ALLOWED_HOSTS = {f"{HOST}:{PORT}", "localhost:8765"}
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", "http://localhost:8765"}


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Check Host header
        host = request.headers.get("host", "").lower()
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse(status_code=403, content={"error": "Запрос отклонён"})

        # For non-GET requests, check Origin header if present
        if request.method != "GET":
            origin = request.headers.get("origin", "").lower()
            if origin and origin not in ALLOWED_ORIGINS:
                return JSONResponse(status_code=403, content={"error": "Запрос отклонён"})

        return await call_next(request)


app.add_middleware(SecurityMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "Не хватает данных в запросе (файлы не выбраны?)"})


@app.exception_handler(HTTPException)
async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(PdfToolsError)
async def _user_error(request: Request, exc: PdfToolsError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unexpected error")
    return JSONResponse(status_code=500, content={"error": "Внутренняя ошибка, подробности в консоли."})


def _file_response(data: bytes, filename: str, media_type: str, extra: dict[str, str] | None = None) -> Response:
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}", **(extra or {})}
    return Response(data, media_type=media_type, headers=headers)


async def _read_all(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [(f.filename or "file", await f.read()) for f in files]


def _stem(filename: str | None) -> str:
    return Path(filename or "").stem or "result"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@dataclass
class Job:
    """Фоновая обработка: страница опрашивает её состояние и забирает результат."""
    label: str = "В очереди…"
    done: int = 0
    total: int = 0
    state: str = "running"  # running | done | error
    error: str = ""
    data: bytes = b""
    filename: str = ""
    media_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    finished_at: float = 0.0


JOB_TTL = 600  # секунд: незабранные результаты удаляются, чтобы не держать память
_JOBS: dict[str, Job] = {}


def _forget_old_jobs() -> None:
    now = time.monotonic()
    for job_id, job in list(_JOBS.items()):
        if job.finished_at and now - job.finished_at > JOB_TTL:
            _JOBS.pop(job_id, None)


def _start(task: Callable[[Job, Progress], None]) -> dict:
    """Запускает task(job, progress) в фоне и сразу возвращает номер задачи."""
    _forget_old_jobs()
    job_id = uuid.uuid4().hex
    job = _JOBS[job_id] = Job()

    def progress(done: int, total: int, label: str) -> None:
        job.done, job.total, job.label = done, total, label

    def worker() -> None:
        try:
            with _LOCK:  # PyMuPDF не потокобезопасен: задачи идут по одной
                task(job, progress)
            job.state = "done"
        except PdfToolsError as exc:
            job.error, job.state = str(exc), "error"
        except Exception:
            log.exception("Unexpected error")
            job.error, job.state = "Внутренняя ошибка, подробности в консоли.", "error"
        job.finished_at = time.monotonic()

    threading.Thread(target=worker, daemon=True).start()
    return {"job": job_id}


def _set_file(job: Job, data: bytes, filename: str, media_type: str, headers: dict[str, str] | None = None) -> None:
    job.data, job.filename, job.media_type, job.headers = data, filename, media_type, headers or {}


def _get_job(job_id: str) -> Job:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена (устарела?) — запусти обработку ещё раз")
    return job


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str) -> dict:
    job = _get_job(job_id)
    return {
        "state": job.state, "done": job.done, "total": job.total, "label": job.label,
        "error": job.error, "created": job.created,
    }


@app.get("/api/jobs/{job_id}/result")
async def api_job_result(job_id: str) -> Response:
    job = _get_job(job_id)
    if job.state != "done" or not job.filename:
        raise HTTPException(status_code=409, detail="Результат ещё не готов")
    _JOBS.pop(job_id, None)
    return _file_response(job.data, job.filename, job.media_type, job.headers)


@app.post("/api/images-to-pdf")
async def api_images_to_pdf(files: Annotated[list[UploadFile], File()]) -> dict:
    items = await _read_all(files)
    return _start(lambda job, progress: _set_file(
        job, images_to_pdf(items, progress), "картинки.pdf", "application/pdf"))


@app.post("/api/merge")
async def api_merge(files: Annotated[list[UploadFile], File()]) -> dict:
    items = await _read_all(files)
    return _start(lambda job, progress: _set_file(
        job, merge_pdfs(items, progress), "объединённый.pdf", "application/pdf"))


@app.post("/api/compress")
async def api_compress(file: Annotated[UploadFile, File()], level: Annotated[str, Form()] = "medium") -> dict:
    original = await file.read()
    name = file.filename or "PDF"

    def task(job: Job, progress: Progress) -> None:
        progress(0, 0, f"Сжатие картинок: {name}")
        data = compress_pdf(original, level, name)
        sizes = {"X-Original-Size": str(len(original)), "X-New-Size": str(len(data))}
        _set_file(job, data, f"{_stem(file.filename)}.pdf", "application/pdf", sizes)

    return _start(task)


@app.post("/api/epub")
async def api_epub(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    author: Annotated[str, Form()] = "",
) -> dict:
    original = await file.read()
    name = file.filename or "book.pdf"
    return _start(lambda job, progress: _set_file(
        job,
        pdf_to_epub(original, title.strip() or None, author.strip() or None, name, progress),
        f"{_stem(file.filename)}.epub",
        "application/epub+zip",
    ))


class FolderRequest(BaseModel):
    path: str
    level: str = "medium"

    def folder(self) -> Path:
        # «Копировать как путь» в Проводнике добавляет кавычки
        cleaned = self.path.strip().strip('"')
        if not cleaned:
            raise PdfToolsError("Укажи папку")
        return Path(cleaned)


FOLDER_ACTIONS = {
    "images": lambda req, progress: folder.folder_images_to_pdf(req.folder(), progress),
    "merge": lambda req, progress: folder.folder_merge(req.folder(), progress),
    "compress": lambda req, progress: folder.folder_compress(req.folder(), req.level, progress),
    "epub": lambda req, progress: folder.folder_to_epub(req.folder(), progress),
}


@app.post("/api/folder/{action}")
async def api_folder(action: str, req: FolderRequest) -> dict:
    if action not in FOLDER_ACTIONS:
        raise HTTPException(status_code=404, detail="Неизвестное действие")
    req.folder()  # пустой путь — сразу ошибка, без фоновой задачи

    def task(job: Job, progress: Progress) -> None:
        job.created = [str(p) for p in FOLDER_ACTIONS[action](req, progress)]

    return _start(task)


def _ask_directory() -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="Выбери папку") or ""
    finally:
        root.destroy()


@app.post("/api/pick-folder")
async def api_pick_folder() -> dict:
    path = await run_in_threadpool(_ask_directory)
    return {"path": str(Path(path)) if path else ""}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # не падаем на консоли без UTF-8
    logging.basicConfig(level=logging.INFO)
    url = f"http://{HOST}:{PORT}"
    threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"PDF Tools: {url}  (Ctrl+C — выход)")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
