# vibe-agent-desktop —— 桌面客户端（M1）

本地化 Vibe Coding Agent 的桌面壳。**Electron 只做三件事**：起后端、开窗口、管进程。

---

## 它有多薄

```
src/main.js       ~330 行：找 Python → spawn 后端 → 等地址 → loadURL → 管生命周期
src/preload.js    ~30 行：只暴露「跑在桌面壳里」这一个事实，不暴露任何 Node 能力
```

**业务逻辑一行都没有。** 不在 Node 里读文件、不拼 Prompt、不管审批。
这不是偷懒，是架构约束（`docs/ARCHITECTURE.md` §3.1）：壳必须可替换。

判断标准很简单：**任何一行「Agent 应该怎么工作」的逻辑，都不属于这个目录。**

---

## 立刻跑起来

```bash
cd apps/desktop
npm install          # 只装 electron 一个依赖
npm start
```

首次启动会在 `%APPDATA%/vibe-agent-desktop/config.json` 生成配置文件。

也可以直接指定工作区：

```bash
npm start -- E:/path/to/your/project
```

---

## 配置

`%APPDATA%/vibe-agent-desktop/config.json`（macOS/Linux 在各自的 `userData` 目录下）：

```json
{
  "pythonPath": "",
  "workspace": "E:/VibeCoding/examples/demo-project",
  "model": "qwen2.5-coder:7b",
  "baseUrl": "http://127.0.0.1:11434/v1",
  "apiKey": "ollama",
  "serverUrl": "",
  "gatewayUrl": ""
}
```

| 字段 | 说明 |
|---|---|
| `pythonPath` | 留空则依次尝试 `python.exe` / `python` / `py`（非 Windows：`python3` / `python`） |
| `workspace` | 上次使用的工作区，菜单「文件 → 切换工作区」会自动写回 |
| `model` / `baseUrl` / `apiKey` | 透传给 agent-core，留空用 agent-core 的默认值 |
| `serverUrl` | 治理面地址。留空 = 纯本地（M0/M1 行为）；填了即可在应用内登录（见「治理面登录」） |
| `gatewayUrl` | 网关基址，留空由 `serverUrl` 派生；只在网关单独部署时才需要填 |

环境变量 `VIBE_PYTHON` 可覆盖解释器路径，优先级高于配置文件。
`VIBE_SERVER_URL` / `VIBE_GATEWAY_URL` 同理可覆盖治理面地址 ——
运维在机器级环境变量里配一次，所有用户的桌面端就都接上了治理面。

> **模型字段（`model` / `baseUrl` / `apiKey`）的走向变了。** 它们现在有第二个真源：
> 用户级 `~/.vibe/settings.json`（`VIBE_SETTINGS` 可覆盖路径），由界面顶部的
> **「模型设置」面板**（点标题栏的模型 chip）写入。生效优先级为
> **环境变量 > `settings.json` > 桌面 `config.json` > 内核默认**。也就是说，
> 用面板改过的值会盖过 `config.json`；而一旦设了 `VIBE_MODEL` / `VIBE_BASE_URL` /
> `VIBE_API_KEY` 环境变量，对应字段就被锁死，面板会置灰并提示「被环境变量锁定」。
> 详见下方「模型设置面板」。

治理面填了但没登录（或服务端不可达）时，后端会**降级为本地继续跑**，
标题栏出现「治理面未登录 / 治理面降级」徽标，并把原因写进顶部提示条。
没填 `serverUrl` 时整个徽标不出现 —— 纯本地用户不该看到治理相关的噪音。

徽标在「已配置治理面但未登录 / 降级」时**追加一个「去登录」入口**，点击直接打开应用内的
**治理面登录弹窗**（见下方「治理面登录」），不再强制跳浏览器。

---

## 治理面登录

标题栏的**「治理面」按钮**（始终可见）打开登录弹窗 —— 把「填地址 → 密码登录 / 邀请码注册 →
**热接入**」做成桌面端闭环，不必再「先用 CLI 登录、再重启」。

| 区域 | 说明 |
|---|---|
| 治理面地址 | 本次登录随请求下发（后端据此即时热接入，**无需重启**）；「保存地址」把它写进 `config.json`，下次启动作为 `VIBE_SERVER_URL` 注入后端（仅桌面壳可用） |
| 密码登录 | `POST /api/governance/login`（`{email, password, serverUrl?}`） |
| 邀请码注册 | `POST /api/governance/register`（`{enrollmentToken, email, password, displayName?}`）—— 首次加入团队 |
| 退出登录 | `POST /api/governance/logout`；**治理面不可达时仍清本地凭证**，会话回落本地直连 |

