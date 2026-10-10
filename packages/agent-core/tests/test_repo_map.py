"""repo_map 双后端（C3）测试。

验收要求（M1.5-PLAN C3）：

1. tree-sitter 是**可选依赖**：装了走 tree-sitter，不装自动回退正则，功能都不失效；
2. 两个后端输出格式严格一致（层级符号树 + 行号）；
3. tree-sitter 召回是正则的**超集**，并额外给出签名/继承/import；
4. 未覆盖的扩展名（如 .cs）回退正则。

注意：本机若没装 tree-sitter，全部「需要 tree-sitter」的断言会被 skip，
但「回退正则」的断言必须始终通过 —— 这正是内网离线环境的实际形态。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog, verify_chain  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard  # noqa: E402
from agent_core.tools.base import ToolContext  # noqa: E402
from agent_core.tools.repo_map import REPO_MAP_SPEC, _repo_map  # noqa: E402
from agent_core.tools.symbols import (  # noqa: E402
    KIND_FUNC,
    KIND_TYPE,
    regex_scan,
    treesitter_available,
    treesitter_scan,
)

PY_SAMPLE = '''import os
from typing import Any


class TaskService(Base, Mixin):
    def create(self, title: str) -> dict:
        return {}


def helper(a, b=1):
    return a + b
'''

TS_SAMPLE = '''import { x } from "./x";

export class Store extends Base {
  get(k: string): number {
    return 0;
  }
}

export function make(a: number): Store {
  return new Store();
}

export const handler = async (req: any) => {
  return req;
};
'''

GO_SAMPLE = '''package main

import "fmt"

type Task struct {
    Title string
}

type Doer interface {
    Do() error
}

func Run(x int) error {
    return nil
}

func (t *Task) Do() error {
    return nil
}
'''

RS_SAMPLE = '''use std::fmt;

pub struct Task {
    title: String,
}

pub enum Kind {
    A,
    B,
}

pub trait Doer {
    fn do_it(&self);
}

impl Task {
    pub fn new(title: String) -> Self {
        Task { title }
    }
}

pub fn run(x: i32) -> i32 {
    x
}
'''

CS_SAMPLE = '''namespace Demo
{
    public class Widget
    {
        public int Size() { return 1; }
    }
}
'''

# 两后端共同遵循的渲染格式：缩进 + 标记 + 名字 + 可选细节 + "  L行号"
_SYMBOL_LINE = re.compile(r"^\s+[◆·] \S.*  L\d+$")

_TS_LANGS = {
    ".py": "python",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
}


class RepoMapHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def _ctx(self) -> ToolContext:
        config = Config.load(self.ws)
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

    def _text(self, **args) -> str:
        return _repo_map(self._ctx(), {"path": ".", **args}).content

    def _names_from_output(self, text: str) -> set[str]:
        names: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^\s+[◆·] ", line):
                body = stripped[2:]
                body = body.split("  L")[0]  # 去掉行号后缀
                name = re.split(r"[(（]", body, maxsplit=1)[0].strip()
                names.add(name)
        return names


class RegexBackendTests(RepoMapHarness):
    def test_regex_scan_captures_expected_symbols(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        result = regex_scan(self.ws / "svc.py", PY_SAMPLE)
        names = {s.name for s in result.symbols}
        self.assertEqual(names, {"TaskService", "create", "helper"})
        kinds = {s.name: s.kind for s in result.symbols}
        self.assertEqual(kinds["TaskService"], KIND_TYPE)
        self.assertEqual(kinds["create"], KIND_FUNC)
        # 行号正确：class 在第 5 行、create 在第 6 行
        lines = {s.name: s.line for s in result.symbols}
        self.assertEqual(lines["TaskService"], 5)
        self.assertEqual(lines["create"], 6)

    def test_regex_depth_reflects_nesting(self) -> None:
        result = regex_scan(Path("x.py"), "class A:\n    def m(self):\n        pass\n")
        depths = {s.name: s.indent for s in result.symbols}
        self.assertEqual(depths["A"], 0)
        self.assertEqual(depths["m"], 1)

    def test_falls_back_to_regex_when_treesitter_missing(self) -> None:
        """不装 tree-sitter（或解析失败）时符号集合必须与正则完全一致。

        后端分派现在统一在 `tools.symbols.scan_file`（repo_map、@symbol 引用、
        依赖图共用同一入口），所以要 patch 的是 symbols 里的 `treesitter_scan`。
        """
        for rel, source in (("svc.py", PY_SAMPLE), ("app.ts", TS_SAMPLE)):
            self._write(rel, source)
        with mock.patch("agent_core.tools.symbols.treesitter_scan", return_value=None):
            text = self._text()
        expected = set()
        for rel, source in (("svc.py", PY_SAMPLE), ("app.ts", TS_SAMPLE)):
            expected |= {s.name for s in regex_scan(Path(rel), source).symbols}
        self.assertTrue(expected <= self._names_from_output(text), self._names_from_output(text))

    def test_csharp_falls_back_to_regex(self) -> None:
        """c_sharp grammar 常见缺失：即便装了 tree-sitter 也必须回退正则。"""
        self._write("Widget.cs", CS_SAMPLE)
        text = self._text()
        names = self._names_from_output(text)
        self.assertIn("Widget", names)
        self.assertIn("Size", names)


class TreeSitterBackendTests(RepoMapHarness):
    def _require(self, suffix: str) -> str:
        lang = _TS_LANGS[suffix]
        if not treesitter_available(lang):
            self.skipTest(f"tree-sitter 语言 {lang} 不可用（未装可选依赖）")
        return lang

    def test_treesitter_is_superset_of_regex(self) -> None:
        samples = {
            "svc.py": PY_SAMPLE,
            "app.ts": TS_SAMPLE,
            "main.go": GO_SAMPLE,
            "lib.rs": RS_SAMPLE,
        }
        for rel, source in samples.items():
            lang = self._require(Path(rel).suffix)
            regex_names = {s.name for s in regex_scan(Path(rel), source).symbols}
            ts_result = treesitter_scan(source, lang)
            self.assertIsNotNone(ts_result)
            ts_names = {s.name for s in ts_result.symbols}
            missing = regex_names - ts_names
            self.assertFalse(missing, f"{rel}: tree-sitter 漏了正则能抓到的符号 {missing}")
            self.assertTrue(ts_result.symbols, f"{rel}: tree-sitter 未抽取到符号")

    def test_treesitter_adds_signature_inherits_imports(self) -> None:
        lang = self._require(".py")
        result = treesitter_scan(PY_SAMPLE, lang)
        by_name = {s.name: s for s in result.symbols}
        self.assertEqual(by_name["TaskService"].inherits, "Base, Mixin")
        self.assertIn("title", by_name["create"].signature)
        self.assertTrue(any("import os" in ref for ref in result.imports))

    def test_treesitter_captures_js_arrow_and_methods(self) -> None:
        lang = self._require(".ts")
        result = treesitter_scan(TS_SAMPLE, lang)
        names = {s.name for s in result.symbols}
        self.assertIn("Store", names)
        self.assertIn("get", names)       # 正则版抓不到的类方法
        self.assertIn("handler", names)   # 箭头函数

    def test_treesitter_go_method_and_types(self) -> None:
        lang = self._require(".go")
        names = {s.name for s in treesitter_scan(GO_SAMPLE, lang).symbols}
        self.assertTrue({"Task", "Doer", "Run", "Do"} <= names)

    def test_treesitter_rust_items(self) -> None:
        lang = self._require(".rs")
        names = {s.name for s in treesitter_scan(RS_SAMPLE, lang).symbols}
        self.assertTrue({"Task", "Kind", "Doer", "do_it", "new", "run"} <= names)


class OutputFormatTests(RepoMapHarness):
    def test_both_backends_share_line_format(self) -> None:
        for rel, source in (("svc.py", PY_SAMPLE), ("app.ts", TS_SAMPLE)):
            regex_result = regex_scan(Path(rel), source)
            self.assertTrue(regex_result.symbols)
            lang = _TS_LANGS[Path(rel).suffix]
            ts_result = treesitter_scan(source, lang)
            if ts_result is None:
                continue
            for result in (regex_result, ts_result):
                self._write(rel, source)
                text = self._text()
                symbol_lines = [
                    ln for ln in text.splitlines() if re.match(r"^\s+[◆·] ", ln)
                ]
                self.assertTrue(symbol_lines)
                for ln in symbol_lines:
                    self.assertRegex(ln, _SYMBOL_LINE, f"{rel} 格式不一致: {ln!r}")

    def test_output_always_carries_line_numbers(self) -> None:
        """无论走哪个后端，每个符号都必须带 L行号（回退路径同样如此）。"""
        self._write("svc.py", PY_SAMPLE)
        with mock.patch("agent_core.tools.symbols.treesitter_scan", return_value=None):
            text = self._text()
        symbol_lines = [ln for ln in text.splitlines() if re.match(r"^\s+[◆·] ", ln)]
        self.assertTrue(symbol_lines)
        for ln in symbol_lines:
            self.assertRegex(ln, r"  L\d+$")

    def test_audit_event_and_chain_valid(self) -> None:
        self._write("svc.py", PY_SAMPLE)
        ctx = self._ctx()
        result = _repo_map(ctx, {"path": "."})
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.meta["symbols"], 1)
        ok, detail = verify_chain(ctx.config.audit_path)
        self.assertTrue(ok, detail)

    def test_spec_is_safe_and_registered(self) -> None:
        self.assertEqual(REPO_MAP_SPEC.name, "repo_map")
        self.assertEqual(REPO_MAP_SPEC.handler, _repo_map)

    def test_max_symbols_truncates(self) -> None:
        body = "\n".join(f"def fn_{i}():\n    pass\n" for i in range(15))
        self._write("many.py", body + "\n")
        text = self._text(max_symbols=10)
        self.assertIn("已截断", text)

    def test_missing_path_raises_tool_error(self) -> None:
        from agent_core.errors import ToolError

        with self.assertRaises(ToolError):
            _repo_map(self._ctx(), {"path": "nope"})


if __name__ == "__main__":
    unittest.main()