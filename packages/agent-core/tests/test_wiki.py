"""Repo Wiki（M3+ / 阶段 C）测试。

验收口径（docs/M3+-PLAN.md §2-C）：

1. **生成结构**：总览 README + 每模块一页（文件清单 / 符号 / 依赖关系）。
2. **落点校验**：越界、`.vibe/`、工作区根目录一律被 PathGuard 拒绝。
3. **落盘恒走结构化补丁**：`plan_overwrite` + `commit_plan`；新建 vs 覆盖语义；
   落点已存在且未显式 `overwrite=true` 时**拒绝**。
4. **截断显式标注**：受 `wiki_max_files` 约束，超限在 README 与 meta 里显式说明。
5. **门控**：`wiki_enabled=False` 时工具列表里**没有** `repo_wiki`；
   `repo_wiki` **不在**只读注册表（子 Agent 恒只读）。

纯本机生成：这些断言不依赖 tree-sitter，也不触任何模型端点。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import SandboxViolation, ToolError  # noqa: E402
from agent_core.index import clear_cache  # noqa: E402
from agent_core.index.depgraph import DepGraph  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools import build_default_registry, build_readonly_registry  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.repo_wiki import (  # noqa: E402
    REPO_WIKI_SPEC,
    _preview_repo_wiki,
    _repo_wiki,
)
from agent_core.wiki import generate_wiki, module_of  # noqa: E402

SRC = {
    "auth.py": "def handle_login(user):\n    return user\n",
    "util.py": "def touch(path):\n    return path\n",
}


class _WikiRepo(unittest.TestCase):
    """公共夹具：一个带子目录 `src/` 的小仓库 + 可复用的 ToolContext。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        clear_cache()
        self._write_sources()

    def tearDown(self) -> None:
        clear_cache()
        self._tmp.cleanup()

    def _write_sources(self, files: dict[str, str] | None = None) -> None:
        for rel, text in (files or SRC).items():
            path = self.ws / "src" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")

    def _ctx(self, **overrides) -> ToolContext:
        config = Config.load(self.ws)
        if overrides:
            config = replace(config, **overrides)
        return ToolContext(
            config=config,
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=AuditLog(
                config.audit_path,
                session_id=config.session_id,
                enabled=config.audit_enabled,
            ),
        )


# --------------------------------------------------------------------------- 生成
class GenerateWikiTests(_WikiRepo):
    def test_readme_and_module_page_are_produced(self) -> None:
        rels = ["auth.py", "util.py"]
        bundle = generate_wiki(self.ws / "src", rels, DepGraph.build(rels, {}))
        names = [page.name for page in bundle.pages]
        self.assertEqual(names[0], "README.md", "总览必须是第一页")
        self.assertEqual(len(names), 2, "两个散文件同属 (root) 模块 → 一页")
        self.assertEqual(bundle.modules, 1)
        self.assertEqual(bundle.files, 2)
        self.assertFalse(bundle.truncated)

        readme = bundle.pages[0].content
        self.assertIn("# 仓库 Wiki", readme)
        self.assertIn("源文件：2", readme)
        self.assertIn("(root)", readme)
        # 目录树落在 README 里
        self.assertIn("```text", readme)

        module_page = bundle.pages[1].content
        self.assertIn("### `auth.py`", module_page)
        self.assertIn("handle_login", module_page)
        self.assertIn("[← 返回总览](README.md)", module_page)

    def test_exceeding_max_files_is_flagged(self) -> None:
        rels = ["auth.py", "util.py"]
        bundle = generate_wiki(
            self.ws / "src", rels, DepGraph.build(rels, {}), max_files=1
        )
        self.assertTrue(bundle.truncated)
        self.assertEqual(bundle.total_files, 2)
        self.assertEqual(bundle.files, 1)
        self.assertIn("⚠", bundle.pages[0].content, "截断必须在 README 显式标注")

    def test_empty_repo_still_renders_readme(self) -> None:
        bundle = generate_wiki(self.ws / "src", [], DepGraph.build([], {}))
        self.assertEqual([page.name for page in bundle.pages], ["README.md"])
        self.assertEqual(bundle.files, 0)
        self.assertEqual(bundle.modules, 0)
        self.assertIn("（空）", bundle.pages[0].content)

    def test_module_of_groups_by_top_directory(self) -> None:
        self.assertEqual(module_of("pkg/a/b.py"), "pkg")
        self.assertEqual(module_of("auth.py"), "(root)")


