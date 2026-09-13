from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_group_chat_ai_image_generation_reuses_sendable_image_draft():
    ai_images = (WEB / "ai_images.js").read_text(encoding="utf-8")
    groups = (WEB / "groups.js").read_text(encoding="utf-8")
    images = (WEB / "images.js").read_text(encoding="utf-8")

    assert "AI 生成图片暂只支持单聊" not in ai_images
    assert "data-ai-image-target" in ai_images
    assert "CM.features.groups?.current?.()" in ai_images
    assert "groupMembers()" in ai_images
    assert "/images/rewrite" in ai_images
    assert "/images/generate" in ai_images
    assert "CM.features.images?.openDataDraft?." in ai_images
    assert 'source:"AI_GENERATED"' in ai_images
    assert "sameConversation(snapshot)" in ai_images

    assert "async function sendImage(draft, caption)" in groups
    assert "image:{filename:draft.filename,data_url:draft.data_url}" in groups
    assert "if (CM.isGroupConversation()) return CM.features.groups?.sendImage?.(currentDraft, caption);" in images

    node = shutil.which("node")
    if node:
        subprocess.run([node, "--check", str(WEB / "ai_images.js")], check=True, capture_output=True, text=True)
