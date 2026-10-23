"""流式下载 + sha256 校验 + 原子落盘（M9）。

下载的是**一个可执行安装器**，所以这里有三条不能松的约束：

1. **绝不整包读进内存**。分块（1 MiB）边下边算 sha256，内存占用与文件大小无关。
   安装器动辄几百 MB，一次性读进来会把一台 8 GB 的内勤机直接压垮。
2. **先落 tmp，验完再 `os.replace`**。`part-<rand>.tmp` → 校验 sha256 → 改名成
   `VibeAgent-Setup-<v>.exe`。改名在 POSIX/Windows 上都是原子的：磁盘上要么
   没有可安装文件，要么是一个**已验签**的完整文件。**绝不存在「半个 exe 被
   当成合法的」**这条路径 —— 那条路径的终点是「运行一个损坏的安装器」。
3. **两道上限都要守**。清单声明的 `size` 是发布者的承诺，`VIBE_UPDATE_MAX_BYTES`
   是本机的兜底；超了立刻中止并删 tmp。没有上限的下载 = 一个可以写满磁盘的入口。

`sha256` 的定位要说清楚：它是**传输完整性**的第二道锁，不是信任锚。
能替换镜像的人同样能改清单里的 sha256，所以它挡的是「文件传坏了」。
真正的信任锚是清单上的 RSA 签名（见 `keys.py` / `manifest.py`）。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, IO

from ..errors import UpdateError

#: 分块大小。与 checkpoint.py 的 `_sha256_file` 同口径。
CHUNK = 1 << 20

#: 安装器文件的固定命名（壳侧 `isStagingPathValid` 按这个正则白名单校验）。
_PACKAGE_RE = re.compile(r"^VibeAgent-Setup-[0-9A-Za-z._-]{1,40}\.exe$")

#: 允许出现在文件名里的版本串。
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z._-]{1,32}$")

#: 取文本响应（manifest.json）时的字节上限。清单很小，超过就是源给错了东西。
TEXT_LIMIT = 1 << 20

#: `opener(url) -> 可读字节流` 的注入点。默认走 urllib / 本地文件，
#: 测试用假 opener 计数与注入，**不碰真实网络**。
Opener = Callable[[str], IO[bytes]]


# --------------------------------------------------------------------------- 命名
def package_filename(version: str) -> str:
    """由版本号推出安装器文件名：`VibeAgent-Setup-<version>.exe`。

    版本串先过白名单再拼进文件名 —— 不做这件事的话，一个形如
    `..\\..\\evil` 的「版本号」就能让落盘路径跳出 updates 目录。
    """
    token = str(version or "").strip()
    if token[:1] in ("v", "V"):
        token = token[1:]
    if not _SAFE_VERSION.match(token):
        raise UpdateError(
            f"版本号含非法字符，不能作为文件名：{version!r}", kind="shape"
        )
    return f"VibeAgent-Setup-{token}.exe"


def is_package_filename(name: str) -> bool:
    return bool(_PACKAGE_RE.match(str(name or "")))


# --------------------------------------------------------------------------- 传输
def open_stream(url: str, *, timeout: int = 20, opener: Opener | None = None) -> IO[bytes]:
    """打开一个只读字节流。支持 http(s) / file:// / 裸路径（本地镜像）。

    用 `"://"` 判断协议而不是 `urlparse().scheme`：Windows 下
    `urlparse("E:\\mirror\\x")` 会把盘符 `E` 当成 scheme，从而把本地路径
    误判成「未知协议」。
    """
    target = str(url or "")
    if opener is not None:
        return opener(target)
    if "://" in target:
        scheme = target.split("://", 1)[0].lower()
        if scheme in ("http", "https", "file"):
            return urllib.request.urlopen(target, timeout=timeout)
        raise UpdateError(f"不支持的下载协议：{scheme}://", kind="network")
    path = Path(target).expanduser()
    try:
        return path.open("rb")
    except OSError as exc:
        raise UpdateError(f"本地文件不可读：{path}", kind="network") from exc


def _close(stream: Any) -> None:
    closer = getattr(stream, "close", None)
    if callable(closer):
        try:
            closer()
        except OSError:
            pass


def fetch_text(
    url: str, *, timeout: int = 20, opener: Opener | None = None, limit: int = TEXT_LIMIT
) -> str:
    """取一段小文本（manifest.json）。超过 `limit` 视为源给错了东西。"""
    stream = None
    try:
        stream = open_stream(url, timeout=timeout, opener=opener)
        data = stream.read(limit + 1)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 传输层异常统一归类
        raise UpdateError(f"读取失败：{exc}", kind="network") from exc
    finally:
        _close(stream)
    if len(data) > limit:
        raise UpdateError(f"响应过大（>{limit} 字节），拒绝当作清单", kind="shape")
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- 落盘
def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def sha256_file(path: str | Path) -> str:
    """算一个已有文件的 sha256（用于「同版本 staging 是否已完成」的幂等判定）。"""
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def download_to_staging(
    *,
    url: str,
    dest_dir: str | Path,
    filename: str,
    expected_sha256: str,
    expected_size: int,
    max_bytes: int = 0,
    opener: Opener | None = None,
    timeout: int = 20,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把 `url` 下载到 `dest_dir/filename`，校验通过后原子改名。

    失败（网络中断 / 超上限 / sha256 不符 / 字节数不符）一律**删掉 tmp**
    并抛 `UpdateError`，磁盘上不留半个文件。
    """
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / f"part-{secrets.token_hex(8)}.tmp"

    # 两道上限取更紧的那条：清单声明的是承诺，本机上限是兜底。
    cap = expected_size
    if max_bytes and max_bytes > 0:
        cap = min(cap, max_bytes)

    digest = hashlib.sha256()
    total = 0
    step = max(1, expected_size // 20)  # 约每 5% 报一次进度
    next_mark = step
    stream = None
    try:
        stream = open_stream(url, timeout=timeout, opener=opener)
        with tmp.open("wb") as fh:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise UpdateError(
                        f"下载字节超过上限（已 {total} > {cap}），已中止", kind="size"
                    )
                digest.update(chunk)
                fh.write(chunk)
                if on_progress is not None and total >= next_mark:
                    on_progress(total, expected_size)
                    next_mark += step
    except UpdateError:
        _discard(tmp)
        raise
    except Exception as exc:  # noqa: BLE001 —— 传输层异常统一归类
        _discard(tmp)
        raise UpdateError(f"下载中断：{exc}", kind="network") from exc
    finally:
        _close(stream)

    if total != expected_size:
        _discard(tmp)
        raise UpdateError(
            f"下载字节与清单声明不符（实得 {total}，声明 {expected_size}）", kind="size"
        )
    actual = digest.hexdigest()
    if actual != str(expected_sha256 or ""):
        _discard(tmp)
        raise UpdateError(
            f"下载内容 sha256 与清单声明不符（实算 {actual[:16]}…）", kind="sha256"
        )

    final = dest / filename
    os.replace(tmp, final)
    _restrict(final)
    if on_progress is not None:
        on_progress(total, expected_size)
    return final


def cleanup_stale_tmp(
    updates_dir: str | Path, *, max_age_seconds: int = 86400, now: float | None = None
) -> int:
    """清扫超过 `max_age_seconds` 的 `part-*.tmp`，返回删除个数。

    只在「会话启动」时调一次。异常一律吞掉：清扫失败不该让任何流程失败，
    它本来就是一次 best-effort 的卫生动作。
    """
    base = Path(updates_dir).expanduser()
    if not base.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for path in base.glob("**/part-*.tmp"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def prune_staged(
    updates_dir: str | Path, *, keep: int = 2, protect: str | Path = ""
) -> int:
    """只保留最近 `keep` 个已 staging 的版本目录，删掉更旧的，返回删除个数。

    存在的理由：新版启动即崩时，用户自救的唯一凭据就是**上一个版本的安装包**。
    全都留着会撑爆磁盘，只留最近 2 版既够自救、又不会无限增长。
    `protect` 指向的目录（通常是刚 staging 的那个）**永不删**。
    """
    base = Path(updates_dir).expanduser()
    if not base.is_dir():
        return 0
    protected = Path(protect).expanduser() if protect else None
    staged: list[tuple[float, Path]] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if protected is not None and child == protected:
            continue
        packages = list(child.glob("VibeAgent-Setup-*.exe"))
        if not packages:
            continue
        try:
            mtime = max(pkg.stat().st_mtime for pkg in packages)
        except OSError:
            continue
        staged.append((mtime, child))
    staged.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    for _, child in staged[max(0, keep) :]:
        try:
            for entry in child.iterdir():
                entry.unlink()
            child.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "CHUNK",
    "Opener",
    "TEXT_LIMIT",
    "cleanup_stale_tmp",
    "download_to_staging",
    "fetch_text",
    "is_package_filename",
    "open_stream",
    "package_filename",
    "prune_staged",
    "sha256_file",
]
