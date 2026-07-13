"""OpenAI chat message normalization, compaction, and prompt rendering."""
import base64
import json
import urllib.parse


CHAT_ROLES = {"system", "developer", "user", "assistant"}


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _decode_data_url(url: str):
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, encoded = url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "application/octet-stream"
        if ";base64" in header:
            data = base64.b64decode(encoded, validate=True)
        else:
            data = urllib.parse.unquote_to_bytes(encoded)
        return data, mime
    except (ValueError, TypeError):
        return None


def _content_parts(content) -> tuple:
    """Return text fragments and image sources from one OpenAI content value."""
    if isinstance(content, str):
        return [content], []
    if not isinstance(content, list):
        return ([_as_text(content)] if content is not None else []), []

    texts = []
    images = []
    for part in content:
        if not isinstance(part, dict):
            if part is not None:
                texts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type in ("text", "input_text", "output_text"):
            text = part.get("text", "")
            if text:
                texts.append(_as_text(text))
            continue
        if part_type == "image_url":
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else image
        elif part_type in ("image", "input_image"):
            source = part.get("source") or {}
            if isinstance(source, dict) and source.get("data"):
                url = f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"
            else:
                url = part.get("image_url") or part.get("url") or ""
        else:
            # Tool/function protocol blocks are intentionally not part of ChatGemini.
            continue

        decoded = _decode_data_url(url)
        if decoded:
            images.append(decoded)
        elif isinstance(url, str) and url.startswith(("http://", "https://")):
            images.append((url, None))
    return texts, images


def normalize_messages(messages: list) -> list:
    """Keep only ordinary chat roles and content; discard all tool protocol data."""
    normalized = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        if role not in CHAT_ROLES:
            continue
        texts, images = _content_parts(message.get("content", ""))
        content = "\n".join(text for text in texts if text)
        if not content and not images:
            continue
        item = {"role": role, "content": content}
        if images:
            item["_images"] = images
        normalized.append(item)
    return normalized


def _message_size(message: dict) -> int:
    return len(message.get("role", "")) + len(message.get("content", ""))


def _trim_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"\n\n[... {len(text) - max_chars} earlier characters omitted ...]\n\n"
    head = min(max_chars // 4, 2000)
    tail = max(1, max_chars - head - len(marker))
    return text[:head] + marker + text[-tail:]


def compact_messages(messages: list, max_messages: int, max_chars: int) -> list:
    """Preserve system instructions and the most recent chat within fixed limits."""
    messages = normalize_messages(messages)
    if not messages:
        return []

    system = [message for message in messages if message["role"] in ("system", "developer")]
    conversation = [message for message in messages if message["role"] not in ("system", "developer")]
    if max_messages > 0:
        conversation = conversation[-max_messages:]

    compacted = system + conversation
    if max_chars <= 0:
        return compacted

    while len(compacted) > 1 and sum(_message_size(message) for message in compacted) > max_chars:
        removable = next(
            (index for index, message in enumerate(compacted) if message["role"] not in ("system", "developer")),
            None,
        )
        if removable is None or removable == len(compacted) - 1:
            break
        compacted.pop(removable)

    total = sum(_message_size(message) for message in compacted)
    if total > max_chars:
        last = dict(compacted[-1])
        other = total - len(last.get("content", ""))
        last["content"] = _trim_text(last.get("content", ""), max(1, max_chars - other))
        compacted[-1] = last
    return compacted


def messages_to_prompt(messages: list) -> tuple:
    """Render ordinary chat history and return `(prompt, images)`."""
    sections = []
    images = []
    labels = {
        "system": "System",
        "developer": "System",
        "user": "User",
        "assistant": "Assistant",
    }
    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        if content:
            label = labels.get(role, "User")
            sections.append(f"[{label}]\n{content}\n[/{label}]")
        images.extend(message.get("_images") or [])
    return "\n\n".join(sections), images


def messages_to_web_prompt(messages: list) -> tuple:
    """Render a human-readable prompt for a persistent Gemini Web chat.

    The Web UI displays the exact prompt supplied to its chat endpoint. For a
    normal user turn, preserve that text exactly instead of exposing the role
    delimiters used by the direct-transport fallback.
    """
    images = []
    system_messages = []
    conversation = []
    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        images.extend(message.get("_images") or [])
        if not content:
            continue
        if role in ("system", "developer"):
            system_messages.append(content)
        else:
            conversation.append((role, content))

    if not conversation:
        return "\n\n".join(system_messages), images

    latest_role, latest_content = conversation[-1]
    if len(conversation) == 1 and latest_role == "user" and not system_messages:
        return latest_content, images

    sections = []
    if system_messages:
        sections.append("Instructions for this chat:\n" + "\n\n".join(system_messages))

    earlier = conversation[:-1] if latest_role == "user" else conversation
    if earlier:
        transcript = []
        for role, content in earlier:
            label = "Earlier assistant reply" if role == "assistant" else "Earlier user message"
            transcript.append(f"{label}:\n{content}")
        sections.append("Conversation context:\n" + "\n\n".join(transcript))

    if latest_role == "user":
        sections.append(latest_content)
    elif not sections:
        sections.append(latest_content)
    return "\n\n".join(sections), images


def serializable_messages(messages: list) -> list:
    """Remove request-only binary image values before hashing or SQLite storage."""
    result = []
    for message in messages or []:
        item = {"role": message.get("role", "user"), "content": message.get("content", "")}
        if message.get("_images"):
            item["has_images"] = True
        result.append(item)
    return result


def _google_content_parts(parts: list) -> tuple:
    """Extract text and inline images from Gemini-native content parts."""
    texts = []
    images = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
            continue
        if not isinstance(part.get("inlineData"), dict):
            continue
        inline = part["inlineData"]
        data = inline.get("data")
        if not isinstance(data, str):
            continue
        try:
            images.append((
                base64.b64decode(data, validate=True),
                inline.get("mimeType") or "application/octet-stream",
            ))
        except (ValueError, TypeError):
            continue
    return texts, images


def google_request_to_messages(request: dict) -> list:
    """Normalize Google-native content into the same chat-only message shape.

    `functionCall` and `functionResponse` parts are intentionally ignored. This
    adapter exists for NewAPI's Gemini-format channels, not for tool calling.
    """
    messages = []
    system = request.get("systemInstruction") or {}
    if isinstance(system, dict):
        texts, images = _google_content_parts(system.get("parts") or [])
        if texts or images:
            item = {"role": "system", "content": "\n".join(texts)}
            if images:
                item["_images"] = images
            messages.append(item)

    for content in request.get("contents") or []:
        if not isinstance(content, dict):
            continue
        role = "assistant" if content.get("role") == "model" else "user"
        texts, images = _google_content_parts(content.get("parts") or [])
        # Function protocol parts intentionally do not reach Gemini.
        if texts or images:
            item = {"role": role, "content": "\n".join(texts)}
            if images:
                item["_images"] = images
            messages.append(item)
    return messages
