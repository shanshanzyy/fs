# M10 里程碑：定时任务调度 —— 任务清单与方案（**已批准 · 已交付**）

| 项 | 内容 |
|---|---|
| 版本 | v1.0（A–F 六阶段全部闭合；本文件为落盘正式版） |
| 日期 | 2026-10-16 |
| 前置 | M0–M9 已交付。开工基线：agent-core **930 passed / 1 skipped / 98 subtests**，服务端 test_chain_compat **25 passed / 339 subtests**，`scripts/e2e_governance.py` 8 档全绿，打包冒烟双路径通过 |
| 定位 | 把「后台任务」从**手动一次性提交**推进到**定时 / 周期自动触发**：新增一层常驻调度器，挂在 M9-D 既有任务池之上，补「定时触发 + 落盘 + 重启补跑」三件缺口 |
| 上游文档 | `docs/M9-PLAN.md` §D（后台/并行 Agent）、`docs/ARCHITECTURE.md`（ADR-003 零依赖 / ADR-006 审计只记元数据 / ADR-008 高危审批 / ADR-011 用户本人动作 / ADR-013 索引不落盘 / **ADR-032 后台 Agent 默认关**）、`docs/M8-PLAN.md`（本文档模板） |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 调研结论：与 M9-D 的重叠界定（**先划清，不重造轮子**）

用户口语的「⑤ 定时任务/后台 Agent」中，**「后台 Agent」已由 M9-D 完整交付**，本轮**只补「定时任务」**。

### 0.1 已落地（M9-D，收口标记 ✅）——本轮**一律复用、不改语义**

| 能力 | 位置 |
|---|---|
| 任务池（stdlib `ThreadPoolExecutor`，只读工具面，共用父审计链） | `packages/agent-core/agent_core/async_agent/pool.py`；`AgentTaskPool.submit / status / result / cancel / list_tasks` |
| 开关 `VIBE_ASYNC_AGENT` / `_MAX` / `_TIMEOUT`（默认关） | `agent_core/config.py`（`async_agent_enabled` 等三字段） |
| 会话挂载（启用才建池）+ 状态广播 | `agent_core/host/session.py`（`Session.tasks`、`_on_task_state`） |
| 四端点 | `agent_core/host/server.py`（`POST /api/tasks`、`GET /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`） |
| 事件 `task.state` + 端点登记 | `agent_core/host/protocol.py` |
| 「后台任务」UI 面板（提交 / 刷新 / 取消 / 列表） | `agent_core/host/ui/app.js` + `index.html` |
| 审计事件 `task.spawn`（三处同步） | `governance/vocabulary.py` / `apps/server/vibe_server/redaction.py` / `apps/server/tests/test_chain_compat.py` |
| 专项测试 11 例 | `packages/agent-core/tests/test_async_agent.py` |
| ADR | `docs/ARCHITECTURE.md` **ADR-032** |

### 0.2 待补（本轮真正的缺口）

| # | 缺口 | 证据 |
|---|---|---|
| 1 | **无任何定时 / 周期触发** —— 现只能「人点一下提交一次」，无 cron / interval 语义 | 全仓 grep `cron\|scheduler\|Periodic\|threading.Timer` **零命中**（`cron` 仅出现在 `sandbox/commands.py` 的**禁用命令**黑名单 `crontab`） |
| 2 | **纯进程内、不落盘** —— 关掉即丢，无重启恢复、无补跑 | `async_agent/pool.py` 模块头明写「结果不落盘、不跨重启 —— 本轮只做进程内异步」 |
| 3 | **无触发动作口径** —— 定时能不能跑 shell / 能不能写盘，尚无定义 | `pool.py` 的 `_run_agent` 已钉死只读面（`allow_write=False` / `allow_shell=False`），本轮沿用 |
| 4 | 无「定时任务」界面入口 | UI 只有「后台任务」页签 |

### 0.3 既有可复用范式（**不新造轮子**）

