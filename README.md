# VibeCoding —— 本地化编码智能体平台

在公司内网私有化部署的 AI 编码智能体：用自然语言描述需求，Agent 自己读代码、
定位问题、生成结构化补丁、等你审批、落盘、跑测试。

对标 Claude Code / Cursor / Qoder 的核心能力，但**执行面完全留在本机，代码不出内网**。

---

## 当前进度

| 阶段 | 目标 | 状态 |
|---|---|---|
| **M0** | CLI 打通本地闭环 | ✅ **已完成** |
| **M1** | 桌面客户端（对话 + Diff 审查 + 审批） | ✅ **已完成** |
| **M1.5** | 效果与体验补全（流式输出 / 会话持久化 / 检查点回滚 / VIBE.md / 验证闭环） | ✅ **已完成**（C4 删除前摘要、D1 打包分发冒烟均已补；另补 W1 工作区切换入口） → [docs/M1.5-PLAN.md](docs/M1.5-PLAN.md) |
| **M2** | 服务端治理面（SSO / RBAC / 网关 / 审计 / 策略） | ✅ **已完成（含收尾 E1–E4）**（治理面 383 个用例；治理面已接进 CLI 与桌面端） → [apps/server/README.md](apps/server/README.md) · [docs/M2-PLAN.md](docs/M2-PLAN.md) |
| **M3** | 多 Agent 编排（主从只读子 Agent） | ✅ **已完成**（父 Agent 经 `task` 工具派生**只读**子 Agent 做探索/检索，结论压缩回传；子 Agent 只读、无递归、共用审计链） → [docs/M3-PLAN.md](docs/M3-PLAN.md) |
| M3+ | 检索增强、Repo Wiki、配额的动态调整 | ✅ **已完成**（① 检索增强：**零依赖**子词化 + 符号感知分块 + RRF 融合 + 按文件去冗余，`VIBE_RETRIEVAL_ENHANCED` 门控，默认关时逐字节一致；② Repo Wiki：**纯本机结构**生成 markdown、落盘走 `plan_overwrite`+审批、默认落 `docs/wiki/`；③ 配额**预算→每日额度**自动推导，**显式策略恒优先**） → [docs/M3+-PLAN.md](docs/M3+-PLAN.md) |
| M4 | 规模化：高可用、监控告警、灰度分发 | 🟡 部分落地（监控告警 / 成本看板数据源 / 灰度分发已落代码；**高可用前置条件已落代码** —— 统一签名密钥硬校验 + **自研纯 stdlib PG 驱动** + TLS/来源 IP 收口，多实例三条硬门槛可运行、`replicas>1` 不再是空话；K8s 仍**只出设计、未落盘**） → [docs/M4-OPS-PLAN.md](docs/M4-OPS-PLAN.md) · [docs/M4+-PLAN.md](docs/M4+-PLAN.md) |
| **M5** | IDE 能力补齐（P0 五条：上下文引用 / 语义检索 / Git 视图 / 编辑面 / LSP） | ✅ **已完成（A–E 五条全部闭合）**（`@file`/`@symbol`/`@folder` 引用；可选 embedding + 词法/依赖图回退的语义检索；只读 Git 面板；内联补全 + 行内改写 + 逐 hunk + 保存；LSP 诊断 / 跳转定义 / 重命名） → [docs/M5-PLAN.md](docs/M5-PLAN.md) |
| **M6** | P1/P2 能力补齐（12 条） | ✅ **已完成（F–Q 十二阶段全部闭合）**（命令面板 / 计划视图 / 上下文窗口 / 多文件事务 / 增量索引 / 测试树 / MCP / 互动终端 / 多模态 / 预览自检 / 规则记忆 / 多租户登录页） → [docs/M6-PLAN.md](docs/M6-PLAN.md) |

---

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
│     ├─ tests/                383 个单元与端到端测试
│     └─ README.md             部署、配置项、接口清单、如何换存储驱动
├─ packages/
│  └─ agent-core/              ★ M0 交付物：Agent 核心引擎（Python，零第三方依赖）
│     ├─ agent_core/           源码
│     │  ├─ governance/        ★ M2 新增：治理面客户端（令牌/策略/审计上报/网关）
│     │  └─ host/              ★ M1 新增：桌面端接入层（HTTP + SSE + 会话/审批会合）
│     │     └─ ui/             零构建前端：对话流 / Diff 审查 / 审批浮层
│     ├─ tests/                754 个单元与端到端测试（752 通过 / 2 跳过）
│     ├─ scripts/              脚本化假模型服务 + 会话/桌面/打包/治理面端到端冒烟
│     └─ README.md             详细文档
└─ examples/
   └─ demo-project/            可试跑的示例项目（刻意留了 3 个 bug）