登录成功后令牌写进用户级 `~/.vibe/credentials.json`（0o600），当前会话**热接入**：
模型客户端与策略闸门立即换新，**当前对话 / 检查点 / 审计链都保留**（不重建会话）。
界面只显示掩码视图，**从不持有令牌明文**。三条纪律：登录是「用户本人动作」
（不走审批浮层，但记审计）；**任务执行中不可登录 / 退出**（409）；**退得出去**
（治理面不可达也清本地凭证）。

---

## 菜单

| 菜单项 | 快捷键 | 说明 |
|---|---|---|
| 文件 → 切换工作区 | `Ctrl/Cmd+O` | 选择目录后调后端 `POST /api/workspaces/open` **优雅切换**（重建会话 / PathGuard / 检查点 / 审计链）；失败回退到重启后端 |
| 文件 → 打开配置文件 | | 用系统默认编辑器打开 config.json |
| 视图 → 重新加载界面 | `Ctrl/Cmd+R` | 刷新不会丢会话（事件流带 `since` 补齐） |
| 视图 → 开发者工具 | | 排查界面问题 |

> **浏览器里也能切工作区，但没有系统目录对话框。** 直接用浏览器打开后端界面时，标题栏的
> 工作区路径是个按钮，点开可以输入路径、从最近列表里选，或者用弹窗里的**内置目录浏览器**
> 从盘符逐级点进去（后端是只读的 `GET /api/fs/dirs`，只回传目录名）。三条前端走的是同一组
> host 接口（`GET /api/workspaces` / `GET /api/fs/dirs` / `POST /api/workspaces/open`），
> 语义完全一致 —— 差别只有一个：弹窗里的「从磁盘选择…」按钮**只在桌面壳里出现**
> （它走 preload 暴露的 `vibeShell.pickFolder()`，见 [`src/preload.js`](src/preload.js)），
> 浏览器里没有这座桥，该按钮直接隐藏，改用「内置目录浏览器 + 路径 + 最近列表」。
> 切换会重启后端上下文，切换前界面会明确提示；执行中切换会被拒（409）。
> 详见 `docs/M2-PLAN.md` §6。

---

## 模型设置面板

点标题栏的**模型 chip**（或命令面板里的「模型设置…」）打开。它把「换一个模型」从
「改配置文件 + 重启」变成「填个地址 + 点保存」，并且**保存即生效、当前对话不丢**。

| 区域 | 字段 | 说明 |
|---|---|---|
| 服务商预设 | Ollama / OpenAI / DeepSeek / 自定义 | 一键填好接入地址与常用模型名，省去手打 |
| 模型接入 | 模型名 / 接入地址 / API Key | 走 host 的 `GET` + `POST /api/settings`；「测试连接」打 `POST /api/model/test`，「拉取模型列表」打 `GET /api/models` |
| 工作区 | 工作区路径 | 留空 = 保持不变；填了就顺手切换（走同一条 `POST /api/workspaces/open` 链路） |
| 补全（FIM） | 启用 FIM / 输出 token 上限 / 光标前窗口 / 光标后窗口 | 走同一条 `GET` + `POST /api/settings`；前 / 后窗口为行对齐截断的字符数（`0` = 不截断），数值留空 = 不修改；保存后后端会清空补全缓存 |
| 解释器 | Python 解释器路径 / 后端二进制路径 | **仅桌面壳**：写进 `config.json` 并重启应用；浏览器里整段隐藏 |
| 联网检索 | 总开关 / 搜索形态（`gateway`/`searxng`）/ 搜索端点 / 允许私网 / host 白名单·黑名单 / 超时 / 上限 | **默认关闭**（ADR-027）：复用同一条 `GET` + `POST /api/settings`（字段 `webEnabled` 等）；被 `VIBE_WEB` 等环境变量锁定时置灰；`gateway` 形态的搜索端点为只读展示（由治理面 `serverUrl` 派生） |

几条关键语义：

- **保真源、按优先级**：保存写的是用户级 `~/.vibe/settings.json`，不改 `config.json`。
  生效优先级 **环境变量 > `settings.json` > `config.json` > 内核默认**。
- **热替换、留对话**：后端只换掉 LLM 客户端的引用（`agent.client`），**不重建会话** ——
  当前对话、检查点、审计链都原样保留。执行中改会被拒（409），请先中断或等任务结束。
- **环境变量锁**：字段被 `VIBE_MODEL` / `VIBE_BASE_URL` / `VIBE_API_KEY`（补全则是
  `VIBE_FIM` / `VIBE_COMPLETION_MAX_TOKENS` / `VIBE_COMPLETION_PREFIX_CHARS` /
  `VIBE_COMPLETION_SUFFIX_CHARS`）设过时，
  面板里对应输入框置灰（`envLocked`），改了也不生效 —— 这是刻意的：运维用环境变量
  钉死的值，不该被界面悄悄改掉。