- **常驻线程 + 协作式停止**：`index/watch.py` 的 `IndexWatcher`（`start/stop` + `threading.Event`）、`async_agent/pool.py` 的取消标志 —— 调度线程同款范式。
- **stdlib sqlite3 落盘**：`agent_core/memory/store.py`（`sqlite3.connect(..., check_same_thread=False)` + 一把锁）—— 调度库同款范式。
- **宿主写 `.vibe/`**：`agent_core/checkpoint.py`（`CheckpointManager` 用绝对路径**直写** `.vibe/checkpoints/`，**不走**模型写盘通道）—— 调度库落 `.vibe/scheduler.db` 同款范式。
- **门控默认关 + 逐字节一致**：M6–M9 六条 `VIBE_*` 开关的既有纪律。

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 基础与契约（config 六字段 + cron 解析器 + 任务模型 + 审计词汇三同步） | 中 | ✅ 已完成 |
| **B** | 持久化存储（stdlib sqlite3 任务库 + 路径解析 + 坏行 fail-closed） | 中 | ✅ 已完成 |
| **C** | 调度器（常驻线程 + 到点投池 + 重启补跑 + 上限防风暴 + 崩溃安全） | 高 | ✅ 已完成 |
| **D** | 宿主接线（session 挂载 + 五端点 + `schedule.state` 事件 + 「定时任务」UI 页签） | 中 | ✅ 已完成 |
| **E** | 测试与反向验证（`test_cron.py` / `test_scheduler.py` / `test_scheduler_store.py` / `test_scheduler_api.py` + 反向验证） | 中 | ✅ 已完成 |
| **F** | 文档与收口（`docs/M10-PLAN.md` 落盘 / ADR-035 / 根 README / agent-core README / `.env.example`） | 低 | ✅ 已完成 |

### 1.1 本轮已定档的边界（用户已确认 + 红线推导）

1. **只投后台 Agent 任务**（用户已选）：定时触发的动作**只有一种** —— 把 job 的 `prompt` 投进 M9-D 的 `AgentTaskPool`。**不**新增 shell 通道、**不**新增可写盘通道；只读面与审批漏斗一字不动（ADR-032 沿用）。
2. **调度器 = host 进程内常驻线程**（用户已选）：stdlib `threading.Thread(daemon=True)` + 轮询，**与 Session 同生命周期**；cron 解析自写、**零第三方依赖**（ADR-003）。
3. **落盘 + 重启补跑**（用户已选）：stdlib sqlite3 落 `<workspace>/.vibe/scheduler.db`；进程重启后**恢复定义**，对逾期 job **各补跑一次**（catch-up once，不连补多次，避免重启瞬间风暴）。
4. **用户本人创建**（ADR-011）：定时任务由**用户经界面 / API 创建**；**不向 Agent 暴露任何调度工具**（`build_default_registry` 与 `build_readonly_registry` **都不新增**调度相关工具）——「Agent 自己给自己排期」这条越权 / 递归路径从结构上堵死。
5. **依赖 M9-D 池**：`VIBE_SCHEDULER=1` 但 `VIBE_ASYNC_AGENT=0` → **调度器不启动**，并在界面 notices 明说「定时任务需要先开启 `VIBE_ASYNC_AGENT`」（fail-closed，不静默降级）。
6. **默认关、逐字节一致**：`VIBE_SCHEDULER` 未设时，**无端点、无线程、无页签、无库文件**，工具清单 / 提示词 / 事件流与改造前**逐字节一致**。
7. **审计只记元数据**（ADR-006）：新事件 `schedule.job` 只记 `job_id / action / state / chars / ms`，**job 的 `prompt` 正文一个字符都不出机**。

### 明确不做（本轮）

- ❌ **定时执行 shell 命令**（用户已排除；`run_command` 通道不外扩到调度面）
- ❌ **定时发起可写盘的完整 Agent 轮次**（触碰 ADR-008 红线，用户已排除）
- ❌ **外部计划任务集成**（Windows Task Scheduler / systemd；用户已排除，落盘面过大）
- ❌ 引入 `croniter`、`APScheduler`、`schedule` 等任何第三方调度库（ADR-003）
- ❌ 任务**结论的跨重启持久化**（沿用 M9-D「结论只留进程内」；本轮只持久化**定义 + 执行指纹元数据**，`last_task_id` 指向的 task 结果在重启后不可取）
- ❌ 任务完成后的**桌面系统通知 / 邮件推送**（本轮不做通知增强，属后续）
- ❌ 分布式调度 / 多实例抢占（调度器**随 Session、单实例本机**；`VIBE_SERVER` 治理面不参与）
- ❌ 时区与夏令时（按**本机本地时间**解析；不做 tz 感知、不做 DST 跳变补偿）
- ❌ 秒级精度与「错过补跑 N 次」（轮询粒度 = `VIBE_SCHEDULER_TICK` 秒，默认 30）
- ❌ 定时任务的**跨工作区共享**（库落各自 workspace 的 `.vibe/`，切工作区 = 各管各的）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零第三方库）；
> 新审计事件 `schedule.job` **三处同批同步**（`agent_core/governance/vocabulary.py` 的 `_RULES` /
> `apps/server/vibe_server/redaction.py` 的 `_SCHEMA` /
> `apps/server/tests/test_chain_compat.py` 的 `_sample_locals()`），且审计调用点**写事件名字面量**
> （AST 扫描只认 `record("schedule.job", ...)`）；`Config` 是 frozen dataclass，改字段用 `replace()`；
> **不配置时行为与改造前逐字节一致**。

