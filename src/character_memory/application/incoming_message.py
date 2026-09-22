from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedUserFact:
    """Channel-neutral user input after media/sticker normalization.

    Channel adapters decide where the fact is stored and add channel provenance
    (conversation id, mentions, turn id). The semantic content and common media
    metadata live here so Direct and Group cannot drift.
    """

    display_text: str
    runtime_content: str
    metadata: dict[str, Any]


def normalize_user_fact(
    message: str,
    *,
    sticker: dict | None = None,
    image: dict | None = None,
) -> NormalizedUserFact:
    content = str(message or "").strip()

    if sticker is not None and image is not None:
        raise ValueError("send a sticker or image in one user turn, not both")
    if not content and sticker is None and image is None:
        raise ValueError("message, sticker or image must not be empty")

    metadata: dict[str, Any] = {"display_text": content}
    runtime_parts = [content] if content else []

    if sticker is not None:
        sticker_id = str(sticker.get("id") or "").strip()
        label = str(sticker.get("label") or sticker_id or "表情包").strip() or "表情包"
        tags = [
            str(value).strip()
            for value in (sticker.get("tags") or [])
            if str(value).strip()
        ] if isinstance(sticker.get("tags"), list) else []
        description = str(sticker.get("description") or "").strip()
        meaning = "、".join(tags) or description or label
        metadata.update(
            {
                "action": "STICKER",
                "sticker_id": sticker_id,
                "sticker_label": label,
                "sticker_meaning": meaning,
            }
        )
        runtime_parts.append(
            f"[用户发送表情包：{label}{f'；含义：{meaning}' if meaning else ''}]"
        )

    if image is not None:
        media_id = str(image.get("id") or "").strip()
        original_name = str(image.get("original_name") or "图片").strip() or "图片"
        mime_type = str(image.get("mime_type") or "").strip()
        size_bytes = int(image.get("size_bytes") or 0)
        metadata.update(
            {
                "media_id": media_id,
                "media_name": original_name,
                "media_mime_type": mime_type,
                "media_size_bytes": size_bytes,
            }
        )
        runtime_parts.append(
            f"[用户发送真实图片：{original_name}。图片本体已随本轮多模态请求提供，请根据实际视觉内容理解。]"
        )

    return NormalizedUserFact(
        display_text=content,
        runtime_content="\n".join(runtime_parts).strip(),
        metadata=metadata,
    )
