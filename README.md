# VibeCoding —— 本地化编码智能体平台

在公司内网私有化部署的 AI 编码智能体：用自然语言描述需求，Agent 自己读代码、
定位问题、生成结构化补丁、等你审批、落盘、跑测试。

对标 Claude Code / Cursor / Qoder 的核心能力，但**执行面完全留在本机，代码不出内网**。

***

## 快速开始（想在自己电脑上用的人先看这里）

### 一、你需要准备什么

| 你的场景 | 必须准备 | 不需要 |
|---|---|---|
| 只想**用**（Windows 同事） | Windows 10/11 x64、一个 OpenAI 兼容的模型地址 | Python、Node、编译工具 |
| 从源码跑桌面端 | Python ≥ 3.10、Node ≥ 20 | 任何第三方 Python 包 |
| 只用命令行 | Python ≥ 3.10 | Node、任何第三方 Python 包 |
| 多人统一治理（可选） | 一台内网常驻机器跑治理面 | 外部数据库（默认 SQLite） |

**唯一的硬前提是一个模型服务**：任何实现了 `POST /v1/chat/completions`（需支持流式 SSE）
的服务都可以 —— 本机 Ollama / vLLM / LM Studio / llama.cpp，或公司内网的推理服务。
本项目**不带模型权重**，也不替你申请 API Key。

### 二、方式一：下载即用（推荐给同事）

