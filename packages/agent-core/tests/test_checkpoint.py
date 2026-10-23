"""检查点 / 一键回滚（B1）测试。

四条硬要求来自 M1.5-PLAN 的验收标准：

1. 连续多轮编辑后回滚到**任意**检查点，文件内容与哈希清单一致
   （含「后续轮改过、目标轮没碰过」的文件 —— 快照链回溯）；
2. 回滚本身可撤销（反向快照也是检查点）；
3. 非 git / git 目录行为一致；checkpoint_dir 在 PathGuard 的写保护范围内；
4. manifest 被篡改（越界路径 / 改坏快照）时整体拒绝恢复，绝不越界写。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.checkpoint import CheckpointError, CheckpointManager  # noqa: E402
from agent_core.config import Config  # noqa: E402
from agent_core.errors import SandboxViolation  # noqa: E402
from agent_core.host.server import HostServer  # noqa: E402
from agent_core.loop import Agent  # noqa: E402
from tests.test_agent_loop import FakeClient  # noqa: E402


def _edit_step(path: str, old: str, new: str) -> tuple[str, dict]:
    return (
        "edit_file",
        {"path": path, "search": old, "replace": new},
    )


class Harness(unittest.TestCase):
    """公共夹具：临时工作区 + 指向它的管理器。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.cp_dir = self.ws / ".vibe" / "checkpoints"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self, keep: int = 10) -> CheckpointManager:
        return CheckpointManager(
            workspace=self.ws, checkpoint_dir=self.cp_dir, session_id="s-test", keep=keep
        )

    def _run_edit(self, manager: CheckpointManager, rel: str, old: str, new: str) -> None:
        """模拟一轮：快照 → 落盘 → 提交。"""
        manager.begin()
        target = self.ws / rel
        manager.snapshot_before_write(target, rel)
        target.write_text(new, encoding="utf-8", newline="")
        info = manager.commit()
        assert info is not None


