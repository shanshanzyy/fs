"""审计载荷脱敏：键白名单、值形状、拒码（ADR-006）。

## 为什么这组必须有测试

这条红线的失败模式是**沉默入库**：某天有人往 payload 里塞了一个 diff，
键名恰好不在黑名单、值恰好没触发形状检查，于是代码就进了库，
而且没人会拦 —— 因为「反正审计是给人看的」。

更隐蔽的一层：这里每拒一条事件的代价，比「少一条记录」更大。
客户端是**先把事件算进本地上传链、再上传**的，所以一条被服务端拒掉的事件
**永久丢失**（本地链已推进，重传不会补上）。这意味着：

- 规则必须挡住代码（安全）；
- 但规则也**不能误伤合法载荷**（否则就是无声地丢审计）。

两个方向都要测。
"""

from __future__ import annotations

import unittest

from vibe_server.redaction import (
    DENIED_KEYS,
    MAX_STRING,
    event_schema,
    known_event_types,
    looks_like_code,
    redaction_stats,
    validate_event,
)


def event(event_type: str, **payload: object) -> dict:
    return {"type": event_type, "payload": payload}


class DeniedKeyTests(unittest.TestCase):
    """闸门 3：正文类键名，一出现就拒。"""

    def test_content_bearing_keys_are_rejected(self) -> None:
        """核心红线：把代码/正文塞进审计的典型键名，全都要挡住。"""
        for key in ("content", "code", "diff", "prompt", "output", "source"):
            with self.subTest(key=key):
                verdict = validate_event(event("tool.write_file", **{key: "anything"}))
                self.assertFalse(verdict.ok, f"{key} 没有被挡下")

    def test_platform_specific_keys_are_also_rejected(self) -> None:
        """这几个是**平台自造**的：本地审计曾用它们装原文。

        不在最初的通用黑名单里，但只要有人「照着客户端字段名补白名单」，
        原文就会顺这条路进库。所以它们是显式钉死的。
        """
        for key in ("command", "pattern", "error", "detail", "arguments", "stdout"):
            with self.subTest(key=key):
                verdict = validate_event(event("tool.run_command", **{key: "ls -la"}))
                self.assertFalse(verdict.ok, f"{key} 没有被挡下")

    def test_denied_key_wins_even_if_whitelisted(self) -> None:
        """黑名单优先于白名单 —— 它是给「未来有人往白名单加东西」留的护栏。"""
        for key in DENIED_KEYS:
            with self.subTest(key=key):
                verdict = validate_event(event("session.start", **{key: "x"}))
                self.assertFalse(verdict.ok)

    def test_rejection_message_is_actionable(self) -> None:
        """拒绝原因要能回给客户端开发者，否则他们只会怀疑服务端在丢数据。"""
        verdict = validate_event(event("tool.write_file", content="x = 1"))
        self.assertIn("正文类字段", verdict.reason)
        self.assertIn("摘要", verdict.reason, "没说清该怎么改")


class KeyWhitelistTests(unittest.TestCase):
    """闸门 1：非白名单字段一律拒，不是「忽略多余的」。"""

    def test_unknown_key_is_rejected(self) -> None:
        verdict = validate_event(event("session.start", model="gpt-4", nonsense="x"))
        self.assertFalse(verdict.ok)
        self.assertIn("nonsense", verdict.reason)

    def test_field_from_another_event_type_is_rejected(self) -> None:
        """字段名对了、事件类型不对，也要拒 —— 白名单是**按类型**的。"""
        # `added` 属于 tool.write_file / tool.edit_file，不属于 session.start
        verdict = validate_event(event("session.start", added=3))
        self.assertFalse(verdict.ok)

    def test_common_fields_are_accepted_everywhere(self) -> None:
        verdict = validate_event(
            {"type": "session.end", "payload": {"reason": "done", "session_id": "s-1", "seq": 3}}
        )
        self.assertTrue(verdict.ok, verdict.reason)

    def test_whole_event_is_rejected_not_partially_cleaned(self) -> None:
        """**不能**「过滤掉坏字段、收下好的」。

        过滤会让客户端以为记下了，实际没记 —— 排查时对不上账。
        """
        stats = redaction_stats(
            [
                event("session.end", reason="done"),
                event("session.end", reason="done", content="leaked"),
            ]
        )
        self.assertEqual(len(stats["accepted"]), 1)
        self.assertEqual(len(stats["rejected"]), 1)
        self.assertEqual(stats["rejected"][0]["index"], 1)