**在 [Releases](https://github.com/shanshanzyy/fs/releases) 下载对应版本：**

| 产物 | 适用 | 用法 |
|---|---|---|
| `VibeAgent-<版本>-win-x64-portable.zip` | 所有 Windows 10/11 x64 | 解压到任意可写目录 → 双击 `VibeAgent.exe` |
| `VibeAgent-Setup-<版本>.exe`（若该版次提供） | 同上 | 双击安装，默认落 `%LOCALAPPDATA%\Programs\VibeAgent` |

两者功能一致，**后端二进制已内嵌，目标机器不需要装 Python / Node，也不需要管理员权限**。
便携版注意别解压到受系统策略限制的目录（如某些同步盘、只读共享）。

1. 解压 / 安装后启动客户端。
2. 右上「设置」→「模型设置」：填 `接入地址`（例 `http://10.0.1.50:8002/v1`）、
   `模型`（例 `deepseek-v4-flash`）、`API Key`（本地服务可随便填）→ 保存，立即生效。
3. 选择工作区（本地目录，或先经「远程设备」连上的远端目录）→ 开始对话。

> 分发形态就是**拷一个压缩包**：不用装 Python、Node，也不用管理员权限。
> 壳的配置落 `%APPDATA%\VibeAgent\`，工作区资产落 `<工作区>\.vibe\`。
> 未签名的内部构建，首次运行可能遇到 SmartScreen 提示 —— 选「更多信息 → 仍要运行」。


### 三、方式二：从源码跑（开发者）

```bash
# 桌面端：先打内嵌后端，再起 Electron 壳
cd packages/agent-core
python scripts/build_desktop.py --backend
cd ../../apps/desktop
npm install          # 只装 electron 与 electron-builder
npm start

# 纯命令行
cd packages/agent-core
python -m agent_core --workspace ../../examples/demo-project \
  --base-url http://127.0.0.1:11500/v1 --model mock-agent-model \
  -p "修复 average 遇到空列表崩溃的问题"

# 没有真模型时，先用脚本化假模型把链路跑通（零依赖）
python scripts/mock_model_server.py --port 11500
```

### 四、方式三：部署治理面（可选，多人协作时才有意义）

见下文「方式三：服务端治理面」。客户端只需设 `VIBE_SERVER_URL` 并登录一次；
**不设这个变量就是纯本地模式**，全部功能照常可用。

### 五、装好后这样验收

1. 输入「列出这个项目的目录结构」→ 它应读文件后回答（证明模型与工具链路通）。
2. 让它改一个文件 → 会弹**审批浮层**，先看 diff 再批准（证明写盘闸门通）。
3. 「设置 → 模型设置」改一次地址并保存 → 提示保存成功（证明配置落盘通）。

### 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动后白屏 / 一直转圈 | 内嵌后端没起来。在终端手动跑 `vibe-agent-core.exe --workspace <目录> --no-open --print-url` 看报错 |
| 提示找不到 Python | 只有**从源码跑**才需要系统 Python；安装包用的是内嵌二进制 |
| 对话报 401 / 429 | 模型服务鉴权失败 / 限流；核对 Key、并发与配额，或换本地模型 |
| 端口被占用 | 后端只监听 `127.0.0.1` 且端口随机，一般不冲突；自定义端口请换一个 |
| Windows 安装被 SmartScreen 拦 | 内部未签名包，选「更多信息 → 仍要运行」 |
| 治理面登录后模型改不了 | 仅当策略允许上云、模型请求真的走网关时才会锁；禁云/降级时本机配置可改 |

### 七、数据落在哪（备份/排查用）

- `%APPDATA%\VibeAgent\config.json` —— 桌面壳配置（解释器路径、治理面地址、窗口状态）
- `~/.vibe\settings.json` —— 界面「模型设置」保存的模型配置
- `<工作区>\.vibe\` —— 会话库、检查点、审计哈希链、`mcp.json`
- 卸载**不会**删除 `%APPDATA%` 里的数据，需要彻底清理请手动删除该目录

***

## 当前进度

| 阶段       | 目标                                                   | 状态                                                                                                                                                                                                                                                                                                                                                                                           |
| -------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M0**   | CLI 打通本地闭环                                           | ✅ **已完成**                                                                                                                                                                                                                                                                                                                                                                                    |
| **M1**   | 桌面客户端（对话 + Diff 审查 + 审批）                             | ✅ **已完成**                                                                                                                                                                                                                                                                                                                                                                                    |
| **M1.5** | 效果与体验补全（流式输出 / 会话持久化 / 检查点回滚 / VIBE.md / 验证闭环）       | ✅ **已完成**（C4 删除前摘要、D1 打包分发冒烟均已补；另补 W1 工作区切换入口） → [docs/M1.5-PLAN.md](docs/M1.5-PLAN.md)                                                                                                                                                                                                                                                                                                      |
| **M2**   | 服务端治理面（SSO / RBAC / 网关 / 审计 / 策略）                    | ✅ **已完成（含收尾 E1–E4）**（治理面 444 个用例；治理面已接进 CLI 与桌面端；M8 补 Web 控制台） → [apps/server/README.md](apps/server/README.md) · [docs/M2-PLAN.md](docs/M2-PLAN.md)                                                                                                                                                                                                                                         |
| **M3**   | 多 Agent 编排（主从只读子 Agent）                              | ✅ **已完成**（父 Agent 经 `task` 工具派生**只读**子 Agent 做探索/检索，结论压缩回传；子 Agent 只读、无递归、共用审计链） → [docs/M3-PLAN.md](docs/M3-PLAN.md)                                                                                                                                                                                                                                                                        |
| M3+      | 检索增强、Repo Wiki、配额的动态调整                               | ✅ **已完成**（① 检索增强：**零依赖**子词化 + 符号感知分块 + RRF 融合 + 按文件去冗余，`VIBE_RETRIEVAL_ENHANCED` 门控，默认关时逐字节一致；② Repo Wiki：**纯本机结构**生成 markdown、落盘走 `plan_overwrite`+审批、默认落 `docs/wiki/`；③ 配额**预算→每日额度**自动推导，**显式策略恒优先**） → [docs/M3+-PLAN.md](docs/M3+-PLAN.md)                                                                                                                                              |
| M4       | 规模化：高可用、监控告警、灰度分发                                    | 🟡 部分落地（监控告警 / 成本看板数据源 / 灰度分发已落代码；**高可用前置条件已落代码** —— 统一签名密钥硬校验 + **自研纯 stdlib PG 驱动** + TLS/来源 IP 收口，多实例三条硬门槛可运行、`replicas>1` 不再是空话；K8s 仍**只出设计、未落盘**） → [docs/M4-OPS-PLAN.md](docs/M4-OPS-PLAN.md) · [docs/M4+-PLAN.md](docs/M4+-PLAN.md)                                                                                                                                                   |
| **M5**   | IDE 能力补齐（P0 五条：上下文引用 / 语义检索 / Git 视图 / 编辑面 / LSP）    | ✅ **已完成（A–E 五条全部闭合）**（`@file`/`@symbol`/`@folder` 引用；可选 embedding + 词法/依赖图回退的语义检索；只读 Git 面板；内联补全 + 行内改写 + 逐 hunk + 保存；LSP 诊断 / 跳转定义 / 重命名） → [docs/M5-PLAN.md](docs/M5-PLAN.md)                                                                                                                                                                                                              |
| **M6**   | P1/P2 能力补齐（12 条）                                     | ✅ **已完成（F–Q 十二阶段全部闭合）**（命令面板 / 计划视图 / 上下文窗口 / 多文件事务 / 增量索引 / 测试树 / MCP / 互动终端 / 多模态 / 预览自检 / 规则记忆 / 多租户登录页） → [docs/M6-PLAN.md](docs/M6-PLAN.md)                                                                                                                                                                                                                                             |
| **M7**   | 受控联网检索 + Git 写操作面板                                   | ✅ **已完成（Part A + Part B 全部闭合）**（`web_search`/`web_fetch` 恒 REVIEW、默认关闭、SSRF 六步防护、治理面 `allowWeb`/`webDeniedHosts` 与搜索中转 `POST /api/v1/search`；Git 面板七个写操作 = 用户本人动作、discard 前置检查点、审计三重同步） → [docs/M7-PLAN.md](docs/M7-PLAN.md)                                                                                                                                                                 |
| **M8**   | 治理面 Web 控制台                                          | ✅ **已完成（Part A + Part B + Part C 全部闭合）**（零构建静态控制台 `/console`，九面板：概览 / 成员与邀请码 / 团队 / 设备 / 策略 / 配额 / 审计 / 用量 / 密钥台账；三条只读补口 `GET /teams`、`GET /teams/{id}/members`、`GET /enrollments`；前端隐藏 + 服务端强制、密钥永不回值、CSP 锁死） → [docs/M8-PLAN.md](docs/M8-PLAN.md)                                                                                                                                          |
| **M9**   | 复用与自治（Skill / 分层规则 / 测试闭环 / 后台 Agent / 代码审阅 / 模型选择器） | ✅ **已完成（A–I 九阶段全部闭合）**（六条能力补齐：① `run_skill` 可复用工作流 + `/skills` `/skill` 斜杠命令；② 跨仓库全局规则继承 `VIBE_RULES_GLOBAL`；③ `test_loop` 测试驱动自动修复闭环（恒走审批 + 上限防死循环）；④ 用户本人发起的后台/并行 Agent `POST /api/tasks`；⑤ `review_changes` 提交前只读自审；⑥ `ModelRouter` 按任务难度选型（授权子集 + fail-closed 回退）） → [docs/M9-PLAN.md](docs/M9-PLAN.md)                                                                                  |
| **M10**  | 定时任务调度（常驻线程 + 落盘补跑 + 只投只读后台任务）                       | ✅ **已完成（A–F 六阶段全部闭合）**（在 M9-D 任务池之上补「定时 / 周期触发」：自写零依赖 cron 解析（5 字段 cron / `@every 30m` / `@at <ISO>`，`dom`+`dow` 同限定取 OR）；host 进程内常驻调度线程；定义落 `<workspace>/.vibe/scheduler.db`、重启补跑一次；单轮 `max_per_tick` 限流防风暴；只投**只读后台任务**、不新增 shell/写盘通道、Agent 无调度工具；`VIBE_SCHEDULER` 默认关、逐字节一致；审计 `schedule.job` 只记元数据） → [docs/M10-PLAN.md](docs/M10-PLAN.md)                                              |
| **M11**  | 远程设备文件工作面（连接管理 / 文件树与查看 / 代码审查 / 编辑保存）               | ✅ **已完成（A–F 六阶段全部闭合）**（在既有「远程设备接入」链路上补**面向用户的文件工作面**：界面管理连接与权限位、远端文件树与只读查看、**留底 vs 远端**的 unified diff 逐 hunk 审查、编辑保存（写前本机留底 + `expectedDigest` 乐观锁 + 受控覆盖 + 回读校验 + 真实 diff）、`remote_review_changes` 只读自审；**不新增传输协议、不新增门控**，挂在既有 `VIBE_REMOTE` 上；审计新增 `remote.connection` / `remote.file` 只记元数据，**远端正文与 diff 不出机**） → [docs/M11-PLAN.md](docs/M11-PLAN.md) · [docs/REMOTE.md](docs/REMOTE.md) |
| **M12**  | 远端目录「加入工作区」（命名空间透明路由）                            | ✅ **已完成（A–F 六阶段全部闭合）**（远端文件树里右键目录 →「加入工作区」，即把远端目录登记为**命名空间根** `@remote/<连接名>/<子路径>`；Agent 的 `read_file` / `list_dir` / `find_files` / `grep` / `edit_file` / `write_file` 见到该前缀**透明路由**到远端臂 —— **工具名与 schema 一字不加**、提示词逐字节一致；本地臂走 `PathGuard`、远端臂走 `RemotePathGuard` 两道 Jail；远端写仍守红线（结构化补丁或受控覆盖 + 写前本机留底 + 真实 diff + 审批）；`VIBE_REMOTE` 默认关时无端点 / 无右键 / 无新工具） → [docs/M12-PLAN.md](docs/M12-PLAN.md) |
| **M13**  | 远端目录「作为工作区」（本地锚点 + 路由默认根）                          | ✅ **已完成（A–F 六阶段全部闭合）**（「切换工作区」弹层新增「远程目录」页签：选连接 → 浏览远端目录（目录行可下钻、**文件行只列出不可选** —— 工作区只能是目录）→「设为工作区并重启」。该远端目录成为**路由默认根**，模型写裸相对路径（`src/app.py`）也落到那台机器上；而 `.vibe` 资产（审计链 / 会话库 / 检查点 / 远端写的本机留底）仍留在**本机锚点** `~/.vibe/workspaces/remote/<连接>/<指纹>/`（ADR-006 不动）。切换**先连后切**（握手 + 目录存在性校验，失败则旧会话原封不动）；本机 Git / LSP / 索引 watch 在该模式下**如实降级**并给出人话原因；**不新增门控**、沿用 `VIBE_REMOTE`（未启用时 `remote` 载荷 400 fail-closed、页签整键隐藏）） → [docs/M13-PLAN.md](docs/M13-PLAN.md) |

***

## 目录结构

```
VibeCoding/
├─ docs/
│  └─ ARCHITECTURE.md          整体架构设计（ADR / 演进路线 / 选型矩阵 / 风险清单）
├─ apps/
│  ├─ desktop/                 ★ M1 交付物：Electron 薄壳（起后端 + 开窗口 + 管进程）
│  │  ├─ src/main.js           主进程：找 Python、spawn 后端、解析地址、生命周期
│  │  ├─ src/preload.js        唯一进渲染进程的桥（只暴露「跑在壳里」这一个事实）
│  │  └─ README.md             配置项、安全边界、排查手册
│  └─ server/                  ★ M2 交付物：治理面（Python，零第三方依赖）
│     ├─ vibe_server/          源码：auth/ rbac / gateway/ audit / policy / store/
│     ├─ tests/                444 个单元与端到端测试（419 通过 / 25 跳过，599 子测试）
│     └─ README.md             部署、配置项、接口清单、如何换存储驱动
├─ packages/
│  └─ agent-core/              ★ M0 交付物：Agent 核心引擎（Python，零第三方依赖）
│     ├─ agent_core/           源码
│     │  ├─ governance/        ★ M2 新增：治理面客户端（令牌/策略/审计上报/网关）
│     │  └─ host/              ★ M1 新增：桌面端接入层（HTTP + SSE + 会话/审批会合）
│     │     └─ ui/             零构建前端：对话流 / Diff 审查 / 审批浮层
│     ├─ tests/                1435 个单元与端到端测试（1434 通过 / 1 跳过，169 子测试）
│     ├─ scripts/              脚本化假模型服务 + 会话/桌面/打包/治理面端到端冒烟
│     └─ README.md             详细文档
└─ examples/
   └─ demo-project/            可试跑的示例项目（刻意留了 3 个 bug）
```

***

## 立刻试一下

### 方式一：桌面客户端（M1）

```bash
# 终端 1：启动脚本化假模型（若已有 Ollama 可跳过）
cd packages/agent-core
python scripts/mock_model_server.py --port 11500

cd ../../apps/desktop
npm install     # 只装 electron 一个依赖
npm start
```

窗口打开后，在输入框里写「修复 average 遇到空列表崩溃的问题」并回车。
Agent 会读代码、生成补丁，然后**弹出一个带真实 diff 的审批浮层** ——
你可以先「先看看」去翻文件树和源码，再决定批准还是拒绝。

> 想让桌面端连假模型，把 `%APPDATA%/vibe-agent-desktop/config.json` 里的
> `model` 改成 `mock-agent-model`、`baseUrl` 改成 `http://127.0.0.1:11500/v1`。

### 方式二：命令行（M0）

不需要安装 Ollama，也不需要任何第三方 Python 包：

```bash
# 终端 1：启动脚本化假模型
cd packages/agent-core
python scripts/mock_model_server.py --port 11500

# 终端 2：跑一次真实任务
cd packages/agent-core
python -m agent_core \
  --workspace ../../examples/demo-project \
  --base-url http://127.0.0.1:11500/v1 \
  --model mock-agent-model \
  -p "修复 average 遇到空列表崩溃的问题"
```

Agent 会：生成符号地图 → 读取 `src/utils.py` → 提交补丁 → **弹出审批并展示真实 diff**
→ 落盘 → 给出结论。审批时输入 `y`。

跑完后可以用 `/diff` 复核改动、`/audit` 校验审计哈希链。

### 方式三：服务端治理面（M2）

治理面是**独立进程**，同样零第三方依赖、内网离线可跑：

```bash
cd apps/server

# 1) 先建第一个组织所有者（会顺手签发一枚注册令牌，供拉人进来用）
python -m vibe_server bootstrap --email owner@corp.example --password 'Str0ng!Passw0rd'

# 2) 起服务（默认 127.0.0.1:8787；数据落到 ./.vibe-governance）
python -m vibe_server serve

# 3) 出问题时自检：库/密钥托管/上游/策略四项一次过一遍
python -m vibe_server doctor --probe http://127.0.0.1:8787
```

建库与签名密钥都在首次运行（`bootstrap` 或 `serve`）时自动生成，
密钥落在 `<DATA_DIR>/jwt.key`（默认 `./.vibe-governance/`）。
配置全部走环境变量（`VIBE_SRV_*`），接口与部署细节见
[apps/server/README.md](apps/server/README.md)。

> 治理面与客户端**已经接上（M3 完成）**：设了 `VIBE_SERVER_URL` 并用
> `vibe-agent governance login` 登录过，Agent 就会按服务端下发的策略收紧本机配置、
> 模型请求走网关、审计事件上报服务端；服务端不可达或未登录时**降级为本地继续**，
> 并在界面顶部给出提示。**不设这个变量则完全是纯本地 M0/M1 行为**，一行未变。

***

## 三条设计红线

这三条是架构里不可协商的部分，每一项都有测试锁住。

1. **代码不出本机。** 文件读写、命令执行、后续的代码索引与向量库全部在客户端。
   服务端只做治理，且**只存元数据与审计**，绝不存代码、Prompt 原文、模型输出原文。
2. **编辑必须走结构化补丁。** 禁止整文件重写 —— 那会丢掉模型没提及的改动、
   引入幻觉代码、让 Diff 不可读、导致无法回滚。
3. **高危操作强制人工审批。** 不接受通过配置静默关闭。原因是 Prompt Injection
   是这类工具的头号风险（读到的代码注释、Issue 内容里都可能藏指令），
   AI 判断不了，人审是最后一道防线。

***

## CI 与发版

**CI 门禁**（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）：push / PR 自动跑
三条独立跑道（互不阻塞），任一失败即整体变红：

| 跑道                             | 覆盖                                                                        |
| ------------------------------ | ------------------------------------------------------------------------- |
| `agent-core (py3.10 / py3.13)` | 内核单元与端到端测试 —— **只装 pytest**，顺带证明「零第三方依赖也能跑」                               |
| `server (py3.13)`              | 治理面测试                                                                     |
| `desktop (node20)`             | `npm ci` 校验 lockfile 与 `package.json` 一致 + 主进程 / preload / host UI 全量语法检查 |

**发版前的打包冒烟刻意不进 CI**：Windows 打包链路重、耗时长且吃额度，挂在每次 push 上
性价比太低，因此改在**发版时手动执行**。它会打内嵌后端、组装分发目录，再对**真实分发包**
跑两遍后端来源的端到端验收（内嵌二进制 / 回退系统 Python），含健康检查、mock 对话、截图
与残留进程断言：

```bash
cd packages/agent-core
python scripts/e2e_package.py --build    # 先重新打包再冒烟
# 产出 dist/desktop/VibeAgent-<版本>-<平台>-<架构>/

# 出 Windows 安装包（给内部同事双击安装；需在 Windows 上执行，首次会联网拉 NSIS 工具链）
python scripts/build_desktop.py --backend --installer
# 产出 dist/desktop/installer/VibeAgent-Setup-<版本>.exe
```

步骤与产物结构详见[桌面客户端文档](apps/desktop/README.md) §分发与冒烟、§Windows 安装包。

***

## 文档

- [整体架构设计](docs/ARCHITECTURE.md) —— 双平面架构、ADR-001\~042、M0–M13 演进路线、选型矩阵
- [agent-core 详细文档](packages/agent-core/README.md) —— 代码地图、安全模型、
  三级补丁匹配、审计设计、如何新增工具、M0 的边界
- [桌面客户端文档](apps/desktop/README.md) —— 配置项、安全边界、排查手册、M1 的边界
- [服务端治理面文档](apps/server/README.md) —— 部署、配置项、接口清单、安全模型、如何换存储驱动
- [M2 收尾方案](docs/M2-PLAN.md) —— 五条缺口（G1–G5）的闭合/定档结论与验收口径
- [M4 运维方案](docs/M4-OPS-PLAN.md) —— 高可用、监控告警、灰度分发、K8s 设计与切换触发条件
- [M4+ 里程碑（高可用最小落地）](docs/M4+-PLAN.md) —— 三门槛收口实测、纯 stdlib PG 驱动与迁移脚本、ADR-026
- [M5 里程碑（IDE 能力补齐）](docs/M5-PLAN.md) —— P0 五条（上下文引用 / 语义检索 / Git / 编辑面 / LSP）收口
- [M6 里程碑（P1+P2 能力补齐）](docs/M6-PLAN.md) —— F–Q 十二阶段（命令面板 / 事务 / MCP / 终端 / 多模态 / 记忆 等）收口
- [M7 里程碑（受控联网检索 + Git 写操作面板）](docs/M7-PLAN.md) —— `web_search`/`web_fetch` 恒 REVIEW + 治理面 `allowWeb`、Git 面板七个用户本人写操作
- [M8 里程碑（治理面 Web 控制台）](docs/M8-PLAN.md) —— 零构建静态控制台九面板、三条只读补口、CSP 锁死 + 前端隐藏/服务端强制
- [M9 里程碑（复用与自治）](docs/M9-PLAN.md) —— Skill / 分层规则继承 / 测试闭环 / 后台 Agent / 代码审阅 / 模型选择器六项收口，六条 `VIBE_*` 门控默认关
- [M10 里程碑（定时任务调度）](docs/M10-PLAN.md) —— 自写 cron / `@every` / `@at` 解析、host 常驻调度线程、落 `.vibe/scheduler.db` 重启补跑一次、单轮限流，`VIBE_SCHEDULER` 默认关、只投只读后台任务
- [M11 里程碑（远程设备文件工作面）](docs/M11-PLAN.md) —— 在既有远程链路上补连接管理 / 文件树与查看 / 代码审查（留底 vs 远端的 unified diff）/ 编辑保存（留底 + 乐观锁 + 真实 diff），`VIBE_REMOTE` 默认关、不新增门控
- [M12 里程碑（远端目录加入工作区）](docs/M12-PLAN.md) —— 远端目录登记为命名空间根 `@remote/<连接名>/<子路径>`，六个工具见到前缀透明路由到远端臂（工具名与 schema 一字不加），本地 `PathGuard` / 远端 `RemotePathGuard` 两道 Jail，`VIBE_REMOTE` 默认关时无端点/无右键/无新工具
- [M13 里程碑（远端目录作为工作区）](docs/M13-PLAN.md) —— 「切换工作区」弹层新增「远程目录」页签（目录可下钻、文件只列出不可选），选中即成为**路由默认根**、`.vibe` 资产仍落**本机锚点**，先连后切保原子性，本机 Git/LSP/索引如实降级，不新增门控
- [远程设备文件操作](docs/REMOTE.md) —— 连接清单与 CLI、ssh 加固、路径 Jail、9 个 Agent 工具、**界面工作面（面板 / 端点 / 事件 / 保存语义）**、**远端目录作为工作区（M13）**、能力降级与排障

***

## 环境要求

- Python ≥ 3.10（开发验证于 3.13）
- **agent-core 无需任何第三方依赖**，内网离线可运行
- 桌面客户端需要 Node ≥ 20 与 Electron（仅一个 npm 依赖）
- 若要接真实模型：Ollama / vLLM / 任意 OpenAI 兼容服务

