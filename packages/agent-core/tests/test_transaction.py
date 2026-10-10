"""多文件原子编辑（事务，P1-8）测试。

重点不在「能不能一次改多个文件」，而在三件**静默灾难**：

1. **改了一半**。第 k 个文件失败时前 k-1 个已经写进去 —— 仓库既不是改动前、
   也不是改动后，还得靠人去分辨哪些该留哪些该退。这里钉住「要么全落盘、
   要么全不落盘」。
2. **回滚不彻底**。写失败后回滚必须**真的**把已写文件恢复为原内容（含新建
   文件被删掉），而不是「嘴上说回滚了」。
3. **审计泄正文**。事务涉及多个文件，最容易被顺手把 diff 一并记进审计；
   按 ADR-006 只能记计数与结果标志。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.edit import (  # noqa: E402
    MAX_EDIT_FILES,
    EditTransaction,
    TransactionError,
)
from agent_core.errors import PatchError, ToolError  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools import ToolContext, build_default_registry  # noqa: E402


class TransactionCoreTests(unittest.TestCase):
    """直接驱动 EditTransaction：整批校验 / 整批落盘 / 失败回滚。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.paths = PathGuard(self.ws, allow_sensitive=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> None:
        (self.ws / name).write_text(text, encoding="utf-8")

    def _read(self, name: str) -> str:
        return (self.ws / name).read_text(encoding="utf-8")

    # ---- ① 全部成功 ----
    def test_all_files_commit(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            self._write(name, "hello\n")
        txn = EditTransaction(paths=self.paths)
        for name in ("a.txt", "b.txt", "c.txt"):
            txn.add_patch(name, "hello", "hi")
        report = txn.commit()
        self.assertEqual(report.changed_count, 3)
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertEqual(self._read(name), "hi\n")

    def test_no_change_is_noop(self) -> None:
        self._write("a.txt", "same\n")
        txn = EditTransaction(paths=self.paths)
        txn.add_overwrite("a.txt", "same\n")
        report = txn.commit()
        self.assertEqual(report.changed_count, 0)
        self.assertEqual(self._read("a.txt"), "same\n")

    # ---- ② 阶段一失败：整体不写 ----
    def test_unmatched_search_writes_nothing(self) -> None:
        self._write("a.txt", "hello\n")
        self._write("b.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        txn.add_patch("a.txt", "hello", "hi")  # 能命中
        with self.assertRaises(PatchError):
            txn.add_patch("b.txt", "不存在的片段", "hi")  # 命中失败 → 抛
        # 关键：a.txt 一个字都不能写进去
        self.assertEqual(self._read("a.txt"), "hello\n")
        self.assertEqual(self._read("b.txt"), "hello\n")

    def test_expected_digest_mismatch_aborts_batch(self) -> None:
        self._write("a.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        with self.assertRaises(PatchError):
            txn.add_patch("a.txt", "hello", "hi", expected_digest="deadbeefdeadbeef")
        self.assertEqual(self._read("a.txt"), "hello\n")

    def test_too_many_files_rejected(self) -> None:
        self._write("a.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        for _ in range(MAX_EDIT_FILES):
            txn.add_patch("a.txt", "hello", "hi")
        with self.assertRaises(PatchError):
            txn.add_patch("a.txt", "hello", "hi")

    def test_overwrite_existing_requires_flag(self) -> None:
        self._write("a.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        with self.assertRaises(ToolError):
            txn.add_overwrite("a.txt", "new\n", overwrite=False)

    # ---- ③ 阶段二失败：回滚已写文件 ----
    def test_commit_failure_rolls_back_written_files(self) -> None:
        self._write("a.txt", "hello\n")
        self._write("b.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        txn.add_patch("a.txt", "hello", "AAA")
        txn.add_patch("b.txt", "hello", "BBB")

        import agent_core.edit.transaction as txn_mod

        real = txn_mod.commit_plan

        def flaky(plan):
            if plan.rel_path == "b.txt":
                raise OSError("磁盘只读（模拟）")
            return real(plan)

        with mock.patch.object(txn_mod, "commit_plan", side_effect=flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        self.assertTrue(ctx.exception.rolled_back)
        # a.txt 先写成功、随后被回滚为原内容；b.txt 根本没写成
        self.assertEqual(self._read("a.txt"), "hello\n")
        self.assertEqual(self._read("b.txt"), "hello\n")

    def test_rollback_removes_created_file(self) -> None:
        self._write("keep.txt", "hello\n")
        txn = EditTransaction(paths=self.paths)
        txn.add_overwrite("new.txt", "brand new\n")  # 新建
        txn.add_patch("keep.txt", "hello", "hi")
        # 让第二项（keep.txt）在落盘时失败，从而回滚已新建的 new.txt
        import agent_core.edit.transaction as txn_mod

        real = txn_mod.commit_plan

        def flaky(plan):
            if plan.rel_path == "keep.txt":
                raise OSError("磁盘只读（模拟）")
            return real(plan)

        with mock.patch.object(txn_mod, "commit_plan", side_effect=flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        self.assertTrue(ctx.exception.rolled_back)
        self.assertFalse((self.ws / "new.txt").exists(), "回滚必须把新建文件删掉")
        self.assertEqual(self._read("keep.txt"), "hello\n")

    def test_snapshot_called_before_each_write(self) -> None:
        self._write("a.txt", "hello\n")
        seen: list[tuple[str, str]] = []
        txn = EditTransaction(paths=self.paths, snapshot=lambda p, r: seen.append((p.name, r)))
        txn.add_patch("a.txt", "hello", "hi")
        txn.commit()
        self.assertEqual(seen, [("a.txt", "a.txt")])


class EditFilesToolTests(unittest.TestCase):
    """工具层：注册 / 校验 / 审批预演 / 审计只记计数。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, *, audit_enabled: bool = False) -> ToolContext:
        config = Config.load(self.ws)
        return ToolContext(
            config=config,
            paths=PathGuard(config.workspace, allow_sensitive=False),
            commands=CommandGuard(config.workspace, allow_network=False),
            audit=AuditLog(config.audit_path, session_id=config.session_id, enabled=audit_enabled),
        )

    def _run(self, args: dict, ctx: ToolContext | None = None):
        registry = build_default_registry(lsp=False)
        return registry.execute(ctx or self._ctx(), "edit_files", args)

    def test_registered_as_review_tool(self) -> None:
        registry = build_default_registry(lsp=False)
        spec = registry.get("edit_files")
        self.assertIsNotNone(spec, "edit_files 必须默认注册（I4）")
        self.assertEqual(spec.risk.value, "review", "多文件写盘是 REVIEW，恒走审批")
        self.assertIn("edit_files", registry.names())

    def test_rejects_empty_and_bad_shapes(self) -> None:
        self.assertFalse(self._run({"edits": []}).ok)
        self.assertFalse(self._run({"edits": "改一下"}).ok)
        self.assertFalse(self._run({"edits": [{"path": ""}]}).ok, "缺 path 应整批拒绝")
        self.assertFalse(
            self._run({"edits": [{"path": "a.txt", "search": "x", "replace": "y", "content": "z"}]}).ok,
            "同时给 search/replace 与 content 应拒绝",
        )
        self.assertFalse(self._run({"edits": [{"path": "a.txt"}]}).ok, "两种模式都没给应拒绝")

    def test_rejects_duplicate_paths(self) -> None:
        self.assertFalse(
            self._run({"edits": [{"path": "a.txt", "search": "a", "replace": "b"},
                                 {"path": "a.txt", "search": "a", "replace": "c"}]}).ok
        )

    def test_rejects_too_many_files(self) -> None:
        edits = [{"path": f"f{i}.txt", "content": "x"} for i in range(MAX_EDIT_FILES + 1)]
        result = self._run({"edits": edits})
        self.assertFalse(result.ok)
        self.assertIn("最多", result.content)

    def test_happy_path_writes_all_and_records_changes(self) -> None:
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")
        (self.ws / "b.txt").write_text("hello\n", encoding="utf-8")
        ctx = self._ctx()
        result = self._run(
            {
                "edits": [
                    {"path": "a.txt", "search": "hello", "replace": "hi"},
                    {"path": "b.txt", "search": "hello", "replace": "hi"},
                ]
            },
            ctx,
        )
        self.assertTrue(result.ok, result.content)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "hi\n")
        self.assertEqual((self.ws / "b.txt").read_text(encoding="utf-8"), "hi\n")
        self.assertEqual(sorted(c.path for c in ctx.changes), ["a.txt", "b.txt"])
        self.assertEqual(result.meta["changed"], 2)

    def test_audit_records_counts_only(self) -> None:
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")
        (self.ws / "b.txt").write_text("hello\n", encoding="utf-8")
        ctx = self._ctx()
        with mock.patch.object(ctx.audit, "record") as record:
            self._run(
                {
                    "edits": [
                        {"path": "a.txt", "search": "hello", "replace": "hi"},
                        {"path": "b.txt", "search": "hello", "replace": "hi"},
                    ]
                },
                ctx,
            )
        call = next(c for c in record.call_args_list if c.args[0] == "edit.transaction")
        self.assertEqual(call.kwargs["files_count"], 2)
        self.assertEqual(call.kwargs["changed_count"], 2)
        self.assertEqual(call.kwargs["strategy"], "atomic")
        self.assertFalse(call.kwargs["rolled_back"])
        # 不允许把正文类东西塞进审计
        joined = " ".join(call.kwargs.keys())
        for forbidden in ("diff", "content", "search", "replace", "body", "text"):
            self.assertNotIn(forbidden, joined)

    def test_previewer_is_blocking_on_unmatched_search(self) -> None:
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")
        registry = build_default_registry(lsp=False)
        spec = registry.get("edit_files")
        preview = spec.preview(
            self._ctx(),
            {"edits": [{"path": "a.txt", "search": "不存在的片段", "replace": "x"}]},
        )
        self.assertEqual(preview.kind, "error")
        self.assertTrue(preview.blocking)

    def test_previewer_summarizes_multiple_files(self) -> None:
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")
        (self.ws / "b.txt").write_text("hello\n", encoding="utf-8")
        registry = build_default_registry(lsp=False)
        preview = registry.get("edit_files").preview(
            self._ctx(),
            {
                "edits": [
                    {"path": "a.txt", "search": "hello", "replace": "hi"},
                    {"path": "b.txt", "search": "hello", "replace": "bye"},
                ]
            },
        )
        self.assertEqual(preview.kind, "diff")
        self.assertIn("a.txt", preview.body)
        self.assertIn("b.txt", preview.body)
        self.assertIn("原子事务", preview.summary)


if __name__ == "__main__":
    unittest.main()