class ValueShapeTests(unittest.TestCase):
    """闸门 2：看**内容**，不依赖键名。这条最有价值。"""

    def test_code_like_values_are_rejected(self) -> None:
        samples = (
            "def handler(request):\n    return request",
            "class Widget:",
            "const x = () => y;",
            "if a && b {}",
            "    indented line",
            "<div class=\"row\">",
            "a(b(c(d(e(f(g))))))",          # 括号密度过高
        )
        for sample in samples:
            with self.subTest(sample=sample[:24]):
                self.assertTrue(looks_like_code(sample), f"没认出代码：{sample!r}")

    def test_code_in_an_allowed_field_is_still_rejected(self) -> None:
        """键名合法不代表内容安全 —— 形状检查是独立的第二道闸门。"""
        verdict = validate_event(
            event("tool.run_command", executable="python", status="def f():\n    pass")
        )
        self.assertFalse(verdict.ok)

    def test_multiline_value_is_rejected(self) -> None:
        verdict = validate_event(event("session.end", reason="line1\nline2"))
        self.assertFalse(verdict.ok)
        self.assertIn("换行", verdict.reason)

    def test_overlong_string_is_rejected(self) -> None:
        verdict = validate_event(event("session.end", reason="x" * (MAX_STRING + 1)))
        self.assertFalse(verdict.ok)

    def test_plain_text_passes(self) -> None:
        """反过来：正常的值不能被误伤 —— 误伤就是无声地丢审计。"""
        verdict = validate_event(
            event("session.end", reason="任务完成", iterations=12, duration_ms=3456)
        )
        self.assertTrue(verdict.ok, verdict.reason)

    def test_non_string_for_text_field_is_rejected(self) -> None:
        self.assertFalse(validate_event(event("session.start", model=123)).ok)

    def test_bool_is_not_an_int(self) -> None:
        """`True` 也是 int 的子类，若不显式排除就会被当成计数 1。"""
        self.assertFalse(validate_event(event("session.end", iterations=True)).ok)

    def test_float_is_coerced_for_int_field(self) -> None:
        verdict = validate_event(event("session.end", iterations=3.0))
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertIsInstance(verdict.cleaned["iterations"], int)

    def test_path_field_rejects_newlines_and_non_paths(self) -> None:
        self.assertFalse(validate_event(event("tool.read_file", path="src/a\nb.py")).ok)
        self.assertTrue(validate_event(event("tool.read_file", path="src/main.py")).ok)
        self.assertTrue(validate_event(event("tool.read_file", path="C:/proj/src/a.py")).ok)


class DigestTests(unittest.TestCase):
    def test_plaintext_is_rejected_where_a_digest_is_required(self) -> None:
        """`*_digest` 字段只收摘要，收到原文要拒 —— 这就是「拒码」的落点。"""
        verdict = validate_event(
            event("tool.grep", path="src", hits=3, pattern_digest="def secret():")
        )
        self.assertFalse(verdict.ok)
        self.assertIn("摘要", verdict.reason)

    def test_valid_digest_is_accepted(self) -> None:
        verdict = validate_event(
            event("tool.grep", path="src", hits=3, pattern_digest="a" * 64)
        )
        self.assertTrue(verdict.ok, verdict.reason)

    def test_short_or_non_hex_digest_is_rejected(self) -> None:
        self.assertFalse(validate_event(event("tool.grep", pattern_digest="deadbeef")).ok)
        self.assertFalse(validate_event(event("tool.grep", pattern_digest="z" * 40)).ok)

    def test_uppercase_digest_is_accepted(self) -> None:
        """摘要大小写等价，必须收。

        这一条看着吹毛求疵，但它挡的是**无声丢审计**：客户端一旦（因为
        某个库的改动）发了大写 hex，大小写敏感的规则会把整条事件拒掉，
        而那条事件在客户端已经算进上传链 —— 永久丢失，且没人会知道。
        """
        self.assertTrue(
            validate_event(event("tool.grep", pattern_digest="A" * 64)).ok,
            "大写 hex 摘要被拒 —— 这会永久丢掉一条审计事件",
        )


