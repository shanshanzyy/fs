"""Skill / 斜杠命令（M9-A）测试。

验证五条纪律：
1. 加载（扫描 / front-matter 解析 / 坏文件跳过 / 超限截断 / 关闭时不加载）；
2. `run_skill` 工具（SAFE / 展开 / 未知 name 报错 / 不进只读注册表）；
3. 提示词段（注入 name+description、正文不注入、`wrap_untrusted` 包裹、
   关闭时逐字节一致）；
4. 审计只记元数据（name / chars / origin，正文不出机）；
5. 默认关闭时工具清单与提示词与未实现前逐字节一致。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.loop import Agent  # noqa: E402
from agent_core.sandbox import CommandGuard, PathGuard, Risk  # noqa: E402
from agent_core.skill import load_skills  # noqa: E402
from agent_core.tools import ToolContext, build_default_registry, build_readonly_registry  # noqa: E402
from agent_core.tools.base import UNTRUSTED_BEGIN  # noqa: E402


def _write_skill(root: Path, name: str, body: str, description: str = "") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if description:
        text = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    else:
        text = body
    (d / "SKILL.md").write_text(text, encoding="utf-8")


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scans_and_parses_front_matter(self) -> None:
        _write_skill(self.root, "commit", "用 git_diff 看改动\n", description="生成提交信息")
        skills, warnings = load_skills(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual([s.name for s in skills], ["commit"])
        s = skills[0]
        self.assertEqual(s.description, "生成提交信息")
        self.assertIn("git_diff", s.body)
        self.assertEqual(s.source, "commit/SKILL.md")

    def test_name_defaults_to_dir_when_no_front_matter(self) -> None:
        _write_skill(self.root, "review", "先看 diff 再给意见\n")
        skills, _ = load_skills(self.root)
        self.assertEqual([s.name for s in skills], ["review"])
        # 无 front-matter 时 description 取正文首行
        self.assertEqual(skills[0].description, "先看 diff 再给意见")

    def test_bad_files_skipped_with_warning(self) -> None:
        _write_skill(self.root, "good", "正文\n")
        # 空正文 = 坏文件
        (self.root / "bad").mkdir()
        (self.root / "bad" / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
        skills, warnings = load_skills(self.root)
        self.assertEqual([s.name for s in skills], ["good"])
        self.assertTrue(any("正文为空" in w for w in warnings), warnings)

    def test_duplicate_names_skipped(self) -> None:
        _write_skill(self.root, "a", "x", description="first")
        (self.root / "b").mkdir()
        (self.root / "b" / "SKILL.md").write_text(
            "---\nname: a\ndescription: dup\n---\n正文\n", encoding="utf-8"
        )
        skills, warnings = load_skills(self.root)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].description, "first")
        self.assertTrue(any("重名" in w for w in warnings), warnings)

    def test_max_files_drops_overflow_with_warning(self) -> None:
        for i in range(5):
            _write_skill(self.root, f"s{i}", f"正文{i}\n")
        skills, warnings = load_skills(self.root, max_files=2)
        self.assertEqual(len(skills), 2)
        self.assertTrue(any("文件数超过上限" in w for w in warnings), warnings)

    def test_max_bytes_drops_overflow_with_warning(self) -> None:
        _write_skill(self.root, "a", "x" * 100 + "\n")
        _write_skill(self.root, "b", "y" * 100 + "\n")
        skills, warnings = load_skills(self.root, max_bytes=150)
        # a（100 字节正文）放得下，b 会超 150，从队首之后整体丢弃
        self.assertEqual([s.name for s in skills], ["a"])
        self.assertTrue(any("总量超过上限" in w for w in warnings), warnings)

    def test_empty_dir_returns_empty(self) -> None:
        skills, warnings = load_skills(self.root)
        self.assertEqual(skills, [])
        self.assertEqual(warnings, [])


class RegistryTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        reg = build_default_registry()
        self.assertNotIn("run_skill", reg.names())

    def test_enabled_registers_run_skill(self) -> None:
        reg = build_default_registry(skills=True)
        self.assertIn("run_skill", reg.names())
        spec = reg.get("run_skill")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, Risk.SAFE)

    def test_not_in_readonly_registry(self) -> None:
        ro = build_readonly_registry(lsp=False)
        self.assertNotIn("run_skill", ro.names())


class RunSkillToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.audit = AuditLog(self.ws / "audit.jsonl", session_id="t", enabled=True)
        self.ctx = ToolContext(
            config=Config.load(self.ws),
            paths=PathGuard(self.ws),
            commands=CommandGuard(self.ws),
            audit=self.audit,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_expands_body_wrapped_untrusted(self) -> None:
        _write_skill(self.ws / ".vibe/skills", "commit", "步骤：用 git_diff 看改动\n")
        skills, _ = load_skills(self.ws / ".vibe/skills")
        self.ctx.skills = skills
        reg = build_default_registry(skills=True)
        result = reg.execute(self.ctx, "run_skill", {"name": "commit"})
        self.assertTrue(result.ok, result.content)
        self.assertIn("git_diff", result.content)
        self.assertIn(UNTRUSTED_BEGIN, result.content)

    def test_unknown_name_errors(self) -> None:
        _write_skill(self.ws / ".vibe/skills", "commit", "正文\n")
        skills, _ = load_skills(self.ws / ".vibe/skills")
        self.ctx.skills = skills
        reg = build_default_registry(skills=True)
        result = reg.execute(self.ctx, "run_skill", {"name": "nope"})
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.content)

    def test_disabled_reports_not_enabled(self) -> None:
        self.ctx.skills = None
        reg = build_default_registry(skills=True)
        result = reg.execute(self.ctx, "run_skill", {"name": "commit"})
        self.assertFalse(result.ok)
        self.assertIn("未启用", result.content)

    def test_audit_records_metadata_only(self) -> None:
        _write_skill(self.ws / ".vibe/skills", "commit", "秘密正文secret\n")
        skills, _ = load_skills(self.ws / ".vibe/skills")
        self.ctx.skills = skills
        reg = build_default_registry(skills=True)
        reg.execute(self.ctx, "run_skill", {"name": "commit"})
        lines = [
            json.loads(l)
            for l in self.ws.joinpath("audit.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        skill_events = [e for e in lines if e["event"] == "skill.run"]
        self.assertEqual(len(skill_events), 1)
        ev = skill_events[0]
        self.assertEqual(ev["name"], "commit")
        self.assertIn("chars", ev)
        self.assertIn("source", ev)  # 本地字段名
        self.assertNotIn("secret", json.dumps(ev, ensure_ascii=False))


class PromptSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_prompt_unchanged(self) -> None:
        _write_skill(self.ws / ".vibe/skills", "commit", "正文\n", description="desc")
        agent = Agent(Config.load(self.ws))  # 默认关
        self.assertNotIn("run_skill", agent.registry.names())
        self.assertNotIn("可用工作流", agent._base_system_prompt)
        self.assertNotIn("commit", agent._base_system_prompt)

    def test_enabled_injects_name_description_only(self) -> None:
        _write_skill(self.ws / ".vibe/skills", "commit", "这是正文BODY\n", description="生成提交信息")
        agent = Agent(Config.load(self.ws, skills_enabled=True))
        self.assertIn("run_skill", agent.registry.names())
        self.assertIn("可用工作流", agent._base_system_prompt)
        self.assertIn("commit: 生成提交信息", agent._base_system_prompt)
        # 正文不整段注入
        self.assertNotIn("这是正文BODY", agent._base_system_prompt)
        # ctx.skills 挂载
        self.assertEqual([s.name for s in agent.ctx.skills], ["commit"])

    def test_empty_dir_no_section(self) -> None:
        agent = Agent(Config.load(self.ws, skills_enabled=True))
        self.assertNotIn("可用工作流", agent._base_system_prompt)


if __name__ == "__main__":
    unittest.main()
