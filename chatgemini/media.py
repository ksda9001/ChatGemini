"""Persist Gemini images and render them as portable chat content."""
import mimetypes
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import CONFIG


MEDIA_PLACEHOLDER = "__CHATGEMINI_MEDIA__/"


def _media_directory() -> Path:
    path = Path(CONFIG.get("media_store_path") or "/app/data/media")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_media(directory: Path):
    now = time.time()
    ttl = max(60, int(CONFIG.get("media_store_ttl_sec", 86400) or 86400))
    max_files = max(1, int(CONFIG.get("media_store_max_files", 500) or 500))
    files = []
    try:
        for path in directory.iterdir():
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_mtime < now - ttl:
                path.unlink(missing_ok=True)
            else:
                files.append((stat.st_mtime, path))
        for _, path in sorted(files, reverse=True)[max_files:]:
            path.unlink(missing_ok=True)
    except OSError:
        return


def cache_image_bytes(data: bytes, content_type: str = "image/png") -> str:
    """Persist image bytes under an opaque name and return a media placeholder."""
    if not data:
        return ""
    mime = (content_type or "image/png").split(";", 1)[0].strip().lower()
    if not mime.startswith("image/"):
        return ""
    extension = mimetypes.guess_extension(mime) or ".png"
    try:
        directory = _media_directory()
        filename = f"{uuid.uuid4().hex}{extension}"
        path = directory / filename
        path.write_bytes(data)
        os.chmod(path, 0o600)
        _prune_media(directory)
    except OSError:
        return ""
    return MEDIA_PLACEHOLDER + filename


async def cache_image_object(image) -> str:
    """Download a gemini-webapi image with its authenticated client."""
    try:
        directory = _media_directory()
        stem = uuid.uuid4().hex
        saved = await image.save(path=str(directory), filename=stem)
        filename = Path(saved).name
        path = directory / filename
        if not path.is_file() or path.parent.resolve() != directory.resolve():
            return ""
        os.chmod(path, 0o600)
        _prune_media(directory)
        return MEDIA_PLACEHOLDER + filename
    except Exception:
        return ""


def image_markdown(url: str, alt: str = "", title: str = "") -> str:
    """Return a Markdown image for a safe HTTP(S) URL."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url.startswith(MEDIA_PLACEHOLDER):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
    label = (alt or title or "Gemini image").strip().strip("[]")
    label = label.replace("\\", "").replace("[", "").replace("]", "")
    label = " ".join(label.split()) or "Gemini image"
    return f"![{label}]({url})"


async def output_deltas(output, seen_urls: set) -> list:
    """Extract text and newly observed image Markdown from a WebAPI output."""
    deltas = []
    text = getattr(output, "text_delta", "") or ""
    if text:
        deltas.append(text)
    for image in getattr(output, "images", None) or []:
        url = getattr(image, "url", "") or ""
        if not url or url in seen_urls:
            continue
        cached_url = await cache_image_object(image)
        markdown = image_markdown(
            cached_url or url,
            getattr(image, "alt", "") or "",
            getattr(image, "title", "") or "",
        )
        if not markdown:
            continue
        seen_urls.add(url)
        deltas.append(f"\n\n{markdown}")
    return deltas