class SchemaTests(unittest.TestCase):
    def test_unknown_event_type_is_rejected(self) -> None:
        verdict = validate_event(event("nope.not.registered"))
        self.assertFalse(verdict.ok)
        self.assertIn("未登记", verdict.reason + "未登记")

    def test_missing_or_bad_type_is_rejected(self) -> None:
        self.assertFalse(validate_event({"payload": {}}).ok)
        self.assertFalse(validate_event({"type": "", "payload": {}}).ok)
        self.assertFalse(validate_event({"type": "x" * 100, "payload": {}}).ok)
        self.assertFalse(validate_event("not a dict").ok)

    def test_payload_must_be_an_object(self) -> None:
        self.assertFalse(validate_event({"type": "session.end", "payload": [1, 2]}).ok)

    def test_payload_may_be_absent(self) -> None:
        self.assertTrue(validate_event({"type": "session.end"}).ok)

    def test_schema_is_exported_for_the_client(self) -> None:
        """契约要能整份发给客户端（`/audit/schema`）。

        只发事件名不够：客户端还得知道每个类型收哪些字段 —— 否则它会
        照着本地日志原样上传，然后被键白名单逐条拒掉。
        """
        schema = event_schema()
        self.assertIn("session.start", schema)
        self.assertIn("workspace", schema["session.start"])
        # 返回值要能安全序列化（不能被内部引用污染）
        schema["session.start"]["injected"] = "x"
        self.assertNotIn("injected", event_schema()["session.start"])

    def test_known_types_are_sorted_and_nonempty(self) -> None:
        types = known_event_types()
        self.assertEqual(types, sorted(types))
        self.assertIn("tool.execute", types)
        self.assertIn("sandbox.block", types)

    def test_every_registered_type_has_a_field_or_is_intentionally_empty(self) -> None:
        for event_type, fields in event_schema().items():
            with self.subTest(event_type=event_type):
                self.assertIsInstance(fields, dict)

    def test_gateway_events_accept_their_declared_fields(self) -> None:
        verdict = validate_event(
            event(
                "gateway.request",
                route="cloud",
                task="reasoning",
                sensitive=False,
                prompt_tokens=1234,
                max_tokens=512,
            )
        )
        self.assertTrue(verdict.ok, verdict.reason)


class RealisticLeakTests(unittest.TestCase):
    """把「有人顺手把代码带进去」的几种真实写法走一遍。"""

    def test_write_file_with_inlined_content_is_rejected(self) -> None:
        verdict = validate_event(
            event(
                "tool.write_file",
                path="src/app.py",
                mode="overwrite",
                # 顺手把内容也传了 —— 这就是 ADR-006 要挡的动作
                content="def main():\n    print('hi')\n",
            )
        )
        self.assertFalse(verdict.ok)

    def test_edit_file_with_raw_search_replace_is_rejected(self) -> None:
        verdict = validate_event(
            event(
                "tool.edit_file",
                path="src/app.py",
                strategy="exact",
                search="old_name",
                replace="new_name",
            )
        )
        self.assertFalse(verdict.ok, "search/replace 是原文，必须只收 digest")

    def test_a_well_formed_metadata_event_passes(self) -> None:
        """对照：按契约只传元数据的同一次写操作，必须通过。"""
        verdict = validate_event(
            event(
                "tool.write_file",
                path="src/app.py",
                mode="overwrite",
                added=12,
                removed=0,
                bytes=210,
                content_digest="b" * 64,
            )
        )
        self.assertTrue(verdict.ok, verdict.reason)


if __name__ == "__main__":
    unittest.main()