### A 基础与契约（config + cron 解析器 + 任务模型）

| 子任务 | 内容 |
|---|---|
| A1 | `config.py`：新增 `scheduler_enabled: bool = False`（`VIBE_SCHEDULER`，默认**关**）、`scheduler_db: str = ""`（`VIBE_SCHEDULER_DB`，空 → 默认 `<workspace>/.vibe/scheduler.db`）、`scheduler_tick: int = 30`（`VIBE_SCHEDULER_TICK`，轮询粒秒，**下限 5**）、`scheduler_max_jobs: int = 50`（`VIBE_SCHEDULER_MAX`，job 数上限）、`scheduler_max_per_tick: int = 3`（`VIBE_SCHEDULER_MAX_PER_TICK`，单轮最多触发数，防风暴）、`scheduler_catch_up: bool = True`（`VIBE_SCHEDULER_CATCHUP`，重启补跑开关）。新增只读 property `scheduler_db_path -> Path`（`workspace/.vibe/scheduler.db`，或 `scheduler_db` 覆盖值）；`load()` 全部接线 |
| A2 | 新增 `agent_core/scheduler/__init__.py` + `agent_core/scheduler/cron.py`：**纯 stdlib、零依赖**的调度表达式解析。`parse_schedule(text) -> Schedule`，支持三形态 —— ① **5 字段 cron**（`分 时 日 月 周`）：`*` / `a,b,c` / `a-b` / `*/n` / `a-b/n` / `?`（等价 `*`），分 `0-59`、时 `0-23`、日 `1-31`、月 `1-12`、周 `0-7`（**0 与 7 均为周日**）；② **`@every <n><unit>`**（`s`/`m`/`h`/`d`，如 `@every 30m`）→ 周期；③ **`@at <ISO8601>`**（如 `@at 2026-10-17T09:00:00`）→ **一次性**。`Schedule.next_after(now) -> float | None`（`None` = 不再排期，用于一次性任务）。`dom` 与 `dow` **同时被限定**（均非 `*`）时取 **OR**（Vixie cron 语义，文档写明）。**跳进算法**：逐字段跳（月 → 日 → 时 → 分），跳过整段不匹配区间，**不做逐分钟扫描**，带 4 年搜索上限防死循环。**非法表达式 fail-closed**：抛 `ScheduleError`，绝不当「无调度」放行。**本机本地时间、不涉 tz/DST** |
| A3 | 新增 `agent_core/scheduler/model.py`：`Schedule`（抽象：`describe()` / `next_after()`）与三个实现 `CronSchedule` / `IntervalSchedule` / `OnceSchedule`；`JobSpec`（frozen dataclass：`job_id` / `name` / `schedule`（原文串）/ `prompt` / `enabled` / `created_at` / `next_run_at` / `last_run_at` / `last_task_id` / `last_state` / `run_count`）。`describe() -> dict` **只回元数据**（**不含 `prompt` 正文**，与 `AgentTask.describe()` 同纪律） |
| A4 | 审计词汇**三同步**：新增事件 `schedule.job`（`job_id: str` / `action: str`（`created`/`updated`/`deleted`/`toggled`/`triggered`/`skipped`/`failed`）/ `state: str`（`enabled`/`disabled`）/ `chars: int` / `ms: int`，**全元数据、不含 prompt 正文**）。登记 —— ① `governance/vocabulary.py` `_RULES`（**`Rule.fields` 键序 = 上传名 → (本地名, 类型)**）；② `apps/server/vibe_server/redaction.py` `_SCHEMA`；③ `apps/server/tests/test_chain_compat.py` `_sample_locals()` |
| A5 | `.env.example`：登记六项（`VIBE_SCHEDULER` / `VIBE_SCHEDULER_DB` / `VIBE_SCHEDULER_TICK` / `VIBE_SCHEDULER_MAX` / `VIBE_SCHEDULER_MAX_PER_TICK` / `VIBE_SCHEDULER_CATCHUP`），逐条写清默认值与语义 |