```

---

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

---

## 三条设计红线

这三条是架构里不可协商的部分，每一项都有测试锁住。

1. **代码不出本机。** 文件读写、命令执行、后续的代码索引与向量库全部在客户端。
   服务端只做治理，且**只存元数据与审计**，绝不存代码、Prompt 原文、模型输出原文。
2. **编辑必须走结构化补丁。** 禁止整文件重写 —— 那会丢掉模型没提及的改动、
   引入幻觉代码、让 Diff 不可读、导致无法回滚。
3. **高危操作强制人工审批。** 不接受通过配置静默关闭。原因是 Prompt Injection
   是这类工具的头号风险（读到的代码注释、Issue 内容里都可能藏指令），
   AI 判断不了，人审是最后一道防线。

---

## CI 与发版

**CI 门禁**（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）：push / PR 自动跑
三条独立跑道（互不阻塞），任一失败即整体变红：

| 跑道 | 覆盖 |
|---|---|
| `agent-core (py3.10 / py3.13)` | 内核单元与端到端测试 —— **只装 pytest**，顺带证明「零第三方依赖也能跑」 |
| `server (py3.13)` | 治理面测试 |
| `desktop (node20)` | `npm ci` 校验 lockfile 与 `package.json` 一致 + 主进程 / preload / host UI 全量语法检查 |

**发版前的打包冒烟刻意不进 CI**：Windows 打包链路重、耗时长且吃额度，挂在每次 push 上
性价比太低，因此改在**发版时手动执行**。它会打内嵌后端、组装分发目录，再对**真实分发包**
跑两遍后端来源的端到端验收（内嵌二进制 / 回退系统 Python），含健康检查、mock 对话、截图
与残留进程断言：

```bash
cd packages/agent-core
python scripts/e2e_package.py --build    # 先重新打包再冒烟
# 产出 dist/desktop/VibeAgent-<版本>-<平台>-<架构>/
```

步骤与产物结构详见[桌面客户端文档](apps/desktop/README.md) §分发与冒烟。

---

## 文档

- [整体架构设计](docs/ARCHITECTURE.md) —— 双平面架构、ADR-001~026、M0–M6 演进路线、选型矩阵
- [agent-core 详细文档](packages/agent-core/README.md) —— 代码地图、安全模型、
  三级补丁匹配、审计设计、如何新增工具、M0 的边界
- [桌面客户端文档](apps/desktop/README.md) —— 配置项、安全边界、排查手册、M1 的边界
- [服务端治理面文档](apps/server/README.md) —— 部署、配置项、接口清单、安全模型、如何换存储驱动
- [M2 收尾方案](docs/M2-PLAN.md) —— 五条缺口（G1–G5）的闭合/定档结论与验收口径
- [M4 运维方案](docs/M4-OPS-PLAN.md) —— 高可用、监控告警、灰度分发、K8s 设计与切换触发条件
- [M4+ 里程碑（高可用最小落地）](docs/M4+-PLAN.md) —— 三门槛收口实测、纯 stdlib PG 驱动与迁移脚本、ADR-026
- [M5 里程碑（IDE 能力补齐）](docs/M5-PLAN.md) —— P0 五条（上下文引用 / 语义检索 / Git / 编辑面 / LSP）收口
- [M6 里程碑（P1+P2 能力补齐）](docs/M6-PLAN.md) —— F–Q 十二阶段（命令面板 / 事务 / MCP / 终端 / 多模态 / 记忆 等）收口

---

## 环境要求

- Python ≥ 3.10（开发验证于 3.13）
- **agent-core 无需任何第三方依赖**，内网离线可运行
- 桌面客户端需要 Node ≥ 20 与 Electron（仅一个 npm 依赖）
- 若要接真实模型：Ollama / vLLM / 任意 OpenAI 兼容服务
