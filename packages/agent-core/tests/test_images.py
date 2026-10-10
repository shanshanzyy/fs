"""多模态图片输入（N / P1-10）测试。

验收要求（P1-P2-FEATURES-PLAN §11 / N 阶段）：

1. 白名单外后缀 / 超大图片被拒；
2. 越权路径（`../` 逃逸）被拒；
3. parts 形状正确（`image_url` + `data:` 前缀）；
4. `vision_mode=off` 时 `image` 引用降级为提示，且消息仍是**纯字符串**；
5. 不含图片的 submit 与今天**逐字节一致**。

反向验证（§11 / N10）：临时让 `off` 也处理图片，
`test_submit_off_degrades_to_notice` 与 `test_resolve_off_degrades_to_error`
必须变红。
"""

from __future__ import annotations

import base64
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.context import RefResolution, resolve_refs  # noqa: E402
from agent_core.context.images import (  # noqa: E402
    ALLOWED_SUFFIXES,
    identify_image,
    load_image,
    message_text,
    to_message_parts,
)
from agent_core.errors import AgentError, ToolError  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.sandbox import PathGuard  # noqa: E402

# 各类型图片的**最小可识别字节**（只满足文件头魔数，够 identify_image 用）。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + (100).to_bytes(4, "little") + b"WEBP" + b"\x00" * 64