- **治理面接管时不可改**：`serverUrl` 配了治理网关时，模型接入由治理面决定，
  面板会提示并拒绝保存（400）。
- **密钥不回明文**：`GET /api/settings` 只回末 4 位掩码；API Key 输入框留空即「保持原值」。
- **解释器要重启**：`pythonPath` / `backendBin` 决定壳怎么起子进程，后端自己不知道，
  只能写 `config.json` 后用 `vibe:relaunch` 重启应用（这就是那一段为什么有个单独的重启按钮）。
  这座桥只接受/只回传 `{pythonPath, backendBin, serverUrl, gatewayUrl}` 四个字段，
  任何密钥都不经它 —— 见 [`src/preload.js`](src/preload.js)。（`serverUrl` 供「治理面登录」
  弹窗保存地址，见上方「治理面登录」。）

---

## MCP 管理面板

侧栏的 **MCP** 图标（`Ctrl+9`）打开，用来启停与维护外部 MCP server —— 不必再手改
`<工作区>/.vibe/mcp.json`。

| 区域 | 说明 |
|---|---|
| 启用 MCP | 复用 `GET` + `POST /api/settings` 的 `mcpEnabled`；被 `VIBE_MCP` 环境变量锁定时置灰 |
| server 列表 | 名称、传输（stdio / http）、状态（运行中 / 未启动 / 不可用 + 原因）、工具数 |
| 增 / 改 / 删 | `POST /api/mcp/servers` / `POST /api/mcp/servers/remove`；弹窗与「模型设置」同范式 |
| 测试连接 | `POST /api/mcp/test`，起一次该 server 并列工具，失败就地提示（不报 5xx） |
| 重载 | `POST /api/mcp/reload`，重读配置并重建 `mcp__*` 工具清单 |

几条关键语义：

- **只写配置文件**：命令 / 参数只落 `<工作区>/.vibe/mcp.json`，界面**从不**把它们拼进 argv
  （ADR-015）；`command` / `args` 始终只取自该受信配置文件。
- **未启用不可管理**：MCP 未开启时写入被拒（400）——先勾「启用 MCP」再管理 server。
- **外部工具恒人审**：`mcp__*` 工具恒 `Risk.REVIEW`、每次调用都弹审批浮层，不可配置放行。
- **执行中改不了**：任务运行中增删改会被拒（409）；治理面接管时同样拒绝（400）。

---

## 它是怎么和后端说话的

不通过 Electron 的 IPC，而是走**本机 HTTP + SSE**：

```
Electron 主进程 ──spawn──▶ python -m agent_core.host --port 0 --print-url
                              │
                              ├─ 往 stdout 打一行 VIBE_URL=http://127.0.0.1:PORT/?token=...
                              │
      主进程解析出地址 ────────┘
                              │
      BrowserWindow.loadURL(那个地址)
                              │
      渲染进程 ──HTTP + SSE──▶ 同一个后端
```

为什么绕这一圈，而不是直接用 IPC：

1. **界面就是普通网页** —— 调试时直接用浏览器打开那个地址就行，不必每次启动 Electron；
2. **前端零桥接** —— `fetch` / `EventSource` 原生可用，`preload.js` 几乎是空的；
3. **壳可以随时换掉** —— 换 Tauri、换纯浏览器、把后端放到远程开发机，这三种场景
   `agent-core` 一行都不用改。

代价是多开了一个本机端口。因此后端有一整套安全闸门（见下），
且**每次启动生成随机 token**，写进 URL 里交给壳。

---

## 安全边界

这个壳面对的风险比 CLI 大：窗口里渲染的是 Agent 读到的代码和模型输出，
**这些内容本质上不可信**。

| 措施 | 说明 |
|---|---|
| `contextIsolation: true` | 渲染进程拿不到 Node |
| `sandbox: true` | 渲染进程跑在沙箱里 |
| `nodeIntegration: false` | 没有 `require` |
| `preload.js` 只暴露只读常量 | 不给 `fs` / `child_process` / 裸 `ipcRenderer` |
| `will-navigate` 拦截 | 窗口不能被导航离开 `127.0.0.1` |
| `setWindowOpenHandler` | 外链一律交给系统浏览器，绝不在应用内打开 |
| CSP（由后端下发） | `default-src 'self'` —— 即使被注入也取不到外部资源 |
| 子进程随窗口退出 | `before-quit` / `window-all-closed` 都会 kill 后端，不留孤儿进程 |
| 单实例锁 | 避免双击图标开出两个后端、占两个端口 |

---

## 排查

**双击后长时间没反应**
`pickPython` 会依次探测候选解释器，每个最多等 6 秒。若都失败会弹框提示，
在配置文件里写死 `pythonPath` 即可。

**窗口显示「后端进程意外退出」**
弹框里会带上后端的 stderr 尾部。也可以在终端直接手跑同一个命令看完整报错：