# --------------------------------------------------------------------------- 落盘
class RepoWikiToolTests(_WikiRepo):
    def test_creates_files_and_records_metadata_only_audit(self) -> None:
        ctx = self._ctx()
        spy = mock.patch.object(ctx.audit, "record")
        with spy as record:
            result = _repo_wiki(ctx, {"path": "src"})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["created"], 2)
        self.assertEqual(result.meta["updated"], 0)

        readme = self.ws / "docs" / "wiki" / "README.md"
        self.assertTrue(readme.is_file(), "落盘必须真的产出文件")
        self.assertIn("# 仓库 Wiki", readme.read_text(encoding="utf-8"))
        module_page = self.ws / "docs" / "wiki" / "01-src.md"
        self.assertTrue(module_page.is_file())
        # 子目录扫描时 rel 是工作区相对路径，符号仍必须被抽出（回归：曾静默丢失）
        self.assertIn("handle_login", module_page.read_text(encoding="utf-8"))

        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], "tool.repo_wiki")
        # 审计只记元数据（ADR-006）：字段固定，绝不含 Wiki 正文
        self.assertEqual(
            set(record.call_args.kwargs),
            {"path", "files", "created", "updated", "bytes", "truncated"},
        )

    def test_existing_target_requires_explicit_overwrite(self) -> None:
        _repo_wiki(self._ctx(), {"path": "src"})
        with self.assertRaises(ToolError):
            _repo_wiki(self._ctx(), {"path": "src"})

    def test_overwrite_updates_changed_content(self) -> None:
        _repo_wiki(self._ctx(), {"path": "src"})
        # 改一个符号名 → 模块页内容变化 → 覆盖时计入 updated
        self._write_sources({"auth.py": "def handle_signin(user):\n    return user\n"})
        result = _repo_wiki(self._ctx(), {"path": "src", "overwrite": True})
        self.assertEqual(result.meta["created"], 0)
        self.assertEqual(result.meta["updated"], 1)
        page = (self.ws / "docs" / "wiki" / "01-src.md").read_text(encoding="utf-8")
        self.assertIn("handle_signin", page)

    def test_target_outside_workspace_is_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            _repo_wiki(self._ctx(), {"path": "src", "target": "../escape"})

    def test_target_inside_vibe_dir_is_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            _repo_wiki(self._ctx(), {"path": "src", "target": ".vibe/wiki"})

    def test_workspace_root_as_target_is_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            _repo_wiki(self._ctx(), {"path": "src", "target": "."})

    def test_write_disabled_is_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            _repo_wiki(self._ctx(allow_write=False), {"path": "src"})

    def test_truncation_surfaces_in_meta(self) -> None:
        result = _repo_wiki(self._ctx(wiki_max_files=1), {"path": "src"})
        self.assertTrue(result.meta["truncated"])


# --------------------------------------------------------------------------- 预演
class RepoWikiPreviewTests(_WikiRepo):
    def test_preview_lists_every_planned_file(self) -> None:
        preview = _preview_repo_wiki(self._ctx(), {"path": "src"})
        self.assertEqual(preview.kind, "diff")
        self.assertIn("docs/wiki/README.md", preview.body)
        self.assertIn("docs/wiki/01-src.md", preview.body)

    def test_preview_errors_when_target_exists_without_overwrite(self) -> None:
        _repo_wiki(self._ctx(), {"path": "src"})
        preview = _preview_repo_wiki(self._ctx(), {"path": "src"})
        self.assertEqual(preview.kind, "error")
        self.assertTrue(preview.blocking)

    def test_preview_errors_when_write_disabled(self) -> None:
        preview = _preview_repo_wiki(self._ctx(allow_write=False), {"path": "src"})
        self.assertEqual(preview.kind, "error")


# --------------------------------------------------------------------------- 门控
class RepoWikiGateTests(unittest.TestCase):
    def test_tool_registered_only_when_enabled(self) -> None:
        self.assertNotIn("repo_wiki", build_default_registry(lsp=False).names())
        self.assertIn("repo_wiki", build_default_registry(lsp=False, wiki=True).names())

    def test_spec_is_review_risk(self) -> None:
        from agent_core.sandbox import Risk

        self.assertEqual(REPO_WIKI_SPEC.risk, Risk.REVIEW)

    def test_repo_wiki_is_not_in_readonly_registry(self) -> None:
        """子 Agent 恒只读：写盘工具不得出现在只读白名单里。"""
        self.assertNotIn("repo_wiki", build_readonly_registry(lsp=False).names())


if __name__ == "__main__":
    unittest.main()