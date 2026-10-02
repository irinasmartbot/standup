"""Публичные афиши для поля «Картинка» в мероприятиях."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

# Отдельная папка: не попадает в рандомные обложки брони из «фото/».
POSTERS_DIR = Path(__file__).resolve().parents[2] / "фото" / "afisha"
_ALLOWED_SUFFIX = {".jpg", ".jpeg", ".png", ".webp"}


async def poster_page(request: web.Request) -> web.StreamResponse:
    name = request.match_info["filename"]
    if Path(name).name != name or name.startswith("."):
        raise web.HTTPNotFound()
    root = POSTERS_DIR.resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise web.HTTPNotFound() from None
    if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIX:
        raise web.HTTPNotFound()
    return web.FileResponse(path)


def register_routes(app: web.Application) -> None:
    # /vk/* уже открыт в nginx без логина админки.
    app.router.add_get("/vk/posters/{filename}", poster_page)