**验收**：`VIBE_SCHEDULER` 未设时行为**逐字节一致**；`parse_schedule` 对 `*/5 * * * *`、`0 9 * * 1-5`、
`@every 30m`、`@at 2026-10-17T09:00:00` 解析正确，对 `99 9 * * *` 抛 `ScheduleError`。

**红线核对**：不新增依赖（自写解析器）；不新增写盘面（A 阶段只读）；新事件只记元数据、三处同步到位。

---

### B 持久化存储（stdlib sqlite3 任务库）

| 子任务 | 内容 |
|---|---|
| B1 | 新增 `agent_core/scheduler/store.py`：`JobStore(path)` —— **纯 stdlib `sqlite3`**（`check_same_thread=False` + 一把 `threading.Lock`，与 `memory/store.py` 同范式）。表 `jobs(job_id TEXT PRIMARY KEY, name TEXT, schedule TEXT, prompt TEXT, enabled INT, created_at REAL, next_run_at REAL, last_run_at REAL, last_task_id TEXT, last_state TEXT, run_count INT)`，`CREATE TABLE IF NOT EXISTS`。方法：`load_all() -> list[JobSpec]`、`get(job_id)`、`upsert(job)`（`INSERT ... ON CONFLICT DO UPDATE`）、`delete(job_id) -> bool`、`close()`。**父目录不存在则 `mkdir(parents=True, exist_ok=True)`**（对齐 `checkpoint.py` 的 `_ensure_dir`） |
| B2 | **落盘纪律**：与 `CheckpointManager` 同范式 —— 宿主用**绝对路径直写** `<workspace>/.vibe/scheduler.db`，**不走**模型的写盘通道（PathGuard 对模型禁写 `.vibe/` 不受影响）。库文件属**宿主资产**（同 `.vibe/checkpoints/`） |
| B3 | **坏行 fail-closed**：`load_all()` 逐行解析，**调度表达式非法 / 字段缺失**的行**跳过并计数**（`skipped`），不静默吞、不让单条坏行弄挂整个调度器；计数进 `Scheduler.status()` 供界面提示 |

**验收**：写入 → 重开库 → round-trip 一致；`enabled=0` 的 job 保留定义；手工塞一行非法 `schedule`
文本后 `load_all` 不抛异常、跳过计数 +1；库文件确实落在 `<workspace>/.vibe/`。

**红线核对**：零依赖；只被宿主写；不触碰模型写盘面。

---

### C 调度器（常驻线程 + 到点投池 + 重启补跑）

