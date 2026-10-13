/* ===========================================================================
 * preload —— 唯一被允许进入渲染进程的桥
 * ---------------------------------------------------------------------------
 * 它只做两件事：告诉界面「你跑在桌面壳里，还是在浏览器里」，
 * 以及提供一个**只有壳才有的能力**：调起系统目录对话框（W1 的「从磁盘选择…」）。
 *
 * 界面本身不依赖这些也能工作 —— 没有 vibeShell 时，工作区弹窗里那个按钮会被隐藏，
 * 改用「最近打开 + 手输路径」（这正是「同一个界面既能被 Electron 装、也能直接用
 * 浏览器打开」的前提）。
 *
 * 注意暴露面被压到极小：**没有 fs、没有 child_process、没有 ipcRenderer 裸对象**，
 * 只有下面这一个白名单方法，且它**只回传一个路径字符串**（用户取消则回传 null）。
 * 渲染进程虽然加载的是本地页面，但它面对的仍然是一个可能被注入内容的 Agent 输出，
 * 给它 Node 权限等于把整台机器交出去。
 * =========================================================================== */

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vibeShell', Object.freeze({
  kind: 'electron',
  platform: process.platform,
  versions: Object.freeze({
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  }),
  // 唯一一个跨进程能力：调起系统目录对话框，只回传一个路径字符串（取消则是 null）。
  // 不用它、不传它、传坏了，界面都只是少一个按钮，切工作区的两条路仍然可用。
  pickFolder: () => ipcRenderer.invoke('vibe:pick-folder'),
  // 网页自检（O/P1-11）的截图能力：起离屏窗口截一张 PNG，**只回传图片字节**
  // （base64）。浏览器里没有这个方法，界面据此把「截图自检」降级为「需桌面端」。
  capturePage: (url, width, height) =>
    ipcRenderer.invoke('vibe:capture-page', { url, width, height }),
  // 桌面壳配置（模型设置面板的解释器段 + 治理面登录弹窗的地址段）：读/写
  // config.json 的解释器与治理面地址字段 + 重启应用。只接受/只回传
  // {pythonPath, backendBin, serverUrl, gatewayUrl} —— 主进程侧再白名单一次，
  // 任何密钥都不经此桥。浏览器里没有这三座桥，界面据此隐藏对应控制。
  readDesktopConfig: () => ipcRenderer.invoke('vibe:read-config'),
  saveDesktopConfig: (patch) => ipcRenderer.invoke('vibe:save-config', patch),
  relaunch: () => ipcRenderer.invoke('vibe:relaunch'),
}));