SAMPLES = {"png": PNG, "jpg": JPEG, "gif": GIF, "webp": WEBP}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class ImageHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.guard = PathGuard(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_bytes(self, rel: str, data: bytes) -> Path:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _session(self, **overrides) -> Session:
        config = replace(Config.load(self.ws), **overrides)
        return Session(config, client=object())

    def _wait(self, cond, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not cond():
            time.sleep(0.01)
        self.assertTrue(cond(), "等待后台任务结果超时")


# ---------------------------------------------------------------- ① 魔数识别
class IdentifyTests(unittest.TestCase):
    def test_magic_bytes_map_to_expected_type(self) -> None:
        self.assertEqual(identify_image(PNG), (".png", "image/png"))
        self.assertEqual(identify_image(JPEG), (".jpg", "image/jpeg"))
        self.assertEqual(identify_image(GIF), (".gif", "image/gif"))
        self.assertEqual(identify_image(WEBP), (".webp", "image/webp"))

    def test_unknown_bytes_return_none(self) -> None:
        self.assertIsNone(identify_image(b"just some text"))
        self.assertIsNone(identify_image(b""))


# ---------------------------------------------------------------- ①② 拒绝
class LoadImageRejectTests(ImageHarness):
    def test_suffix_outside_whitelist_rejected(self) -> None:
        # 内容其实是 PNG，但后缀不在白名单 —— 后缀这一关就该拦下
        self._write_bytes("pic.bmp", PNG)
        with self.assertRaises(ToolError) as ctx:
            load_image(self.guard, "pic.bmp")
        self.assertIn("不是受支持的图片类型", str(ctx.exception))

    def test_oversized_rejected(self) -> None:
        self._write_bytes("big.png", PNG)
        with self.assertRaises(ToolError) as ctx:
            load_image(self.guard, "big.png", max_bytes=8)
        self.assertIn("过大", str(ctx.exception))

    def test_magic_mismatch_rejected(self) -> None:
        # 后缀是 .png，内容却是文本 —— 魔数这一关必须拦下
        self._write_bytes("fake.png", b"this is not an image at all")
        with self.assertRaises(ToolError) as ctx:
            load_image(self.guard, "fake.png")
        self.assertIn("文件头", str(ctx.exception))

    def test_escape_path_rejected(self) -> None:
        outside = self.ws.parent / "outside.png"
        outside.write_bytes(PNG)
        try:
            with self.assertRaises(AgentError):
                load_image(self.guard, "../outside.png")
        finally:
            outside.unlink(missing_ok=True)

    def test_allowed_suffixes_are_the_five_images(self) -> None:
        self.assertEqual(
            ALLOWED_SUFFIXES, frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
        )


# ---------------------------------------------------------------- ③ parts 形状
class PartShapeTests(ImageHarness):
    def test_png_becomes_data_url(self) -> None:
        for ext, blob in SAMPLES.items():
            self._write_bytes(f"a.{ext}", blob)
            part = load_image(self.guard, f"a.{ext}")
            self.assertEqual(part["type"], "image_url")
            url = part["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/"))
            self.assertIn(";base64,", url)
            # data URL 解回来必须与原始字节逐字节相同
            encoded = url.split(";base64,", 1)[1]
            self.assertEqual(base64.b64decode(encoded), blob)

    def test_to_message_parts_puts_text_first(self) -> None:
        self._write_bytes("a.png", PNG)
        image = load_image(self.guard, "a.png")
        parts = to_message_parts("据图改一下", [image])
        self.assertEqual(parts[0], {"type": "text", "text": "据图改一下"})
        self.assertEqual(parts[1], image)

    def test_message_text_ignores_image_payload(self) -> None:
        parts = to_message_parts("正文", [{"type": "image_url", "image_url": {"url": "data:x"}}])
        self.assertEqual(message_text(parts), "正文")
        # 图片的 base64 不进文本指纹
        self.assertNotIn("data:x", message_text(parts))


# -------------------------------------------------- ④ resolve_refs 的 off 降级
class ResolveImagesTests(ImageHarness):
    def test_resolve_off_degrades_to_error(self) -> None:
        self._write_bytes("a.png", PNG)
        result = resolve_refs(self.guard, [{"type": "image", "value": "a.png"}])
        self.assertEqual(result.images, [])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("图片输入未开启", result.errors[0])

    def test_resolve_on_produces_image_part_without_budget(self) -> None:
        self._write_bytes("a.png", PNG)
        result = resolve_refs(
            self.guard, [{"type": "image", "value": "a.png"}], allow_images=True
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.images), 1)
        self.assertTrue(result.ok)
        # 图片不占文本字符预算
        self.assertEqual(result.chars, 0)

    def test_escape_image_degrades_not_raises(self) -> None:
        outside = self.ws.parent / "outside.png"
        outside.write_bytes(PNG)
        try:
            result = resolve_refs(
                self.guard,
                [{"type": "image", "value": "../outside.png"}],
                allow_images=True,
            )
        finally:
            outside.unlink(missing_ok=True)
        self.assertEqual(result.images, [])
        self.assertIn("被拒绝", result.errors[0])


# -------------------------------------------------- ④⑤ 会话层 submit
class SessionSubmitTests(ImageHarness):
    def test_submit_off_degrades_to_notice(self) -> None:
        """vision_mode=off：图片降级为提示，消息仍是**纯字符串**。"""
        self._write_bytes("a.png", PNG)
        session = self._session(vision_mode="off")
        captured: dict[str, object] = {}
        session.agent.run = lambda text: captured.setdefault("text", text) or "done"  # type: ignore[method-assign]

        session.submit("据图改一下", [{"type": "image", "value": "a.png"}])
        self._wait(lambda: "text" in captured)

        message = captured["text"]
        self.assertIsInstance(message, str)          # 降级后仍是纯字符串
        self.assertEqual(message, "据图改一下")
        self.assertTrue(
            any("图片输入未开启" in note for note in session._notices),
            f"应有一条「图片输入未开启」提示，实际：{session._notices}",
        )

    def test_submit_on_attaches_parts(self) -> None:
        """vision_mode=on：消息 content 变成 parts 列表（先文字、后图片）。"""
        self._write_bytes("a.png", PNG)
        session = self._session(vision_mode="on")
        captured: dict[str, object] = {}
        session.agent.run = lambda text: captured.setdefault("text", text) or "done"  # type: ignore[method-assign]

        session.submit("据图改一下", [{"type": "image", "value": "a.png"}])
        self._wait(lambda: "text" in captured)

        message = captured["text"]
        self.assertIsInstance(message, list)
        assert isinstance(message, list)             # 供类型收窄
        self.assertEqual(message[0], {"type": "text", "text": "据图改一下"})
        self.assertEqual(message[1]["type"], "image_url")

    def test_submit_without_images_is_byte_identical(self) -> None:
        """不含图片：消息仍是纯字符串、正文逐字节不变。"""
        session = self._session(vision_mode="on")
        captured: dict[str, object] = {}
        session.agent.run = lambda text: captured.setdefault("text", text) or "done"  # type: ignore[method-assign]

        session.submit("原样提交")
        self._wait(lambda: "text" in captured)
        self.assertEqual(captured["text"], "原样提交")