| 子任务 | 内容 |
|---|---|
| C1 | 新增 `agent_core/scheduler/runner.py`：`Scheduler`。构造 `Scheduler(*, pool, store, audit, tick=30, max_per_tick=3, max_jobs=50, catch_up=True, now=time.time)`。**线程模型**：一把 `_lock` 护内存 job 字典与 store 写；`start()` 装载 → （`catch_up`）补跑 → 起 `threading.Thread(daemon=True, name="vibe-scheduler")`；`stop()` 置 `threading.Event` → `join(timeout)` → 关 store。**与 `IndexWatcher` / `AgentTaskPool` 同款生命周期范式** |
| C2 | **到点触发**：`_loop()` 每 `tick` 秒醒一次 → `_fire_due()`：按 `next_run_at` 排序取 `next_run_at <= now` 且 `enabled` 的 job，**单轮最多 `max_per_tick` 个**（余下的留到下一轮，**不并发风暴**）→ 对每个 `pool.submit(prompt)` 拿 `task_id` → 回写 `last_run_at` / `last_task_id` / `next_run_at`（`Schedule.next_after(now)`；一次性任务 → `enabled=False` 并写库）→ `run_count += 1` → 审计 `schedule.job`（`action="triggered"`，`chars=len(prompt)`，`ms` = 提交耗时）。**emit `schedule.state`**（经回调，供 host 广播） |
| C3 | **重启补跑**：`start()` 时对 `enabled` 且 `next_run_at <= now` 的 job **各补跑一次**（catch-up once），随后 `next_run_at` **推到未来**（`Schedule.next_after(now)`），**不连补 N 次**（避免重启瞬间把错过的整周任务一次全起）。`VIBE_SCHEDULER_CATCHUP=0` 时改为「只把 `next_run_at` 推到未来、不触发」，并审计 `action="skipped"` |
| C4 | **上限与防风暴**：`max_jobs` 到顶后 `add_job` 抛 `ValueError`（host 层转 400）；`max_per_tick` 限单轮触发数；**错过窗口 > 1 个周期**时**只补 1 次**，不追补历史 |
| C5 | **崩溃安全**：`_loop()` 整体 `try/except`，**单个 job 触发失败只审计 `action="failed"` 并继续**，绝不弄挂调度线程；`stop()` 幂等；**daemon 线程随进程退出，不留孤儿** |
| C6 | **不向 Agent 暴露任何调度工具**：`tools/__init__.py` 的 `build_default_registry` 与 `build_readonly_registry` **都不新增**调度相关 spec（对齐 ADR-011「用户本人动作」；堵死「Agent 自己给自己排期」） |
| C7 | **API 面**：`add_job(name, schedule, prompt, enabled=True) -> JobSpec`、`update_job(job_id, **fields) -> JobSpec | None`、`remove_job(job_id) -> bool`、`set_enabled(job_id, enabled) -> JobSpec | None`、`list_jobs() -> list[dict]`（元数据）、`run_now(job_id) -> str | None`（立即投一次，便于界面验证）、`status() -> dict`（`enabled` / `tick` / `jobCount` / `skippedRows` / `threadAlive`）。每次变更审计 `schedule.job` 并 emit `schedule.state` |

**验收**：建一个 `@every 1s` 的 job（测试用短周期）→ 秒级内 `pool` 里出现任务、`next_run_at` 推进、
审计写 `triggered`；`max_per_tick=1` 时同轮只触发 1 个；`stop()` 后线程确实退出（`threadAlive=False`）；
`catch_up` 场景下预置一个逾期 job → `start()` 后**恰好补跑 1 次**。

**红线核对**：只投**只读**后台任务；不新增 Agent 工具；审计只记元数据；零依赖；线程有收尾。

---

### D 宿主接线（session + 端点 + 事件 + UI）

| 子任务 | 内容 |
|---|---|
| D1 | `host/session.py`：`Session.scheduler: Scheduler | None`。启用条件 **`config.scheduler_enabled and self.tasks is not None`**（池未启用 → 不建调度器，并往 `self._notices` 压一条「定时任务需要先开启 `VIBE_ASYNC_AGENT`」）。随 Session 生命周期（切工作区换 Session = 各工作区各管各的 job）。`start()` 在挂载后调用（**含补跑**）；收尾处（与 `watcher` / `tasks` 同段）调 `scheduler.stop()`。新增门面方法 `job_create / job_update / job_delete / job_toggle / job_list / job_run_now`（薄封装 + 参数校验）。新增 `_on_job_state(payload)` → `self._emit("schedule.state", payload)` |
| D2 | `host/server.py`：新增五端点（**`VIBE_SCHEDULER=0` 时 `session.scheduler is None` → 一律 400**，对齐 `/api/tasks` 口径）：<br>`POST /api/schedules` `{name, schedule, prompt, enabled?}` → `{ok, jobId}`（`name` 非空、`schedule` 可解析、`prompt` 非空且 `<= MAX_ASYNC_TASK_CHARS`、job 数未超上限，否则 400 + 原因）<br>`GET  /api/schedules` → `{jobs:[{jobId,name,schedule,enabled,nextRunAt,lastRunAt,lastTaskId,lastState,runCount}]}`（**不含 prompt**）<br>`POST /api/schedules/{id}` `{name?, schedule?, prompt?, enabled?}` → `{ok, job}`；不存在 404<br>`POST /api/schedules/{id}/delete` → `{ok, deleted}`；不存在 → `{ok, deleted:false}`<br>`POST /api/schedules/{id}/run` → `{ok, taskId}`（立即投一次；不存在 404） |
| D3 | `host/protocol.py`：新增事件 `schedule.state`（`jobId` / `action`（`created`/`updated`/`deleted`/`toggled`/`triggered`/`skipped`/`failed`）/ `state`（`enabled`/`disabled`）/ `nextRunAt` / `lastState` / `runCount` / `taskId`；**不含 prompt 正文**）；端点表新增五行；「定时任务边界」三条写进头部（只投只读后台任务 / 用户本人创建、Agent 无工具 / 审计只记元数据） |
| D4 | `host/ui/app.js` + `index.html`：新增「定时任务」页签（`data-panel="schedules"`），`VIBE_SCHEDULER=0` 时**隐藏入口**（对齐 `tasks` 页签的 `taskAct.hidden` 写法，`runtime()` 暴露 `schedulerEnabled`）。面板：job 列表（name / schedule / 下次运行 / 最近状态 / runCount）+ **启用开关** + **立即跑** + **删除** + **新建表单**（name / schedule / prompt）。事件 `schedule.state` 合并进 `S.schedules` 并重绘 |
| D5 | `host/session.py::runtime()`：暴露 `schedulerEnabled` / `schedulerTick`（供 UI 判隐藏）；`session.py` 的装配点把 `audit` / `pool` / `store` 注入 `Scheduler` |

