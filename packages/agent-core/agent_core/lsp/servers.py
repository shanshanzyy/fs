"""语言服务器探测表（E1）—— 「有没有、是哪一条命令、为什么没有」。

## 为什么是探测而不是依赖

核心链路零强制依赖（`pyproject.toml` 的 `dependencies` 保持 `[]`）。语言服务器
是装在机器上的外部程序，这里只做**描述与探测**，不引入任何 Python 包：
`shutil.which` 命中就用，没命中就返回原因，调用方据此降级到「正则符号 + grep」。

## 安全约定

- **argv 里绝不出现用户输入。** 只可能来自本表里的常量 + `shutil.which` 的
  绝对路径。用户的 `path` 只用来**选语言**（看后缀），不参与拼命令。
- 语言服务器是**本地进程**：不连远端、不做远端服务器模式（ADR-014）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# LSP 的 languageId（`textDocument/didOpen` 要用）。与下面的语言名分开：
# 语言名是「本模块内部按后缀归并出来的桶」，languageId 是协议要求的字面量。
LANGUAGE_IDS: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
}

# 后缀 → 语言桶。同一门语言的不同后缀归到同一桶，实例才能复用
# （`.js` 与 `.ts` 都归 typescript 桶会让 ts 服务器去解析 js，不合适 —— 所以分开）。
_SUFFIX_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
}

# 语言桶 → 候选服务器（按优先级）。第一个探测到的即被采用。
_SERVERS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "python": (
        ("pyright-langserver", ("--stdio",)),
        ("pylsp", ()),
    ),
    "javascript": (("typescript-language-server", ("--stdio",)),),
    "typescript": (("typescript-language-server", ("--stdio",)),),
    "go": (("gopls", ()),),
    "rust": (("rust-analyzer", ()),),
}


@dataclass(frozen=True)
class ServerInfo:
    """一个语言桶的探测结论。

    `available=False` 时 `reason` 一定非空 —— 界面直接展示这句话，
    所以它必须是人话（「未找到 gopls（未安装或不在 PATH 中）」），
    而不是异常字符串。
    """

    language: str
    available: bool
    argv: tuple[str, ...] = ()
    command: str = ""
    reason: str = ""

    def to_api(self) -> dict:
        return {
            "language": self.language,
            "available": self.available,
            "command": self.command,
            "reason": self.reason,
        }


def language_for(path: str | Path) -> str:
    """按后缀判断语言桶；不认识的后缀返回空串（= LSP 不介入）。"""
    suffix = Path(str(path)).suffix.lower()
    return _SUFFIX_LANGUAGE.get(suffix, "")


def candidates(language: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return _SERVERS.get(language, ())


def resolve(path: str | Path, *, language: str = "") -> ServerInfo:
    """探测某个文件（或某个语言桶）该用哪个语言服务器。永不抛异常。"""
    lang = language or language_for(path)
    if not lang:
        return ServerInfo(
            language="",
            available=False,
            reason=f"不认识的文件类型，LSP 不介入：{Path(str(path)).name}",
        )
    options = candidates(lang)
    if not options:
        return ServerInfo(language=lang, available=False, reason=f"{lang} 没有登记语言服务器")

    tried: list[str] = []
    for command, args in options:
        found = shutil.which(command)
        if found:
            # 用 which 的绝对路径，而不是裸命令名 —— 让审计与日志里能看出
            # 到底跑的是哪一个二进制（多版本共存时这点很关键）。
            return ServerInfo(
                language=lang,
                available=True,
                argv=(found, *args),
                command=command,
            )
        tried.append(command)
    return ServerInfo(
        language=lang,
        available=False,
        reason=f"未找到 {lang} 语言服务器（{'、'.join(tried)}）：未安装或不在 PATH 中",
    )


def detect_all() -> list[ServerInfo]:
    """全量探测清单，供 `/api/lsp/status` 展示「哪些能用、哪些为什么不能用」。"""
    return [resolve("", language=lang) for lang in sorted(_SERVERS)]


def any_available() -> bool:
    """本机是否至少装了一个语言服务器（决定诊断工具是否注册进工具清单）。"""
    return any(info.available for info in detect_all())


__all__ = [
    "LANGUAGE_IDS",
    "ServerInfo",
    "any_available",
    "candidates",
    "detect_all",
    "language_for",
    "resolve",
]