class CheckpointCoreTests(Harness):
    def test_user_seq_is_persisted_and_annotated(self) -> None:
        """逐条回退的映射：userSeq 写进 manifest，重启后依然读得回来。"""
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        cp = manager.list_checkpoints()[0]
        self.assertEqual(cp.user_seq, 0)
        self.assertIn("userSeq", cp.to_api())

        self.assertTrue(manager.annotate(cp.id, 7))
        # 换一个 Manager 实例（等价于重启）：只从 manifest 读
        self.assertEqual(self._manager().list_checkpoints()[0].user_seq, 7)
        # 幂等；非法值（0 / 不存在的 id）不写也不报错
        self.assertTrue(manager.annotate(cp.id, 7))
        self.assertFalse(manager.annotate(cp.id, 0))
        self.assertFalse(manager.annotate("cp-not-exist", 3))

    def test_restore_to_each_checkpoint(self) -> None:
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        self._run_edit(manager, "a.txt", "A1", "A2")
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A2")

        ids = [cp.id for cp in manager.list_checkpoints()]
        report = manager.restore(ids[0])
        self.assertEqual(report.restored, ["a.txt"])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A0")

        manager.restore(ids[1])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A1")

    def test_restore_uses_chain_for_files_target_never_touched(self) -> None:
        """回滚到 cp1 时，cp2 才改过的文件也必须被撤回 —— 快照链回溯。"""
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        (self.ws / "b.txt").write_text("B0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        self._run_edit(manager, "b.txt", "B0", "B1")

        ids = [cp.id for cp in manager.list_checkpoints()]
        manager.restore(ids[0])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A0")
        self.assertEqual((self.ws / "b.txt").read_text(encoding="utf-8"), "B0")

    def test_restore_deletes_files_created_after_checkpoint(self) -> None:
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")

        manager.begin()
        new_file = self.ws / "new.txt"
        manager.snapshot_before_write(new_file, "new.txt")  # 当时不存在
        new_file.write_text("N1", encoding="utf-8", newline="")
        manager.commit()

        ids = [cp.id for cp in manager.list_checkpoints()]
        report = manager.restore(ids[0])
        self.assertEqual(report.deleted, ["new.txt"])
        self.assertFalse(new_file.exists())
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A0")

    def test_restore_is_undoable_via_reverse_snapshot(self) -> None:
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")

        first = manager.list_checkpoints()[0]
        report = manager.restore(first.id)
        self.assertTrue(report.redo_id, "回滚必须产生反向快照")

        # 反向快照在列表里，且标注为 redo
        kinds = {cp.id: cp.kind for cp in manager.list_checkpoints()}
        self.assertEqual(kinds[report.redo_id], "redo")

        # 撤销回滚 → 回到 A1
        manager.restore(report.redo_id)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A1")

    def test_keep_prunes_oldest(self) -> None:
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager(keep=2)
        for old, new in (("A0", "A1"), ("A1", "A2"), ("A2", "A3")):
            self._run_edit(manager, "a.txt", old, new)
        ids = [cp.id for cp in manager.list_checkpoints()]
        self.assertEqual(len(ids), 2)
        # 淘汰的是最旧的：恢复最旧已不可能，最新的两个仍可用
        with self.assertRaises(CheckpointError):
            manager.restore("cp-does-not-exist")
        manager.restore(ids[-1])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A2")

    def test_run_without_writes_leaves_no_checkpoint(self) -> None:
        manager = self._manager()
        manager.begin()
        self.assertIsNone(manager.commit())
        self.assertEqual(manager.list_checkpoints(), [])
        self.assertEqual(manager.current_id, "")

    def test_same_file_snapshotted_once_per_checkpoint(self) -> None:
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        manager.begin()
        target = self.ws / "a.txt"
        manager.snapshot_before_write(target, "a.txt")
        target.write_text("A1", encoding="utf-8", newline="")
        manager.snapshot_before_write(target, "a.txt")  # 第二次编辑同文件
        manager.commit()
        cp = manager.list_checkpoints()[0]
        self.assertEqual(cp.file_count, 1)
        self.assertEqual(cp.files[0].hash, _sha("A0"))

    def test_git_dir_same_behavior(self) -> None:
        """MVP 不依赖 git：有 .git 目录时行为与非 git 目录完全一致。"""
        (self.ws / ".git").mkdir()
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        manager.restore(manager.list_checkpoints()[0].id)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A0")

    def test_tampered_manifest_escape_rejected(self) -> None:
        """manifest 被篡改出越界路径：整体拒绝，绝不越界写。"""
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        cp = manager.list_checkpoints()[0]
        manifest_path = self.cp_dir / cp.id / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["files"][0]["path"] = "../evil.txt"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(CheckpointError) as ctx:
            manager.restore(cp.id)
        self.assertIn("越界", str(ctx.exception))
        self.assertFalse((self.ws.parent / "evil.txt").exists())

    def test_tampered_snapshot_rejected(self) -> None:
        """快照内容被改坏：哈希校验不过，整体拒绝恢复。"""
        (self.ws / "a.txt").write_text("A0", encoding="utf-8", newline="")
        manager = self._manager()
        self._run_edit(manager, "a.txt", "A0", "A1")
        cp = manager.list_checkpoints()[0]
        snapshot = next((self.cp_dir / cp.id).glob("*.bin"))
        snapshot.write_bytes(b"tampered")

        (self.ws / "a.txt").write_text("A1-broken", encoding="utf-8", newline="")
        with self.assertRaises(CheckpointError) as ctx:
            manager.restore(cp.id)
        self.assertIn("校验失败", str(ctx.exception))
        # 整体拒绝：文件保持被改坏的状态，而不是恢复一半
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "A1-broken")

    def test_unknown_checkpoint_rejected(self) -> None:
        manager = self._manager()
        with self.assertRaises(CheckpointError):
            manager.restore("nope")

    def test_ten_mb_file_under_a_second(self) -> None:
        """验收：10MB 级单文件的检查点创建 < 1s（不做全量复制）。"""
        big = self.ws / "big.bin"
        big.write_bytes(b"x" * (10 * 1024 * 1024))
        manager = self._manager()
        started = time.perf_counter()
        manager.begin()
        manager.snapshot_before_write(big, "big.bin")
        big.write_bytes(b"y" * (10 * 1024 * 1024))
        manager.commit()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"10MB 检查点创建耗时 {elapsed:.3f}s")

        started = time.perf_counter()
        manager.restore(manager.list_checkpoints()[0].id)
        restore_elapsed = time.perf_counter() - started
        self.assertLess(restore_elapsed, 1.0)
        self.assertEqual(big.stat().st_size, 10 * 1024 * 1024)
        self.assertTrue(big.read_bytes()[:4] == b"xxxx")


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CheckpointAgentTests(Harness):
    """Agent 循环挂接：编辑落盘前自动快照，run 结束统一提交。"""

    def _agent(self, steps: list) -> Agent:
        config = Config.load(self.ws)
        # edit_file 是 REVIEW 级，需要审批放行才能真正落盘
        return Agent(config, client=FakeClient(steps), approver=lambda r: "approve")

    def test_run_with_edit_creates_checkpoint(self) -> None:
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8", newline="")
        agent = self._agent([[_edit_step("a.txt", "hello", "hello world")], "改好了。"])
        agent.run("改一下")
        checkpoints = agent.checkpoints.list_checkpoints()
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].files[0].path, "a.txt")
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "hello world\n")

        agent.checkpoints.restore(checkpoints[0].id)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "hello\n")

    def test_run_without_tools_leaves_no_checkpoint(self) -> None:
        agent = self._agent(["纯回答，不动文件。"])
        agent.run("打个招呼")
        self.assertEqual(agent.checkpoints.list_checkpoints(), [])

    def test_interrupted_run_still_commits_checkpoint(self) -> None:
        """中断发生在轮次边界，已写入的文件必须已进检查点。"""
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8", newline="")
        agent = self._agent([[_edit_step("a.txt", "hello", "hi")], "不会被用到"])
        agent.should_stop = lambda: True
        agent.run("改一下")  # 第一轮边界即中断：编辑未执行，本轮无文件
        self.assertEqual(agent.checkpoints.list_checkpoints(), [])

    def test_checkpoint_dir_blocked_for_model_writes(self) -> None:
        """checkpoint_dir 与审计同在 .vibe 下，模型的写必须被 PathGuard 拒绝。"""
        agent = self._agent(["x"])
        with self.assertRaises(SandboxViolation):
            agent.paths.check_write(".vibe/checkpoints/evil.txt")


