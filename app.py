"""Локальный веб-интерфейс PDF Tools. Запуск: python app.py"""
import logging
import sys
import threading
import webbrowser
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

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("pdftools")
app = FastAPI(title="PDF Tools")
_LOCK = threading.Lock()  # PyMuPDF не потокобезопасен


def _run(fn, *args):
    with _LOCK:
        return fn(*args)

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


@app.post("/api/images-to-pdf")
async def api_images_to_pdf(files: Annotated[list[UploadFile], File()]) -> Response:
    data = await run_in_threadpool(_run, images_to_pdf, await _read_all(files))
    return _file_response(data, "картинки.pdf", "application/pdf")


@app.post("/api/merge")
async def api_merge(files: Annotated[list[UploadFile], File()]) -> Response:
    data = await run_in_threadpool(_run, merge_pdfs, await _read_all(files))
    return _file_response(data, "объединённый.pdf", "application/pdf")


@app.post("/api/compress")
async def api_compress(file: Annotated[UploadFile, File()], level: Annotated[str, Form()] = "medium") -> Response:
    original = await file.read()
    data = await run_in_threadpool(_run, compress_pdf, original, level, file.filename or "PDF")
    sizes = {"X-Original-Size": str(len(original)), "X-New-Size": str(len(data))}
    return _file_response(data, f"{_stem(file.filename)}.pdf", "application/pdf", sizes)


@app.post("/api/epub")
async def api_epub(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    author: Annotated[str, Form()] = "",
) -> Response:
    original = await file.read()
    data = await run_in_threadpool(
        _run, pdf_to_epub, original, title.strip() or None, author.strip() or None, file.filename or "book.pdf"
    )
    return _file_response(data, f"{_stem(file.filename)}.epub", "application/epub+zip")


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
    "images": lambda req: folder.folder_images_to_pdf(req.folder()),
    "merge": lambda req: folder.folder_merge(req.folder()),
    "compress": lambda req: folder.folder_compress(req.folder(), req.level),
    "epub": lambda req: folder.folder_to_epub(req.folder()),
}


@app.post("/api/folder/{action}")
async def api_folder(action: str, req: FolderRequest) -> dict:
    if action not in FOLDER_ACTIONS:
        raise HTTPException(status_code=404, detail="Неизвестное действие")
    created = await run_in_threadpool(_run, FOLDER_ACTIONS[action], req)
    return {"created": [str(p) for p in created]}


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
