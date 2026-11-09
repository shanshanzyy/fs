"""Generic dependency scheduler. Workers receive isolated, versioned inputs.

Only explicit user actions start execution or change a plan. Model output never
changes scheduler state directly. SQLite serializes durable state; one supervisor
owns each project and bounded worker threads own individual attempts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Callable

from ..agents.store import write_text_atomic


class CollaborationError(ValueError):
    pass


_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "blocked"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise CollaborationError("成果路径必须是相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or any(part in {".git", ".vibe"} for part in Path(name).parts):
        raise CollaborationError("成果路径越界或属于内部目录")
    return path


def validate_plan(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 24:
        raise CollaborationError("计划需包含 1–24 个任务")
    tasks = []
    ids = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CollaborationError("任务必须是对象")
        key = item.get("id", "")
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in ids:
            raise CollaborationError("任务 ID 非法或重复")
        ids.add(key)
        goal = item.get("goal", "")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
            raise CollaborationError("任务目标不能为空，且最多 12000 字")
        task = {"id": key, "goal": goal, "role": str(item.get("role", "执行者"))[:100],
                "dependsOn": item.get("dependsOn", []), "resources": item.get("resources", []),
                "outputs": item.get("outputs", []), "acceptance": item.get("acceptance", []),
                "timeout": item.get("timeout", 300), "state": "pending", "attempt": 0,
                "artifacts": [], "error": "", "summary": ""}
        for name in ("dependsOn", "resources", "outputs", "acceptance"):
            if not isinstance(task[name], list) or len(task[name]) > 32 or any(not isinstance(x, str) or not x or len(x) > 2000 for x in task[name]):
                raise CollaborationError(f"{name} 需为有界字符串数组")
            task[name] = list(dict.fromkeys(task[name]))
        if not task["acceptance"]:
            raise CollaborationError("每个任务必须提供验收条件")
        if isinstance(task["timeout"], bool) or not isinstance(task["timeout"], int) or not 10 <= task["timeout"] <= 1800:
            raise CollaborationError("任务超时需为 10–1800 秒")
        for name in task["outputs"]:
            local_file(Path.cwd(), name)
            if Path(name).parts[0] == "inputs" or name == "result.json":
                raise CollaborationError("交付物不能覆盖输入或执行记录")
        tasks.append(task)
    by_id = {t["id"]: t for t in tasks}
    visiting, done = set(), set()
    def visit(key):
        if key in visiting:
            raise CollaborationError("任务依赖存在循环")
        if key in done:
            return
        visiting.add(key)
        for dep in by_id[key]["dependsOn"]:
            if dep not in by_id:
                raise CollaborationError("任务依赖不存在")
            visit(dep)
        visiting.remove(key)
        done.add(key)
    for key in by_id:
        visit(key)
    return tasks


class CollaborationService:
    def __init__(self, workspace: Path, runner: Callable, *, emit=None, max_workers=2):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".vibe" / "collaborations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.emit = emit or (lambda event: None)
        self.max_workers = max(1, min(4, max_workers))
        self.lock = threading.RLock()
        self.closed = False
        self.active = {}
        self.db = sqlite3.connect(self.root / "projects.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, body TEXT NOT NULL)")
        self.db.commit()
        for project in self.list():
            if project["state"] == "running":
                for task in project["tasks"]:
                    if task["state"] == "running":
                        task["state"] = "interrupted"
                project["state"] = "interrupted"
                self._save(project)

    def _save(self, project):
        with self.lock:
            existing = self.db.execute("SELECT body FROM projects WHERE id=?", (project["id"],)).fetchone()
            if existing:
                saved = json.loads(existing[0])
                if len(saved.get("messages", [])) > len(project.get("messages", [])):
                    project["messages"] = saved["messages"]
                    project["requiresRevision"] = saved.get("requiresRevision", False)
            project["updatedAt"] = time.time()
            self.db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?, ?)",
                            (project["id"], project["requestId"], json.dumps(project, ensure_ascii=False)))
            self.db.commit()

    def get(self, key):
        if not isinstance(key, str) or not _ID.fullmatch(key):
            raise CollaborationError("项目 ID 非法")
        with self.lock:
            row = self.db.execute("SELECT body FROM projects WHERE id=?", (key,)).fetchone()
        if not row:
            raise CollaborationError("协作项目不存在")
        return json.loads(row[0])

    def list(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM projects ORDER BY rowid DESC LIMIT 100")]

    def create(self, goal, tasks, request_id):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
            raise CollaborationError("项目目标不能为空，且最多 12000 字")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise CollaborationError("requestId 必填，最多 100 字")
        tasks = validate_plan(tasks)
        with self.lock:
            existing = self.db.execute("SELECT body FROM projects WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return json.loads(existing[0])
            project = {"id": uuid.uuid4().hex[:12], "requestId": request_id, "goal": goal,
                       "revision": 1, "state": "draft", "tasks": tasks, "messages": [], "createdAt": time.time()}
            self._save(project)
            return project

    def revise(self, key, tasks, expected_revision):
        tasks = validate_plan(tasks)
        with self.lock:
            project = self.get(key)
            if key in self.active or project["state"] == "running":
                raise CollaborationError("请先停止执行，再修订计划")
            if expected_revision != project["revision"]:
                raise CollaborationError("计划版本已变化，请刷新")
            project["history"] = project.get("history", []) + [{"revision": project["revision"], "tasks": project["tasks"]}]
            project.update(tasks=tasks, revision=project["revision"] + 1, state="draft", requiresRevision=False)
            self._save(project)
            return project

    def message(self, key, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise CollaborationError("消息需为 1–4000 字")
        with self.lock:
            project = self.get(key)
            if key in self.active:
                self.active[key][0].set()
            project["requiresRevision"] = True
            project["messages"].append({"text": text, "at": time.time()})
            project["messages"] = project["messages"][-100:]
            self._save(project)
        return project

    def start(self, key):
        with self.lock:
            if self.closed or self.active:
                raise CollaborationError("已有协作项目在执行，或会话已关闭")
            project = self.get(key)
            if project.get("requiresRevision"):
                raise CollaborationError("补充要求后请先保存计划修订，避免复用过期成果")
            if project["state"] not in {"draft", "interrupted", "cancelled", "failed", "blocked"}:
                raise CollaborationError("当前项目状态不允许执行")
            for task in project["tasks"]:
                if task["state"] != "succeeded":
                    task.update(state="pending", error="")
                else:
                    for artifact in task["artifacts"]:
                        path = self.root / artifact["path"]
                        if not path.is_file() or digest(path) != artifact["sha256"]:
                            raise CollaborationError("已完成成果发生变化，请修订计划后重新执行")
            stop = threading.Event()
            project["state"] = "running"
            self._save(project)
            thread = threading.Thread(target=self._run, args=(project, stop), daemon=True, name=f"collaboration-{key}")
            self.active[key] = (stop, thread)
            thread.start()
        return self.get(key)

    def artifact(self, key, task_id, name):
        project = self.get(key)
        task = next((item for item in project["tasks"] if item["id"] == task_id), None)
        if not task or task["state"] != "succeeded":
            raise CollaborationError("成果尚未通过验收")
        artifact = next((item for item in task["artifacts"] if item["name"] == name), None)
        if not artifact:
            raise CollaborationError("成果不存在")
        path = self.root / artifact["path"]
        if not path.resolve().is_relative_to(self.root.resolve()) or digest(path) != artifact["sha256"]:
            raise CollaborationError("成果已变化，请重新验收")
        return path, artifact

    def deliver(self, key, approve, check_write):
        with self.lock:
            project = self.get(key)
            if project["state"] != "succeeded" or project.get("requiresRevision"):
                raise CollaborationError("整场任务通过验收后才能交付")
            target = self.workspace / "collaboration-deliveries" / f"{key}-v{project['revision']}"
            check_write(target.relative_to(self.workspace).as_posix())
            check_write((target / "manifest.json").relative_to(self.workspace).as_posix())
            if target.exists():
                raise CollaborationError("该版本已交付，禁止覆盖")
            manifest = []
            for task in project["tasks"]:
                for item in task["artifacts"]:
                    path, artifact = self.artifact(key, task["id"], item["name"])
                    check_write((target / task["id"] / item["name"]).relative_to(self.workspace).as_posix())
                    manifest.append((task["id"], path, artifact))
        if not approve(project, manifest, target):
            raise CollaborationError("用户未批准交付")
        with self.lock:
            current = self.get(key)
            if current["revision"] != project["revision"] or current.get("requiresRevision") or current["state"] != "succeeded":
                raise CollaborationError("审批期间计划已变化")
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.parent / (".staging-" + uuid.uuid4().hex)
            stage.mkdir()
            try:
                for task_id, source, artifact in manifest:
                    if digest(source) != artifact["sha256"]:
                        raise CollaborationError("审批期间成果已变化")
                    output = stage / task_id / artifact["name"]
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(source.read_bytes())
                    if digest(output) != artifact["sha256"]:
                        raise CollaborationError("交付复制校验失败")
                write_text_atomic(stage / "manifest.json", json.dumps(project, ensure_ascii=False))
                if target.exists():
                    raise CollaborationError("交付目录已存在")
                os.rename(stage, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            current["delivery"] = str(target.relative_to(self.workspace))
            self._save(current)
        return {"ok": True, "path": current["delivery"]}

    def stop(self, key):
        with self.lock:
            if key not in self.active:
                raise CollaborationError("项目未在执行")
            self.active[key][0].set()
        return {"ok": True, "id": key}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            entries = list(self.active.values())
            for stop, _ in entries:
                stop.set()
        def finish_close():
            for _, thread in entries:
                thread.join()
            with self.lock:
                self.db.close()
        if entries:
            threading.Thread(target=finish_close, daemon=True).start()
        else:
            self.db.close()

    def _event(self, project, task=None):
        try:
            self.emit({"type": "collaboration.state", "projectId": project["id"],
                       "state": project["state"], "taskId": task["id"] if task else "",
                       "taskState": task["state"] if task else "", "revision": project["revision"]})
        except Exception:
            pass

    def _run(self, project, stop):
        running, resources = {}, set()
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="collaboration-worker") as pool:
                while True:
                    by_id = {task["id"]: task for task in project["tasks"]}
                    for task in project["tasks"]:
                        if stop.is_set() or len(running) >= self.max_workers:
                            break
                        if task["state"] != "pending":
                            continue
                        deps = [by_id[key] for key in task["dependsOn"]]
                        if any(dep["state"] in {"failed", "blocked", "cancelled"} for dep in deps):
                            task["state"] = "blocked"
                            continue
                        if not all(dep["state"] == "succeeded" for dep in deps) or resources.intersection(task["resources"]):
                            continue
                        task["state"] = "running"
                        task["attempt"] += 1
                        resources.update(task["resources"])
                        self._save(project)
                        self._event(project, task)
                        future = pool.submit(self._attempt, copy.deepcopy(project), copy.deepcopy(task), stop)
                        running[future] = task
                    if not running:
                        break
                    completed, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        task = running.pop(future)
                        resources.difference_update(task["resources"])
                        try:
                            result = future.result()
                            task.update(result)
                        except Exception as exc:
                            task.update(state="cancelled" if stop.is_set() else "failed", error=str(exc)[:2000])
                        self._save(project)
                        self._event(project, task)
                for task in project["tasks"]:
                    if task["state"] == "pending":
                        task["state"] = "cancelled" if stop.is_set() else "blocked"
                project["state"] = "cancelled" if stop.is_set() else ("succeeded" if all(t["state"] == "succeeded" for t in project["tasks"]) else "failed")
                self._save(project)
                self._event(project)
        except Exception as exc:
            project.update(state="interrupted", error=str(exc)[:2000])
            self._save(project)
        finally:
            with self.lock:
                self.active.pop(project["id"], None)

    def _attempt(self, project, task, stop):
        directory = self.root / project["id"] / f"v{project['revision']}" / task["id"] / f"attempt-{task['attempt']}"
        directory.mkdir(parents=True, exist_ok=False)
        inputs = directory / "inputs"
        inputs.mkdir()
        dependencies = []
        for dep in project["tasks"]:
            if dep["id"] not in task["dependsOn"]:
                continue
            for artifact in dep["artifacts"]:
                source = self.root / artifact["path"]
                if digest(source) != artifact["sha256"]:
                    raise CollaborationError("依赖成果版本已变化")
                target = inputs / dep["id"] / artifact["name"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                dependencies.append({"taskId": dep["id"], "path": str(target.relative_to(directory))})
        deadline = time.monotonic() + task["timeout"]
        cancelled = lambda: stop.is_set() or time.monotonic() >= deadline
        result = self.runner(project, task, directory, dependencies, cancelled)
        if cancelled():
            raise CollaborationError("任务已停止或超时")
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise CollaborationError(str(result.get("reason", "验收未通过")) if isinstance(result, dict) else "执行结果不合法")
        artifacts = []
        for name in task["outputs"]:
            path = local_file(directory, name)
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise CollaborationError(f"交付物缺失或超过 20 MB：{name}")
            artifacts.append({"name": name, "path": str(path.relative_to(self.root)), "sha256": digest(path), "bytes": path.stat().st_size})
        summary = str(result.get("summary", ""))[:12000]
        write_text_atomic(directory / "result.json", json.dumps({"summary": summary, "artifacts": artifacts}, ensure_ascii=False))
        return {"state": "succeeded", "artifacts": artifacts, "summary": summary, "error": "", "validation": result.get("validation", "")}
