"""远程工作区的「本机能力已不可用」清单（M13 / ADR-042 决策 9）。

## 为什么单独放一个模块

这份清单要在**三处**被读到：HTTP 端点（`server.py` 的降级守卫）、系统提示词
（`loop/prompts.py`）、会话快照（`host/session.py` 的 `describe`）。
三处各写一份必然漂移，最后变成「界面说支持、内核不支持」这种最难排查的错位 ——
所以它是一个**只依赖 Config 的纯函数**，谁都能取，且取值必然一致。

## 为什么键是能力名而值是人话原因

只 saying「git 不可用」不够 —— 用户真正需要知道的是「那我要查远端仓库状态该怎么做」。
所以每条降级都写成**可执行的一句话**，而不是一个状态码。
"""

from __future__ import annotations

from typing import Any

__all__ = ["DEGRADED_KEYS", "is_remote_workspace", "remote_degraded"]

# 清单的键是稳定的契约：端点前缀映射表、提示词、界面都按这几个取值。
DEGRADED_KEYS: tuple[str, ...] = ("git", "lsp", "index_watch", "repo_map", "local_write")


def is_remote_workspace(config: Any) -> bool:
    """是不是远程工作区。**本机模式恒 False**（未启用 / 未配置都算）。"""
    return bool(
        bool(getattr(config, "remote_enabled", False))
        and str(getattr(config, "remote_workspace", "") or "")
    )


def remote_degraded(config: Any) -> dict[str, str]:
    """远程工作区模式下不适用的本机能力 → 人话原因。本机模式恒返回空字典。

    空字典这个返回值本身就是一条契约：`host/server.py` 的降级守卫看到空就直接
    放行，于是本机模式的行为与未实现前**逐字节一致**。
    """
    if not is_remote_workspace(config):
        return {}
    # 连接名只用来把原因写得更具体（「远端 devbox」而不是「远端设备」）；
    # 取不到也不影响 —— 少一个名字不妨碍它把事实说清楚。
    target = "该远端设备"
    try:
        from .roots import parse_ns_root

        connection, _sub = parse_ns_root(str(config.remote_workspace or ""))
        if connection:
            target = connection
    except Exception:  # noqa: BLE001 — 坏写法不该让清单本身起不来
        pass
    return {
        "git": (
            f"当前工作区在远端「{target}」，本机 Git 面板看不到它。"
            "要查远端仓库状态，请在远端会话里执行 git 命令。"
        ),
        "lsp": (
            f"本机语言服务器够不到远端「{target}」的文件，诊断已关闭"
            "（不是「没报错」，是这一项不可用）。"
        ),
        "index_watch": (
            f"增量索引监听的是本机锚点目录，不含远端「{target}」的文件。"
        ),
        "repo_map": (
            f"代码地图与语义检索只覆盖本机锚点目录，不含远端「{target}」的文件。"
        ),
        "local_write": (
            f"当前工作区在远端「{target}」，文件工具一律作用于远端 —— "
            "本模式下无法在本机写入任何文件。"
        ),
    }