```bash
cd packages/agent-core
python -m agent_core.host --workspace <目录> --port 0 --print-url --no-open
```

**界面空白**
`Ctrl/Cmd+R` 刷新；还不行就开开发者工具看 Console。

**模型不可达**
这是后端报的，不是壳的问题。先用 CLI 的自检确认连通性：

```bash
cd packages/agent-core
python -m agent_core -w <目录> --check
```

---

## M1 的边界（明确没做的事）

- ✅ 已有 Windows 安装包（`--installer`，electron-builder + NSIS），见下面「Windows 安装包」；
  分发包仍是「一个可双击的目录」，见「分发与冒烟」
- ❌ 没有代码签名与自动更新 —— 升级靠重装新版本安装包
- ❌ 没有多窗口 / 多工作区同时打开
- ❌ 没有托盘、没有系统通知

---

## 分发与冒烟（D1）

打包分三步，都从 `packages/agent-core` 下执行：

```bash
# 1) 打内嵌后端（PyInstaller onefile）并组装分发目录
python scripts/build_desktop.py

# 2) 打包冒烟：两条后端路径各跑一轮，含健康检查 / mock 对话 / 截图 / 残留进程断言
python scripts/e2e_package.py

# 3) Windows 安装包（NSIS）：给内部同事双击安装用
python scripts/build_desktop.py --backend --installer
```

产出 `dist/desktop/VibeAgent-<版本>-<平台>-<架构>/`，里面是：

```
VibeAgent.exe                      # Electron 壳（主进程 + 零构建前端）
resources/backend/vibe-agent-core.exe   # 内嵌后端（PyInstaller onefile）
resources/backend/version.json     # 后端清单：core 版本 / 平台 / 架构 / 构建时间
resources/app/...                  # main.js / backend.js / preload.js / smoke.js
```

**后端来源优先级**（实现在 [src/backend.js](src/backend.js)）：
显式指定的 `backendBin` / `VIBE_BACKEND_BIN` → **内嵌二进制** → 回退系统 Python。
第二档是「机器上没装 Python 也能用」的关键；`VIBE_NO_EMBEDDED=1` 可跳过它，
用来验证回退路径（冒烟脚本就是这么跑第二遍的）。

跨目录迁移时**整个目录一起搬**：内嵌后端在 `resources/backend/` 下，
只拷 `VibeAgent.exe` 会退化成「找不到后端」。

打包版本号只有一处真源（`agent_core.__version__`），`build_desktop.py` 从那里取，
组装出的目录名与壳的 `package.json` 版本三处必须一致 —— 这条由
[test_packaging.py](../../packages/agent-core/tests/test_packaging.py) 的静态契约锁住。
后端清单一律由 `write_backend_manifest()` 生成，分发目录与安装包都带，
所以两条链路的 `version.json` 格式永远同源。

---

## Windows 安装包（NSIS）

`python scripts/build_desktop.py --backend --installer` 产出
`dist/desktop/installer/VibeAgent-Setup-<版本>.exe` —— 同事双击即可安装，
装完有开始菜单与桌面快捷方式，控制面板里能卸载。

| 配置项 | 值 | 说明 |
|---|---|---|
| 安装器 | electron-builder + NSIS | 构建期依赖，见 [package.json](package.json) 的 `build` 段 |
| 安装范围 | `perMachine: false` | 装到 `%LOCALAPPDATA%\Programs\VibeAgent`，**不需要管理员** |
| 安装目录 | `allowToChangeInstallationDirectory: true` | 安装时可自选 |
| 内嵌后端 | `extraResources: resources/backend → backend` | 与分发目录同构，仍是 `resources/backend/`，见 `embeddedCandidates`；含 `version.json` |

几条要点：

- **首次构建要联网**：electron-builder 会下载 NSIS 工具链（缓存在
  `%LOCALAPPDATA%\electron-builder\Cache`），之后走缓存。`electronDist` 指向本机
  已装的 `node_modules/electron/dist`，因此**不会重复下载 Electron**。
- **用户数据不在安装目录**：配置在 `%APPDATA%\vibe-agent-desktop\config.json`，
  内核数据在 `~/.vibe/`。装到 `Program Files` 也不会因只读而失败。
- **不做的事**：没有代码签名（同事首次运行会看到 SmartScreen 提示，选「仍要运行」即可）、
  没有自动更新（升级重装新版本安装包）。这两项留给后续按需引入。
- 应用图标目前用 Electron 默认图标（仓库里没有 `.ico` 资源）；要换图标就放一张
  256×256 的 `resources/icon.ico` 并在 `build.win.icon` 里指过去。

以上都是**刻意不做**：M1 的目标是让单人能真正用起来，
而不是先花两周配打包流水线。
