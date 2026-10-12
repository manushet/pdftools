"""Локальный веб-интерфейс PDF Tools. Запуск: python app.py"""
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

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
    data = await run_in_threadpool(images_to_pdf, await _read_all(files))
    return _file_response(data, "картинки.pdf", "application/pdf")


@app.post("/api/merge")
async def api_merge(files: Annotated[list[UploadFile], File()]) -> Response:
    data = await run_in_threadpool(merge_pdfs, await _read_all(files))
    return _file_response(data, "объединённый.pdf", "application/pdf")


@app.post("/api/compress")
async def api_compress(file: Annotated[UploadFile, File()], level: Annotated[str, Form()] = "medium") -> Response:
    original = await file.read()
    data = await run_in_threadpool(compress_pdf, original, level, file.filename or "PDF")
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
        pdf_to_epub, original, title.strip() or None, author.strip() or None, file.filename or "book.pdf"
    )
    return _file_response(data, f"{_stem(file.filename)}.epub", "application/epub+zip")


class FolderRequest(BaseModel):
    path: str
    level: str = "medium"

    def folder(self) -> Path:
        # «Копировать как путь» в Проводнике добавляет кавычки
        return Path(self.path.strip().strip('"'))


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
    created = await run_in_threadpool(FOLDER_ACTIONS[action], req)
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
    logging.basicConfig(level=logging.INFO)
    url = f"http://{HOST}:{PORT}"
    threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"PDF Tools: {url}  (Ctrl+C — выход)")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