**验收**：`VIBE_SCHEDULER=0` 时五端点全 400、UI 无页签、事件流**逐字节**与改造前一致；开
`VIBE_ASYNC_AGENT=1` + `VIBE_SCHEDULER=1` 后，建 job → 界面可见 → 到点触发 → 「后台任务」面板里
出现对应 task → 状态回传。

**红线核对**：端点闸门 fail-closed；UI 隐藏 + 服务端强制；prompt 正文不进事件、不进审计。

---

### E 测试与反向验证

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `packages/agent-core/tests/test_cron.py`：解析（`*` / `,` / `-` / `/` / `?`）、`next_after` 边界（**跨月 / 跨年 / 周末 / 闰年 2 月 / 月末**）、`dom` 与 `dow` 同限定时 **OR** 语义、`@every` / `@at` 形态、**非法表达式 fail-closed**、跳进算法正确性（`0 0 1 1 *` 不前推到逐分钟扫描） |
| E2 | 新增 `packages/agent-core/tests/test_scheduler.py`：`add/list/update/remove/toggle/run_now`、**到点投池**（注入假 `now` 快速推进）、`next_run_at` 推进、**一次性任务触发后自禁用**、`max_per_tick` 限流、`max_jobs` 上限、**补跑恰好 1 次**、`CATCHUP=0` 只跳过不触发、**单 job 失败不中断线程**、`stop()` 后**无残留线程**、`status()` 字段 |
| E3 | 新增 `packages/agent-core/tests/test_scheduler_store.py`：sqlite round-trip、`enabled` 保留、**坏行跳过计数**、父目录自动创建、**库落在 `<workspace>/.vibe/`** |
| E4 | 新增 `packages/agent-core/tests/test_scheduler_api.py`：五端点 CRUD + **未启用一律 400**、参数校验 400（空 name / 坏 schedule / 超长 prompt / 超上限）、`schedule.state` 事件字段（**不含 prompt**）、审计 `schedule.job` 只记元数据、`/api/schedules/{id}/run` 立即投池 |
| E5 | **反向验证**（随后**还原**）：① 让 `Scheduler.start()` 忽略补跑 → E2 变红；② 让 `parse_schedule` 接受 `99 9 * * *` → E1 变红；③ 让 `JobStore.upsert` 不落盘 → E3 变红；④ 让 `schedule` 事件带上 `prompt` → E4 变红 |
| E6 | 全量回归：agent-core + 服务端 + `scripts/e2e_governance.py`（8 档）+ 打包冒烟双路径；**既有用例不变红、总数只增不减**；agent-core 与服务端**不并行跑**（端口争用） |

**计数同步**：agent-core 测试总数与 `packages/agent-core/README.md` 用例表同步更新。

---

### F 文档与收口