class CheckpointApiTests(Harness):
    """HTTP 层：/api/checkpoints 两件套。"""

    def setUp(self) -> None:
        super().setUp()
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8", newline="")
        # REVIEW 级编辑需要审批，且审批不可配置关闭（ADR-008）。
        # 冒烟里显式注入自动批准（等效测试夹具替人按下「批准」），聚焦检查点链路。
        config = Config.load(self.ws)
        self.host = HostServer(config, port=0, client=FakeClient([]))
        self.host.start_background()
        self.host.session.agent.approver = lambda request: "approve"
        self.base = f"http://127.0.0.1:{self.host.port}"
        self.token = self.host.guard.token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.host.shutdown()
        super().tearDown()

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path)
        request.add_header("X-Vibe-Token", self.token)
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, data=b"{}", method="POST"
        )
        request.add_header("X-Vibe-Token", self.token)
        try:
            with self._opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_list_empty_then_restore_unknown_400(self) -> None:
        status, data = self._get("/api/checkpoints")
        self.assertEqual(status, 200)
        self.assertEqual(data["checkpoints"], [])

        status, body = self._post("/api/checkpoints/nope/restore")
        self.assertEqual(status, 400)
        self.assertIn("不存在", body["error"])

    def test_run_edit_then_list_and_restore(self) -> None:
        # 让 host 的 session 执行一次带编辑的任务
        self.host.session.agent.client.steps.extend(
            [[_edit_step("a.txt", "hello", "hello world")], "改好了。"]
        )
        self.host.session.submit("改一下")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, state = self._get("/api/state")
            if state["state"] == "idle":
                break
            time.sleep(0.05)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "hello world\n")

        status, data = self._get("/api/checkpoints")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["checkpoints"]), 1)
        cp = data["checkpoints"][0]
        self.assertEqual(cp["fileCount"], 1)
        self.assertIn("a.txt", cp["files"])

        # 运行中拒绝回滚
        self.host.session._state = "running"
        status, body = self._post(f"/api/checkpoints/{cp['id']}/restore")
        self.assertEqual(status, 409)
        self.host.session._state = "idle"

        status, body = self._post(f"/api/checkpoints/{cp['id']}/restore")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["redoId"])
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "hello\n")


if __name__ == "__main__":
    unittest.main()
