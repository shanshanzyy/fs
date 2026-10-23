"""调度器（M10）：常驻线程 + 到点投只读后台任务 + 重启补跑。

生命周期与 `index/watch.py` 的 `IndexWatcher`、`async_agent/pool.py` 的
`AgentTaskPool` 同款：`start()` 起 daemon 线程、`stop()` 协作式停止 + `join`。

四条硬边界：
  1. **只投只读后台任务** —— 触发动作只有一种：`pool.submit(job.prompt)`。
     不新增 shell 通道、不新增可写盘通道（ADR-032 沿用）。
  2. **到点触发有上限** —— 单轮最多 `max_per_tick` 个（防「到点任务同时起一堆」
     的风暴）；job 数上限 `max_jobs`。
  3. **重启补跑只补一次** —— 逾期 job **各补跑一次**（catch-up once），随后把
     `next_run_at` 推到未来；不连补 N 次（避免重启瞬间把错过的整周任务一次全起）。
  4. **审计只记元数据** —— `schedule.job` 只记 `job_id / action / state /
     chars / ms`，**prompt 正文一个字符都不出机**（ADR-006）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable

from .cron import ScheduleError, parse_schedule
from .model import MAX_JOB_NAME_CHARS, MAX_JOB_PROMPT_CHARS, JobSpec

# 轮询粒度下限：防忙轮询。
MIN_TICK_SECONDS = 5


class Scheduler:
    """定时任务调度器。线程安全（一把 RLock 护内存 job 字典与库写）。"""

    def __init__(
        self,
        *,
        pool: Any,
        store: Any,
        audit: Any = None,
        tick: int = 30,
        max_per_tick: int = 3,
        max_jobs: int = 50,
        catch_up: bool = True,
        now: Callable[[], float] = time.time,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """`pool` 需要一个 `submit(prompt) -> task_id` 方法（鸭子类型，便于测试）。

        `store` 需要一个 `JobStore` 形状（`load_all / upsert / delete / close /
        skipped_rows`）。
        """
        self._pool = pool
        self._store = store
        self._audit = audit
        self._tick = max(MIN_TICK_SECONDS, int(tick))
        self._max_per_tick = max(1, int(max_per_tick))
        self._max_jobs = max(1, int(max_jobs))
        self._catch_up = bool(catch_up)
        self._now = now
        self._on_state = on_state
        self._lock = threading.RLock()
        self._jobs: dict[str, JobSpec] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """装载定义 → （按需）补跑 → 起常驻线程。重复调用幂等。"""
        with self._lock:
            if self._thread is not None:
                return
            now = self._now()
            for job in self._store.load_all():
                self._jobs[job.job_id] = job
            self._catch_up_on_start(now)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="vibe-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """停线程 + 关库。幂等（会话收尾会再调一次）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._tick + 1.0)
        self._thread = None
        try:
            self._store.close()
        except Exception:  # noqa: BLE001 - 关库失败不该让会话收尾炸掉
            pass

    def _run(self) -> None:
        while not self._stop.wait(self._tick):
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 - 后台线程绝不能因一次抖动而死
                continue

    # ---------------------------------------------------------------- 轮询
    def tick_once(self) -> int:
        """跑一轮：触发所有到期 job（受 `max_per_tick` 限流）。返回触发数。"""
        now = self._now()
        with self._lock:
            due = [
                j
                for j in self._jobs.values()
                if j.enabled and j.next_run_at > 0 and j.next_run_at <= now
            ]
        due.sort(key=lambda j: j.next_run_at)
        fired = 0
        for job in due[: self._max_per_tick]:
            self._fire(job, now)
            fired += 1
        return fired

    def _catch_up_on_start(self, now: float) -> None:
        """重启补跑：逾期 job 各补跑一次（或按配置只跳过）。"""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in sorted(jobs, key=lambda j: j.next_run_at):
            if not job.enabled:
                continue
            if job.next_run_at <= 0:
                # 从未排期（如表达式是已过的一次性时间）：算一次未来排期，不触发。
                self._persist(self._reconcile(job, now))
                continue
            if job.next_run_at > now:
                continue
            if self._catch_up:
                self._fire(job, now)
            else:
                skipped = replace(job, last_state="skipped")
                self._persist(self._reconcile(skipped, now))
                self._audit_record(skipped, action="skipped", ms=0)
                self._emit(skipped, "skipped", task_id="")

    # ---------------------------------------------------------------- job CRUD
    def add_job(
        self, name: str, schedule: str, prompt: str, *, enabled: bool = True
    ) -> JobSpec:
        """新建一个 job。参数非法一律抛异常（`ScheduleError` / `ValueError`）。"""
        clean_name = _clean_name(name)
        prompt_text = _clean_prompt(prompt)
        sched = parse_schedule(schedule)
        now = self._now()
        with self._lock:
            if len(self._jobs) >= self._max_jobs:
                raise ValueError(f"定时任务数已达上限（{self._max_jobs}）")
            nxt = sched.next_after(now)
            job = JobSpec(
                job_id=uuid.uuid4().hex[:12],
                name=clean_name,
                schedule=sched.describe(),
                prompt=prompt_text,
                enabled=bool(enabled) and nxt is not None,
                created_at=now,
                next_run_at=float(nxt or 0.0),
            )
            self._jobs[job.job_id] = job
        self._store.upsert(job)
        self._audit_record(job, action="created", ms=0)
        self._emit(job, "created", task_id="")
        return job

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        schedule: str | None = None,
        prompt: str | None = None,
        enabled: bool | None = None,
    ) -> JobSpec | None:
        """整体更新一个 job 的可选字段。不存在返回 None。"""
        now = self._now()
        with self._lock:
            current = self._jobs.get(job_id)
        if current is None:
            return None

        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = _clean_name(name)
        if schedule is not None:
            updates["schedule"] = parse_schedule(schedule).describe()
        if prompt is not None:
            updates["prompt"] = _clean_prompt(prompt)
        if enabled is not None:
            updates["enabled"] = bool(enabled)

        updated = self._reconcile(
            replace(current, **updates), now, force=schedule is not None
        )
        self._persist(updated)
        self._audit_record(updated, action="updated", ms=0)
        self._emit(updated, "updated", task_id="")
        return updated

    def set_enabled(self, job_id: str, enabled: bool) -> JobSpec | None:
        now = self._now()
        with self._lock:
            current = self._jobs.get(job_id)
        if current is None:
            return None
        updated = self._reconcile(replace(current, enabled=bool(enabled)), now)
        self._persist(updated)
        self._audit_record(updated, action="toggled", ms=0)
        self._emit(updated, "toggled", task_id="")
        return updated

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            current = self._jobs.pop(job_id, None)
        if current is None:
            return False
        self._store.delete(job_id)
        self._audit_record(current, action="deleted", ms=0)
        self._emit(current, "deleted", task_id="")
        return True

    def run_now(self, job_id: str) -> str | None:
        """立即投一次（不受排期约束，也不改动 `next_run_at`）。"""
        now = self._now()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        t0 = time.time()
        try:
            task_id = self._pool.submit(job.prompt)
        except Exception as exc:  # noqa: BLE001 - 提交失败只记审计，不影响别处
            self._store.upsert(replace(job, last_state="failed"))
            self._audit_record(job, action="failed", ms=int((time.time() - t0) * 1000))
            self._emit(job, "failed", task_id="")
            return None
        updated = replace(
            job,
            last_run_at=now,
            last_task_id=task_id,
            last_state="triggered",
            run_count=job.run_count + 1,
        )
        self._persist(updated)
        self._audit_record(updated, action="triggered", ms=int((time.time() - t0) * 1000))
        self._emit(updated, "triggered", task_id=task_id)
        return task_id

    # ---------------------------------------------------------------- 查询
    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: (j.created_at, j.job_id))
        return [j.describe() for j in jobs]

    def status(self) -> dict[str, Any]:
        thread = self._thread
        return {
            "enabled": True,
            "tick": self._tick,
            "maxPerTick": self._max_per_tick,
            "maxJobs": self._max_jobs,
            "catchUp": self._catch_up,
            "jobCount": len(self._jobs),
            "skippedRows": int(getattr(self._store, "skipped_rows", 0)),
            "threadAlive": bool(thread is not None and thread.is_alive()),
        }

    # ---------------------------------------------------------------- 内部
    def _fire(self, job: JobSpec, now: float) -> str | None:
        """触发一次：投池 → 回写排期与元数据 → 审计 + emit。"""
        t0 = time.time()
        try:
            task_id = self._pool.submit(job.prompt)
        except Exception as exc:  # noqa: BLE001 - 单个 job 失败绝不弄挂调度线程
            failed = self._reconcile(
                replace(job, last_state=f"failed:{type(exc).__name__}"), now
            )
            self._persist(failed)
            self._audit_record(failed, action="failed", ms=int((time.time() - t0) * 1000))
            self._emit(failed, "failed", task_id="")
            return None

        rescheduled = self._reconcile(job, now)
        updated = replace(
            rescheduled,
            last_run_at=now,
            last_task_id=task_id,
            last_state="triggered",
            run_count=job.run_count + 1,
        )
        self._persist(updated)
        self._audit_record(updated, action="triggered", ms=int((time.time() - t0) * 1000))
        self._emit(updated, "triggered", task_id=task_id)
        return task_id

    def _reconcile(self, job: JobSpec, now: float, *, force: bool = False) -> JobSpec:
        """保证不变量：`enabled` 时 `next_run_at` 必须是未来；算不出即自禁用。

        用于「变更后重排」「启用时重排」「一次性已过」三种场合。**补跑路径不用
        它** —— 那条路要的正是「`next_run_at` 在过去」这个状态。

        `force=True` 时无视「已经是未来」这个短路：**表达式本身变了**，必须按新
        表达式重排，否则会沿用旧表达式的下次触发点 —— 要等它真跑过一次才纠正，
        中间那次触发就跑错了时间。
        """
        if not job.enabled:
            return job
        if not force and job.next_run_at > now:
            return job
        nxt = parse_schedule(job.schedule).next_after(now)
        if nxt is None:
            return replace(job, enabled=False, next_run_at=0.0)
        return replace(job, next_run_at=float(nxt))

    def _persist(self, job: JobSpec) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
        self._store.upsert(job)

    def _audit_record(self, job: JobSpec, *, action: str, ms: int) -> None:
        """只记元数据：`prompt` 正文一个字符都不出机（ADR-006）。"""
        if self._audit is None:
            return
        self._audit.record(
            "schedule.job",
            job_id=job.job_id,
            action=action,
            state="enabled" if job.enabled else "disabled",
            chars=len(job.prompt),
            ms=int(ms),
        )

    def _emit(self, job: JobSpec, action: str, *, task_id: str) -> None:
        if self._on_state is None:
            return
        self._on_state(
            {
                "jobId": job.job_id,
                "action": action,
                "state": "enabled" if job.enabled else "disabled",
                "nextRunAt": job.next_run_at,
                "lastState": job.last_state,
                "runCount": job.run_count,
                "taskId": task_id,
            }
        )


# --------------------------------------------------------------------------- 校验


def _clean_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("定时任务名不能为空")
    if len(text) > MAX_JOB_NAME_CHARS:
        raise ValueError(f"定时任务名过长（{len(text)} 字符，上限 {MAX_JOB_NAME_CHARS}）")
    return text


def _clean_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("定时任务描述不能为空")
    if len(text) > MAX_JOB_PROMPT_CHARS:
        raise ValueError(
            f"定时任务描述过长（{len(text)} 字符，上限 {MAX_JOB_PROMPT_CHARS}）"
        )
    return text


__all__ = ["Scheduler", "MIN_TICK_SECONDS", "ScheduleError"]