| 子任务 | 内容 |
|---|---|
| F1 | 本文档（批准后）落盘为 `docs/M10-PLAN.md` |
| F2 | `docs/ARCHITECTURE.md`：新增 **ADR-035「定时任务调度 = 常驻线程 + 只投只读后台任务 + 落盘补跑，默认关」**，固化五项决策（stdlib 调度线程 / cron 自写零依赖 / 只投只读后台任务、不新增 Agent 工具 / 落 `<workspace>/.vibe/` + 补跑一次 / 默认关逐字节一致），交叉引用 ADR-003 / ADR-006 / ADR-011 / ADR-032；「M0–M10 演进路线」提一句 |
| F3 | 根 `README.md`：进度表新增 **M10** 行，链到 `docs/M10-PLAN.md`；文档索引区补一行 |
| F4 | `packages/agent-core/README.md`：新增「定时任务调度」小节（端点 / 事件 / 开关 / 落点）；用例表 + 总数更新 |
| F5 | `packages/agent-core/.env.example` 六项（与 A5 同一批） |

---

## 3. 里程碑完成定义（DoD）

- [x] A–F 六阶段全部交付；专项测试 `test_cron.py` / `test_scheduler.py` / `test_scheduler_store.py` / `test_scheduler_api.py`
- [x] 全量测试数**只增不减**（agent-core **992 passed / 2 skipped / 98 subtests**，服务端 **419 passed / 25 skipped / 574 subtests**），既有用例**无变红**
- [x] 反向验证（E5 四条）已做并已还原
- [x] 审计三重同步齐全（`schedule.job` 进 `vocabulary._RULES` / `redaction._SCHEMA` / `test_chain_compat._sample_locals`）
- [x] **不配置时行为逐字节一致**：`VIBE_SCHEDULER` 未设时无端点、无线程、无页签、无库文件、事件流不变
- [x] **落盘可恢复**：重启后定义恢复、逾期 job 各补跑一次
- [x] **红线三核对**：① 不新增依赖、只投只读后台任务；② Agent 无任何调度工具；③ 审计 / 事件不含 prompt 正文

## 4. 风险与边界

- **重启风暴**：`CATCHUP` 只补 1 次 + `max_per_tick` 限流双保险；`CATCHUP=0` 可整体退化为「只跳过」。
- **线程残留**：`stop()` 置 Event + `join(timeout)`；E2 钉死「`stop()` 后 `threadAlive=False`」。
- **sqlite 跨线程**：`check_same_thread=False` + 一把锁（沿用 `memory/store.py` 的既有做法）。
- **cron 语义歧义**：`dom`/`dow` 同限定取 OR（Vixie 惯例）已在 A2 写明，E1 钉死。
- **夏令时 / 时区**：明确不做（A2 注明「本机本地时间」），避免引入 `zoneinfo` 复杂度。
- **`@at` 已过期**：`next_after` 返回 `None` → job 直接 `enabled=False`，不报错。
- **`prompt` 正文泄漏面**：库文件在 `.vibe/`（本机）、事件与审计都不带正文，三处口径一致。

## 5. 验证方式（端到端）

1. 单测：`cd packages/agent-core && python -m pytest tests/test_cron.py tests/test_scheduler.py tests/test_scheduler_store.py tests/test_scheduler_api.py -q`；再跑全量 `python -m pytest -q`，核对总数与跳过数。
2. 服务端跑道：`cd apps/server && python -m pytest tests/test_chain_compat.py -q`（`schedule.job` 三同步生效）。
3. 本地起 host（`VIBE_ASYNC_AGENT=1` + `VIBE_SCHEDULER=1`）：界面新建 `@every 1m` 的任务 → 观察
   「定时任务」面板 `nextRunAt` 推进、「后台任务」面板出现 task → 审计写 `schedule.job`。
4. 重启 host：确认 job 定义仍在；手工把某 job 的 `next_run_at` 改到过去 → 重启后**恰好补跑 1 次**。
5. `VIBE_SCHEDULER=0` 复跑：界面无页签、`GET /api/schedules` 回 400、无库文件生成、事件流与原版一致。
6. 打包冒烟双路径 + `scripts/e2e_governance.py` 8 档复跑。

---

## 6. 交付验证结果（M10 收口实测，2026-10-16）

### 6.1 代码交付物

