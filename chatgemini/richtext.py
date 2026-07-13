"""Convert Gemini Web's private rich-component markup into portable Markdown."""
import html
import re


_ATTRIBUTE_RE = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_COMPONENT_RE = re.compile(r"^<\/?[A-Z][A-Za-z0-9]*(?:\s|/?>)")


def _attributes(tag: str) -> dict:
    return {
        name: html.unescape(double or single or "")
        for name, double, single in _ATTRIBUTE_RE.findall(tag)
    }


def _clean_inline(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split())


def _find_tag_end(value: str, start: int) -> int:
    quote = ""
    for index in range(start + 1, len(value)):
        char = value[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in ("\"", "'"):
            quote = char
        elif char == ">":
            return index
    return -1


def _render_component(tag: str) -> str:
    match = re.match(r"^<\s*(/?)\s*([A-Z][A-Za-z0-9]*)", tag)
    if not match:
        return tag
    closing, name = match.groups()
    if closing:
        return "\n" if name == "TimelineEvent" else ""

    attrs = _attributes(tag)
    if name == "Timeline":
        return ""
    if name == "TimelineEvent":
        time_value = _clean_inline(attrs.get("time", ""))
        title = _clean_inline(attrs.get("title", ""))
        heading = " · ".join(value for value in (time_value, title) if value)
        return f"\n\n### {heading}\n\n" if heading else "\n\n"
    if name == "ElicitationsGroup":
        message = _clean_inline(attrs.get("message", ""))
        return f"\n\n### {message}\n\n" if message else "\n\n"
    if name == "Elicitation":
        label = _clean_inline(attrs.get("label", ""))
        query = _clean_inline(attrs.get("query", ""))
        if label and query:
            return f"- **{label}**：{query}\n"
        if label or query:
            return f"- {label or query}\n"
        return ""
    if name == "Image":
        alt = _clean_inline(attrs.get("alt", "")) or "Gemini image"
        caption = _clean_inline(attrs.get("caption", ""))
        src = attrs.get("src", "").strip()
        parts = []
        if src.startswith(("http://", "https://")):
            parts.append(f"![{alt}]({src})")
        if caption:
            parts.append(f"*{caption}*")
        return "\n\n" + "\n\n".join(parts) + "\n\n" if parts else ""

    # Unknown Gemini components are presentation wrappers. Keep their child
    # text while removing the private JSX-like tags themselves.
    return ""


class RichTextSanitizer:
    """Incrementally sanitize Gemini component markup across SSE boundaries."""

    def __init__(self):
        self._pending = ""
        self._in_fence = False

    def feed(self, chunk: str = "", final: bool = False) -> str:
        self._pending += chunk or ""
        output = []
        position = 0
        length = len(self._pending)

        while position < length:
            if self._pending.startswith("```", position):
                self._in_fence = not self._in_fence
                output.append("```")
                position += 3
                continue

            if self._in_fence:
                next_fence = self._pending.find("```", position)
                if next_fence < 0:
                    if final:
                        output.append(self._pending[position:])
                        position = length
                    else:
                        keep = min(2, length - position)
                        output.append(self._pending[position:length - keep])
                        position = length - keep
                    break
                output.append(self._pending[position:next_fence])
                position = next_fence
                continue

            if self._pending.startswith("{/*", position):
                comment_end = self._pending.find("*/}", position + 3)
                if comment_end < 0:
                    if final:
                        position = length
                    break
                position = comment_end + 3
                continue

            char = self._pending[position]
            if char == "<":
                tag_end = _find_tag_end(self._pending, position)
                if tag_end < 0:
                    tail = self._pending[position:]
                    possible_component = tail in ("<", "</") or bool(re.match(r"^</?[A-Z]", tail))
                    if final or not possible_component:
                        output.append(char)
                        position += 1
                        continue
                    break
                tag = self._pending[position:tag_end + 1]
                if _COMPONENT_RE.match(tag):
                    output.append(_render_component(tag))
                    position = tag_end + 1
                    continue

            if char == "{" and not final:
                tail = self._pending[position:]
                if "{/*".startswith(tail):
                    break
            if char == "`" and not final:
                tail = self._pending[position:]
                if "```".startswith(tail):
                    break

            output.append(char)
            position += 1

        self._pending = self._pending[position:]
        if final and self._pending:
            output.append(self._pending)
            self._pending = ""
        return "".join(output)


def sanitize_rich_text(text: str) -> str:
    sanitizer = RichTextSanitizer()
    return sanitizer.feed(text or "", final=True)
