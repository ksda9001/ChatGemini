"""Render Gemini media objects as portable chat content."""
from urllib.parse import urlsplit


def image_markdown(url: str, alt: str = "", title: str = "") -> str:
    """Return a Markdown image for a safe HTTP(S) URL."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    label = (alt or title or "Gemini image").strip().strip("[]")
    label = label.replace("\\", "").replace("[", "").replace("]", "")
    label = " ".join(label.split()) or "Gemini image"
    return f"![{label}]({url})"


def output_deltas(output, seen_urls: set) -> list:
    """Extract text and newly observed image Markdown from a WebAPI output."""
    deltas = []
    text = getattr(output, "text_delta", "") or ""
    if text:
        deltas.append(text)
    for image in getattr(output, "images", None) or []:
        url = getattr(image, "url", "") or ""
        if not url or url in seen_urls:
            continue
        markdown = image_markdown(
            url,
            getattr(image, "alt", "") or "",
            getattr(image, "title", "") or "",
        )
        if not markdown:
            continue
        seen_urls.add(url)
        deltas.append(f"\n\n{markdown}")
    return deltas
