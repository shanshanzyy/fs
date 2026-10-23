"""客户端自动更新（M9）。

把「发现新版本 → 下载 → 校验 → 静默重装 → 重启」这条链路整体下沉到
零第三方依赖的内核；桌面壳只保留一条 `vibe:update-apply` 窄桥负责
「静默 NSIS 覆盖安装 + relaunch」（ADR-002 薄壳原则仍然成立）。

**三条纪律**（与 ADR-030 同源）：

1. **信任锚唯一**：只有通过 RSA 验签的 manifest 才被采信；sha256 只是
   「传输完整性」第二道锁，挡不住「有人替换了镜像」。任何**源**验签失败
   只作废**该源**，绝不降级信任 —— 可用性与信任是解耦的。
2. **下载与部署分离**：内核负责「检查 / 下载 / 验签 / staging」，
   壳负责「把 staging 路径交给安装器」。壳不读 manifest、不算 sha256、不下载。
3. **不在内核里执行安装**：本模块产出的是一个已验签的安装器文件路径，
   「覆盖安装」这一步永远发生在壳侧（见 apps/desktop/src/update.js）。

模块布局：

  · `version`  —— 版本号比较（与服务端 releases.py 口径镜像）
  · `keys`     —— 内置发布公钥 + RS256 验签（唯一信任锚）
  · `manifest` —— 清单解析 + canonical + 逐源验签
  · `state`    —— `state.json` 反降级 / 防重放
  · `download` —— 流式下载 + 边下边算 sha256 + 原子落盘
  · `source`   —— 双通道取清单（治理面 / 本地镜像），逐源独立验签
  · `service`  —— `UpdateService` 编排（含活动 run 闸门）
  · `cli`      —— `agent-core update check|download|status|rollback`
"""

from __future__ import annotations

from .manifest import ALLOWED_CHANNELS, Manifest, core_of, signing_input
from .version import at_least, compare, is_newer, parse_version

__all__ = [
    "ALLOWED_CHANNELS",
    "Manifest",
    "at_least",
    "compare",
    "core_of",
    "is_newer",
    "parse_version",
    "signing_input",
]
