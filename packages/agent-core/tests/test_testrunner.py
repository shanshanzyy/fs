"""IDE 内测试树（K / P2-14）的回归用例。

钉住四件事（计划 §8-K8）：

1. 用**临时工作区里的极小 pytest 用例**做 collect / run —— 收集出的 node id 与
   `--collect-only -q` 一致，跑完能拿到结构化的通过/失败；
2. junit 解析把「失败用例 + 定义行号」正确抽出来（这是「点失败项跳到哪一行」的
   唯一数据来源）；
3. 非零退出码但报告解析成功时**不炸**（失败是正常结果，不是异常）；
4. 未配 `test_command` 时端点回「未配置」而不是 500。

本机没装 pytest 时，依赖真跑的子集 `skipTest`；纯解析与配置的用例照跑。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.config import Config  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.host.session import Session  # noqa: E402
from agent_core.sandbox import CommandGuard  # noqa: E402
from agent_core.testing import collect, parse_junit, run  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402

HAVE_PYTEST = importlib.util.find_spec("pytest") is not None

# 极小用例集：一个通过、一个失败。失败发生在第 6 行的断言行 ——
# 这正是「点失败项跳到哪一行」要落的位置（定义行在第 5 行，跳它没有意义）。
SAMPLE = (
    "def test_ok():\n"
    "    assert 1 + 1 == 2\n"
    "\n"
    "\n"
    "def test_fail():\n"
    "    assert 1 + 1 == 3\n"
)
SAMPLE_FILE = "tests/test_sample.py"
FAIL_ASSERT_LINE = 6

# 刻意用裸命令名 `python` 而不是 sys.executable：命令沙箱拒绝以**绝对路径**
# 调用可执行文件（绕过 basename 白名单的那条路），测试命令也该遵守同一条规矩。
TEST_COMMAND = "python -m pytest -q"


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "tests").mkdir(parents=True, exist_ok=True)
        (self.ws / SAMPLE_FILE).write_text(SAMPLE, encoding="utf-8", newline="")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def guard(self) -> CommandGuard:
        return CommandGuard(self.ws)

    def config(self, command: str = TEST_COMMAND) -> Config:
        return replace(Config.load(self.ws), test_command=command)


class ConfigConventionTests(_Workspace):
    """K1：测试命令的两级配置兜底（环境变量 > VIBE.md 的 `test:` 行 > 空）。"""

    def test_vibe_md_test_line(self) -> None:
        (self.ws / "VIBE.md").write_text(f"test: {TEST_COMMAND}\n", encoding="utf-8")
        self.assertEqual(Config.load(self.ws).test_command, TEST_COMMAND)

    def test_list_prefix_supported(self) -> None:
        (self.ws / ".vibe").mkdir(exist_ok=True)
        (self.ws / ".vibe" / "VIBE.md").write_text(
            "- test: python -m pytest -q\n", encoding="utf-8"
        )
        self.assertEqual(Config.load(self.ws).test_command, "python -m pytest -q")

    def test_env_var_wins(self) -> None:
        (self.ws / "VIBE.md").write_text("test: python -m pytest\n", encoding="utf-8")
        os.environ["VIBE_TEST_COMMAND"] = TEST_COMMAND
        try:
            config = Config.load(self.ws)
        finally:
            os.environ.pop("VIBE_TEST_COMMAND", None)
        self.assertEqual(config.test_command, TEST_COMMAND)

    def test_empty_by_default_and_default_collect_args(self) -> None:
        config = Config.load(self.ws)
        self.assertEqual(config.test_command, "")
        self.assertEqual(config.test_collect_args, "--collect-only -q")


class JunitParseTests(unittest.TestCase):
    """纯解析：不需要 pytest 也能跑，且是反向验证（K9）的靶子。"""

    XML = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<testsuites>\n"
        '  <testsuite name="pytest" tests="3" failures="1" errors="0" skipped="1">\n'
        '    <testcase classname="tests.test_sample" name="test_ok" '
        'file="tests/test_sample.py" line="1" time="0.001" />\n'
        '    <testcase classname="tests.test_sample" name="test_fail" '
        'file="tests/test_sample.py" line="5" time="0.002">\n'
        '      <failure message="assert 2 == 3">traceback…</failure>\n'
        "    </testcase>\n"
        '    <testcase classname="tests.test_sample" name="test_skip" '
        'file="tests/test_sample.py" line="9" time="0.000">\n'
        '      <skipped message="no reason" />\n'
        "    </testcase>\n"
        "  </testsuite>\n"
        "</testsuites>\n"
    )

    def test_failure_keeps_file_and_definition_line(self) -> None:
        """有 `file`/`line` 属性时按属性走（老 pytest 只有定义行）。"""
        results = parse_junit(self.XML)
        failed = [item for item in results if item.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].nodeId, f"{SAMPLE_FILE}::test_fail")
        self.assertEqual(failed[0].failureFile, SAMPLE_FILE)
        self.assertEqual(failed[0].failureLine, 5)
        self.assertIn("assert 2 == 3", failed[0].messageHead)

    def test_traceback_location_wins_over_missing_attributes(self) -> None:
        """没有 `file`/`line` 属性时（本机 pytest 即如此），从失败正文里取。

        traceback 末行是真正断言的那一行 —— 它比定义行更适合当导航目标。
        """
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="tests.test_sample" name="test_fail" time="0.01">'
            '<failure message="assert (1 + 1) == 3">def test_fail():\n'
            "&gt;       assert 1 + 1 == 3\n"
            "E       assert (1 + 1) == 3\n"
            "\n"
            "tests\\test_sample.py:6: AssertionError</failure>"
            "</testcase></testsuite></testsuites>"
        )
        item = parse_junit(xml)[0]
        self.assertEqual(item.nodeId, f"{SAMPLE_FILE}::test_fail")
        self.assertEqual(item.failureFile, SAMPLE_FILE)
        self.assertEqual(item.failureLine, 6)

    def test_node_id_resolved_from_workspace_when_all_attributes_missing(self) -> None:
        """属性与正文都取不到位置时，用「classname + 工作区真实文件」反推。"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "tests").mkdir(parents=True, exist_ok=True)
            (ws / SAMPLE_FILE).write_text(SAMPLE, encoding="utf-8", newline="")
            xml = (
                '<testsuites><testsuite name="pytest">'
                '<testcase classname="tests.test_sample" name="test_ok" time="0.01" />'
                "</testsuite></testsuites>"
            )
            item = parse_junit(xml, ws)[0]
        self.assertEqual(item.nodeId, f"{SAMPLE_FILE}::test_ok")

    def test_statuses_and_passed_has_no_position(self) -> None:
        by_name = {item.nodeId: item for item in parse_junit(self.XML)}
        self.assertEqual(by_name[f"{SAMPLE_FILE}::test_ok"].status, "passed")
        self.assertEqual(by_name[f"{SAMPLE_FILE}::test_ok"].failureLine, 0)
        self.assertEqual(by_name[f"{SAMPLE_FILE}::test_skip"].status, "skipped")

    def test_class_based_node_id_matches_collect_output(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="tests.test_sample.TestFoo" name="test_a" '
            'file="tests/test_sample.py" line="3" time="0.01" />'
            "</testsuite></testsuites>"
        )
        self.assertEqual(parse_junit(xml)[0].nodeId, f"{SAMPLE_FILE}::TestFoo::test_a")

    def test_broken_xml_is_empty_not_error(self) -> None:
        self.assertEqual(parse_junit("<testsuites><oops"), [])


class RunnerTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        if not HAVE_PYTEST:
            self.skipTest("本机未安装 pytest")

    def test_empty_command_reports_not_configured(self) -> None:
        result = collect(command="", collect_args="--collect-only -q", workspace=self.ws, guard=self.guard())
        self.assertFalse(result.configured)
        self.assertEqual(result.cases, [])
        self.assertIn("未配置", result.error)

    def test_denied_command_is_rejected_without_running(self) -> None:
        result = collect(
            command="sudo rm -rf /tmp/x",
            collect_args="",
            workspace=self.ws,
            guard=self.guard(),
        )
        self.assertFalse(result.configured)
        self.assertIn("拒绝", result.error)

    def test_collect_lists_both_cases(self) -> None:
        result = collect(
            command=TEST_COMMAND,
            collect_args="--collect-only -q",
            workspace=self.ws,
            guard=self.guard(),
        )
        self.assertTrue(result.configured, result.error)
        node_ids = {case["nodeId"] for case in result.cases}
        self.assertIn(f"{SAMPLE_FILE}::test_ok", node_ids)
        self.assertIn(f"{SAMPLE_FILE}::test_fail", node_ids)

    def test_run_failure_locates_definition_line(self) -> None:
        """非零退出 + 解析成功：结果要结构化给出失败项与行号，而不是抛异常。"""
        payload = run(
            command=TEST_COMMAND,
            node_ids=[],
            workspace=self.ws,
            guard=self.guard(),
        )
        self.assertTrue(payload["configured"], payload["error"])
        self.assertNotEqual(payload["exitCode"], 0)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["passed"], 1)
        self.assertEqual(payload["failed"], 1)
        failed = [item for item in payload["results"] if item["status"] == "failed"]
        self.assertEqual(failed[0]["failureFile"], SAMPLE_FILE)
        self.assertEqual(failed[0]["failureLine"], FAIL_ASSERT_LINE)

    def test_run_subset_only_runs_selected(self) -> None:
        payload = run(
            command=TEST_COMMAND,
            node_ids=[f"{SAMPLE_FILE}::test_ok"],
            workspace=self.ws,
            guard=self.guard(),
        )
        self.assertEqual(payload["exitCode"], 0)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["passed"], 1)