# -------------------------------------------------- 上传端点（session 层）
class UploadImageTests(ImageHarness):
    def test_upload_writes_and_returns_ref(self) -> None:
        session = self._session(vision_mode="on")
        result = session.upload_image("shot.png", _b64(PNG), "image/png")
        self.assertEqual(result["ref"]["type"], "image")
        self.assertTrue(result["ref"]["value"].startswith(".vibe/images/"))
        self.assertEqual(result["ext"], ".png")
        self.assertEqual(result["bytes"], len(PNG))
        # 全量 base64 已落盘
        dest = self.ws / result["ref"]["value"]
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), PNG)

    def test_upload_accepts_data_url_prefix(self) -> None:
        session = self._session(vision_mode="on")
        data_url = "data:image/png;base64," + _b64(PNG)
        result = session.upload_image("shot.png", data_url)
        self.assertEqual(result["ext"], ".png")

    def test_upload_off_is_rejected(self) -> None:
        session = self._session(vision_mode="off")
        with self.assertRaises(ValueError) as ctx:
            session.upload_image("shot.png", _b64(PNG))
        self.assertIn("未开启", str(ctx.exception))

    def test_upload_bad_base64_rejected(self) -> None:
        session = self._session(vision_mode="on")
        with self.assertRaises(ValueError):
            session.upload_image("shot.png", "!!!not-base64!!!")

    def test_upload_non_image_rejected(self) -> None:
        session = self._session(vision_mode="on")
        with self.assertRaises(ValueError) as ctx:
            session.upload_image("notes.txt", _b64(b"hello world"))
        self.assertIn("不是受支持的图片格式", str(ctx.exception))

    def test_upload_oversize_rejected(self) -> None:
        session = self._session(vision_mode="on", vision_max_image_bytes=8)
        with self.assertRaises(ValueError) as ctx:
            session.upload_image("big.png", _b64(PNG))
        self.assertIn("过大", str(ctx.exception))

    def test_upload_audit_records_metadata_only(self) -> None:
        """审计只记 bytes/ext/digest，**图片正文（base64）不出本机**（ADR-006）。"""
        session = self._session(vision_mode="on", audit_enabled=True)
        session.upload_image("shot.png", _b64(PNG))
        audit_file = self.ws / ".vibe" / "audit.jsonl"
        self.assertTrue(audit_file.exists())
        text = audit_file.read_text(encoding="utf-8")
        self.assertIn("image.upload", text)
        self.assertIn('"bytes"', text)
        self.assertIn('"ext"', text)
        self.assertIn('"digest"', text)
        # 正文 base64 的片段绝不出现在审计里
        self.assertNotIn(_b64(PNG)[:24], text)


# -------------------------------------------------- 兜底：无图时逐字节不变
class NoImageInvarianceTests(ImageHarness):
    def test_empty_resolution_renders_nothing(self) -> None:
        from agent_core.context import format_refs_block

        self.assertEqual(format_refs_block(RefResolution()), "")


if __name__ == "__main__":
    unittest.main()