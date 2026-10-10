"""上下文引用（P0-A）测试。

验收要求（P0-FEATURES-PLAN §3 / A8）：

1. 越权路径（`../` 逃逸、`.vibe/` 敏感目录）被拒，且**不阻断**其余引用；
2. `@file` / `@folder` / `@symbol` 三种类型都能解析出正确的产物形状；
3. 超出字符预算时按序截断并标记 `truncated`；
4. `refs` 为空 / 不传时，提交行为与今天**逐字节一致**；
5. `/api/run` 的 refs 形状校验（服务端 400 防线）。

反向验证（§9.2）：临时让 `PathGuard.check_read` 失效，越权用例必须变红。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.context import (  # noqa: E402
    MAX_REF_CHARS,
    build_refs_prompt,
    format_refs_block,
    iter_workspace_dirs,
    iter_workspace_files,
    iter_workspace_symbols,
    resolve_refs,
)
from agent_core.host.server import validate_refs  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.sandbox import PathGuard  # noqa: E402

PY_SAMPLE = '''import os


class TaskService:
    def create(self, title: str) -> dict:
        return {"title": title}


def helper(a, b=1):
    return a + b
'''


class RefHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.guard = PathGuard(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def _resolve(self, refs, **kwargs):
        return resolve_refs(self.guard, refs, **kwargs)


class EscapeTests(RefHarness):
    """越权与敏感文件：拒绝，但不弄死整条任务。"""

    def test_dotdot_escape_is_rejected(self) -> None:
        outside = self.ws.parent / "outside-secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        try:
            result = self._resolve([{"type": "file", "value": "../outside-secret.txt"}])
        finally:
            outside.unlink(missing_ok=True)
        self.assertEqual(result.refs, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("被拒绝", result.errors[0])

    def test_vibe_audit_is_rejected(self) -> None:
        (self.ws / ".vibe").mkdir()
        (self.ws / ".vibe" / "audit.jsonl").write_text("{}", encoding="utf-8")
        result = self._resolve([{"type": "file", "value": ".vibe/audit.jsonl"}])
        self.assertEqual(result.refs, [])
        self.assertTrue(result.errors)

    def test_bad_ref_does_not_block_good_ref(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        result = self._resolve(
            [
                {"type": "file", "value": "../nope.txt"},
                {"type": "file", "value": "ok.py"},
            ]
        )
        self.assertEqual([ref.value for ref in result.refs], ["ok.py"])
        self.assertEqual(len(result.errors), 1)


class ShapeTests(RefHarness):
    """三种引用类型的产物形状。"""

    def test_file_ref_carries_body_between_markers(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        result = self._resolve([{"type": "file", "value": "ok.py"}])
        self.assertTrue(result.ok)
        ref = result.refs[0]
        self.assertEqual(ref.type, "file")
        self.assertEqual(ref.label, "ok.py")
        self.assertIn("---8<--- 文件内容开始", ref.body)
        self.assertIn("class TaskService", ref.body)

    def test_folder_ref_lists_one_level(self) -> None:
        self._write("src/app.py", PY_SAMPLE)
        self._write("src/util.py", "def u():\n    pass\n")
        result = self._resolve([{"type": "folder", "value": "src"}])
        ref = result.refs[0]
        self.assertEqual(ref.label, "src")
        self.assertIn("src/", ref.body)
        self.assertIn("app.py", ref.body)
        self.assertIn("util.py", ref.body)

    def test_symbol_ref_locates_definition_line(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        result = self._resolve([{"type": "symbol", "value": "TaskService"}])
        ref = result.refs[0]
        self.assertIn("TaskService", ref.label)
        self.assertIn("svc.py:4", ref.label)  # class 在第 4 行
        self.assertIn("---8<--- 文件内容开始", ref.body)

    def test_symbol_ref_with_explicit_path(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        self._write("other.py", "class TaskService:\n    pass\n")
        result = self._resolve([{"type": "symbol", "value": "svc.py::helper"}])
        ref = result.refs[0]
        self.assertIn("svc.py", ref.label)
        self.assertIn("helper", ref.label)

    def test_missing_symbol_degrades_to_error(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        result = self._resolve([{"type": "symbol", "value": "NoSuchSymbolXYZ"}])
        self.assertEqual(result.refs, [])
        self.assertIn("未找到符号", result.errors[0])

    def test_unknown_type_and_empty_value_are_skipped(self) -> None:
        result = self._resolve(
            [
                {"type": "banana", "value": "x"},
                {"type": "file", "value": "  "},
                "not-a-dict",
            ]
        )
        self.assertEqual(result.refs, [])
        self.assertEqual(len(result.errors), 3)

    def test_duplicate_refs_collapse(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        result = self._resolve(
            [
                {"type": "file", "value": "ok.py"},
                {"type": "file", "value": "ok.py"},
            ]
        )
        self.assertEqual(len(result.refs), 1)


class BudgetTests(RefHarness):
    """字符预算：按序截断并标记。"""

    def test_single_ref_truncated_to_budget(self) -> None:
        self._write("big.py", "x = 1\n" * 8000)
        result = self._resolve([{"type": "file", "value": "big.py"}])
        self.assertTrue(result.truncated)
        self.assertTrue(result.refs[0].truncated)
        self.assertLessEqual(result.chars, MAX_REF_CHARS)

    def test_later_refs_dropped_when_budget_exhausted(self) -> None:
        self._write("a.py", "a = 1\n" * 8000)
        self._write("b.py", PY_SAMPLE)
        result = self._resolve(
            [
                {"type": "file", "value": "a.py"},
                {"type": "file", "value": "b.py"},
            ]
        )
        self.assertTrue(result.truncated)
        self.assertEqual([ref.value for ref in result.refs], ["a.py"])
        self.assertIn("预算", result.errors[0])

    def test_custom_budget_is_respected(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        result = self._resolve([{"type": "file", "value": "ok.py"}], max_chars=50)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.refs[0].body), 50)


class CandidateTests(RefHarness):
    """`/api/refs` 背后的候选遍历（与解析共用同一口径）。"""

    def test_file_and_dir_candidates_skip_ignored_dirs(self) -> None:
        self._write("src/app.py", PY_SAMPLE)
        self._write("node_modules/pkg/index.js", "x")
        self._write(".env", "SECRET=1")
        files = list(iter_workspace_files(self.guard))
        dirs = list(iter_workspace_dirs(self.guard))
        self.assertIn("src/app.py", files)
        self.assertNotIn(".env", files)          # 敏感文件不进候选
        self.assertTrue(all("node_modules" not in p for p in files))
        self.assertIn("src", dirs)
        self.assertTrue(all("node_modules" not in p for p in dirs))

    def test_symbol_candidates_include_path_and_line(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        found = {(rel, symbol.name) for rel, symbol in iter_workspace_symbols(self.guard)}
        self.assertIn(("svc.py", "TaskService"), found)
        self.assertIn(("svc.py", "helper"), found)


class PromptTests(RefHarness):
    """拼装提交提示：无引用时必须逐字节不变。"""

    def test_no_refs_is_byte_identical(self) -> None:
        text = "把登录逻辑改掉"
        for refs in (None, [], [{"type": "nope", "value": "x"}]):
            prompt, _ = build_refs_prompt(self.guard, text, refs)
            self.assertEqual(prompt, text)

    def test_block_is_prepended_and_wrapped(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        prompt, resolution = build_refs_prompt(
            self.guard, "看看这个文件", [{"type": "file", "value": "ok.py"}]
        )
        self.assertTrue(resolution.ok)
        self.assertTrue(prompt.endswith("看看这个文件"))
        self.assertIn("<<<UNTRUSTED_DATA", prompt)
        self.assertIn("[引用上下文]", prompt)

    def test_format_block_lists_errors_alongside_good_refs(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        resolution = self._resolve(
            [
                {"type": "file", "value": "ok.py"},
                {"type": "file", "value": "../nope"},
            ]
        )
        block = format_refs_block(resolution)
        self.assertIn("未能解析的引用", block)
        self.assertIn("<<<UNTRUSTED_DATA", block)

    def test_format_block_empty_when_nothing_to_inject(self) -> None:
        from agent_core.context import RefResolution

        self.assertEqual(format_refs_block(RefResolution()), "")
        # 只有报错、没有正文时不注入任何块
        self.assertEqual(
            format_refs_block(self._resolve([{"type": "file", "value": "../nope"}])), ""
        )


class SessionRefsTests(RefHarness):
    """会话层：引用块进第一条 user 消息，且不污染会话标题。"""

    def _session(self) -> Session:
        config = Config.load(self.ws)
        return Session(config, client=object())

    def _wait(self, cond, timeout: float = 10.0) -> None:
        """等后台线程把结果写出来。

        不能 join `session._thread` —— 任务跑得太快时 `_worker` 的 finally
        会在 join 之前就把 `_thread` 置回 None。改为轮询条件。
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not cond():
            time.sleep(0.01)
        self.assertTrue(cond(), "等待后台任务结果超时")

    def test_submit_prepends_refs_block(self) -> None:
        self._write("ok.py", PY_SAMPLE)
        session = self._session()
        captured: dict[str, str] = {}
        session.agent.run = lambda text: captured.setdefault("text", text) or "done"  # type: ignore[method-assign]

        session.submit("改一下", [{"type": "file", "value": "ok.py"}])
        self._wait(lambda: "text" in captured)

        self.assertIn("[引用上下文]", captured["text"])
        self.assertIn("class TaskService", captured["text"])
        self.assertTrue(captured["text"].endswith("改一下"))
        # 标题提示仍是用户原文，不是引用块
        self.assertEqual(session._title_hint, "改一下")

    def test_submit_without_refs_passes_text_through(self) -> None:
        session = self._session()
        captured: dict[str, str] = {}
        session.agent.run = lambda text: captured.setdefault("text", text) or "done"  # type: ignore[method-assign]

        session.submit("原样提交")
        self._wait(lambda: "text" in captured)

        self.assertEqual(captured["text"], "原样提交")

    def test_submit_reports_ref_errors_as_notices(self) -> None:
        session = self._session()
        session.agent.run = lambda text: "done"  # type: ignore[method-assign]

        session.submit("x", [{"type": "file", "value": "../escape.txt"}])
        self._wait(lambda: any("引用未解析" in note for note in session._notices))


class ValidateRefsTests(unittest.TestCase):
    """`/api/run` 的 refs 形状校验 —— 前端 bug 应 400，而不是静默丢弃。"""

    def test_valid_shapes(self) -> None:
        self.assertEqual(validate_refs(None), [])
        self.assertEqual(validate_refs([]), [])
        cleaned = validate_refs([{"type": "FILE", "value": " a.py "}])
        self.assertEqual(cleaned, [{"type": "file", "value": "a.py"}])

    def test_invalid_shapes_raise(self) -> None:
        for bad in (
            "not-a-list",
            [{"type": "banana", "value": "x"}],
            [{"type": "file", "value": ""}],
            [{"type": "file", "value": "x" * 600}],
            [123],
        ):
            with self.assertRaises(ValueError):
                validate_refs(bad)

    def test_too_many_refs_raise(self) -> None:
        with self.assertRaises(ValueError):
            validate_refs([{"type": "file", "value": f"f{i}.py"} for i in range(60)])


if __name__ == "__main__":
    unittest.main()