| 阶段 | 落盘位置 | 要点 |
|---|---|---|
| A | `agent_core/config.py`（六字段 + `scheduler_db_path`）、`agent_core/scheduler/{__init__,cron,model}.py`、`governance/vocabulary.py`、`apps/server/vibe_server/redaction.py`、`apps/server/tests/test_chain_compat.py`、`packages/agent-core/.env.example` | 自写零依赖 cron 解析（5 字段 / `@every` / `@at`）、`Schedule.next_after` 逐字段跳进、非法表达式 `ScheduleError`；审计 `schedule.job` 三处同步 |
| B | `agent_core/scheduler/store.py` | stdlib sqlite3 + `check_same_thread=False` + 锁；坏行跳过计数；库损坏挪走重建；父目录自动创建 |
| C | `agent_core/scheduler/runner.py` | 常驻线程 + 到点投池 + `max_per_tick` 限流 + 补跑一次 + 崩溃安全 + CRUD/`run_now`/`status` |
| D | `agent_core/host/session.py`、`agent_core/host/protocol.py`、`agent_core/host/server.py`、`agent_core/host/ui/{index.html,app.js}` | 会话挂载（依赖池，缺池不起） + 五端点 + `schedule.state` 事件 + 运行时字段 + 「定时任务」UI 页签 |
| E | `tests/{test_cron,test_scheduler,test_scheduler_store,test_scheduler_api}.py` | 四文件 **63 例** |
| F | 本文件、`docs/ARCHITECTURE.md`（ADR-035）、根 `README.md`、`packages/agent-core/README.md` | 文档收口 |

### 6.2 专项测试与全量回归

- 专项：`python -m pytest tests/test_cron.py tests/test_scheduler.py tests/test_scheduler_store.py tests/test_scheduler_api.py -q` → **63 passed**。
- agent-core 全量：**992 passed / 2 skipped / 98 subtests**（开工基线 930/1/98，只增不减、无变红）。
- 服务端全量：**419 passed / 25 skipped / 574 subtests**。
- `scripts/e2e_governance.py`：**8 档全绿**，无残留进程。
- 打包冒烟：**GUI 路径在无显示环境下无法产出报告**（与 M9 同因，Electron 壳不参与本轮改动）；
  已做**内嵌后端二进制等效验证** —— 启动分发包内 `resources/backend/vibe-agent-core.exe`
  （PyInstaller onefile）→ 打印 `VIBE_URL` → `GET /api/health` **200**、版本与核心 `0.1.0` 一致、
  退出按进程树强杀且**无残留后端进程**。注：本轮未改打包与桌面壳，`dist` 产物沿用既有构建。

### 6.3 反向验证（四条，均已临时改坏 → 确认变红 → 复原）

| # | 改动 | 期望结果 | 实测 |
|---|---|---|---|
| ① | cron `日`+`周` 同限定由 OR 改 AND | `test_dom_and_dow_both_restricted_is_or` 转红 | ✅ 转红（跑到了 11-01） |
| ② | 去掉 `tick_once` 的 `due[:max_per_tick]` 限流 | `test_max_per_tick_throttles` 转红 | ✅ 转红（5 != 3） |
| ③ | 重启补跑改为连补 3 次 | `test_catch_up_once_on_start` / `test_restart_catches_up_once_and_reschedules` 转红 | ✅ 转红（3 != 1） |
| ④ | 审计 `schedule.job` 带上 `prompt` 正文 | `test_audit_has_no_prompt` / `test_audit_metadata_only` 转红 | ✅ 转红（命中 SECRET） |

> 四条改动均在确认变红后**完整复原**，复原后四个测试文件复跑 **63 passed**，且全仓 grep 无残留标记。

### 6.4 红线核对

- **不新增依赖**：`dependencies` 保持 `[]`；cron 自写、落盘 `sqlite3`、并发 `threading`（ADR-003）。
- **只投只读后台任务**：触发动作唯一 —— `pool.submit(job.prompt)`；未新增 shell / 写盘通道（ADR-032）。
- **Agent 无调度工具**：`build_default_registry` / `build_readonly_registry` 均未新增调度 spec（ADR-011）。
- **审计 / 事件不含 prompt 正文**：`schedule.job` 只记 `{job_id, action, state, chars, ms}`；列表与
  `schedule.state` 事件均只回元数据（ADR-006）。
- **默认关逐字节一致**：`VIBE_SCHEDULER` 未设时无端点 / 无线程 / 无页签 / 无库文件。