class SessionTests(_Workspace):
    """会话层：收集缓存、结果缓存、审计事件（K4/K7 的落点）。"""

    def setUp(self) -> None:
        super().setUp()
        if not HAVE_PYTEST:
            self.skipTest("本机未安装 pytest")
        self.session = Session(self.config(), client=FakeClient([]))

    def tearDown(self) -> None:
        self.session.close()
        super().tearDown()

    def _wait_last(self, timeout: float = 60.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = self.session.tests_last()
            if last.get("result") is not None:
                return last
            time.sleep(0.05)
        self.fail("测试在超时前没有产出结果")

    def test_run_records_audit_and_last_result(self) -> None:
        tree = self.session.tests_tree()
        self.assertTrue(tree["configured"], tree["error"])
        self.session.tests_run([])
        last = self._wait_last()
        self.assertEqual(last["result"]["failed"], 1)
        self.assertEqual(last["result"]["failures"][0]["line"], FAIL_ASSERT_LINE)

        audit_path = self.session.agent.config.audit_path
        entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [entry for entry in entries if entry.get("event") == "test.run"]
        self.assertEqual(len(events), 1, "test.run 审计应正好一条")
        self.assertEqual(events[0]["scope"], "all")
        self.assertEqual(events[0]["failed"], 1)
        self.assertEqual(events[0]["file"], SAMPLE_FILE)
        # 失败正文不上报（ADR-006）：审计里不该出现断言文本
        self.assertNotIn("assert", json.dumps(events[0], ensure_ascii=False))

    def test_unknown_node_id_rejected(self) -> None:
        self.session.tests_tree()
        with self.assertRaises(ValueError):
            self.session.tests_run(["tests/not_collected.py::test_x"])


class EndpointTests(unittest.TestCase):
    """未配置 test_command 时端点必须「如实回未配置」而不是 500（K8-④）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.host = HostServer(
            Config.load(self.ws), port=0, client=FakeClient([])
        )
        self.host.start_background()
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        self._tmp.cleanup()

    def _request(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data,
            headers={"X-Vibe-Token": self.token, "Content-Type": "application/json"},
        )
        return self._opener.open(request, timeout=10)

    def test_tests_tree_reports_not_configured(self) -> None:
        with self._request("/api/tests") as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["cases"], [])
        self.assertIn("未配置", payload["error"])

    def test_tests_last_is_null_before_any_run(self) -> None:
        with self._request("/api/tests/last") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertFalse(payload["configured"])
        self.assertIsNone(payload["result"])

    def test_tests_run_not_configured_is_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/api/tests/run", {"nodeIds": []})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()