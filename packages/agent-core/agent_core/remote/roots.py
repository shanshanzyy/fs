"""远端「工作区根」：把远端连接上的某个目录登记成模型可直接操作的命名空间。

## 命名空间写法

    @remote/<连接名>/<相对该连接 root 的路径>

例：连接 `gdhd-10.0.1.50` 的 root 是 `/home/gdhd`，把它下面的 `项目/smart-writer`
加入工作区后，模型就可以用**本地同名工具**直接操作：

    read_file("@remote/gdhd-10.0.1.50/项目/smart-writer/src/app.py")

内核在两道 Jail **之前**按前缀把这类调用分流到远端链路（见 `routing.py`）。

## 为什么用 `@remote/` 前缀而不是另加工具

新增工具的代价是「模型必须先判断文件在哪台机器上」—— 判断错了就把改动写到错的
地方，而且工具清单变了、提示词也变了。命名空间前缀把「判断」交给内核（纯文本
前缀匹配），模型看到的仍是原来那套工具与原来那套语义。

代价是**遮蔽**：字面叫 `@remote/<已登记连接名>/...` 的本地目录会被命名空间吃掉。
这只在第二段恰好是一条已登记连接名时发生（精确遮罩，不是整前缀封禁），
且未路由的工具收到该前缀时由 `PathGuard.reserved_prefixes` 明确报错，不静默。

## 存哪

`<workspace>/.vibe/remote.json` 的**顶层 `roots` 数组**，与连接清单同一份文件、
同一套原子写与 0600 权限。**不升 `FORMAT_VERSION`**：老客户端把 `roots` 当未知
键忽略即可，仍能正常读连接。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from .errors import RemoteConfigError

# 命名空间前缀。**单一真源** —— routing.py 与提示词都从这里取，避免两处漂移。
PREFIX = "@remote/"


def is_ns_path(path: object) -> bool:
    """是不是命名空间写法（只看前缀，不看是否已登记）。"""
    return isinstance(path, str) and path.startswith(PREFIX)


def normalize_sub(raw: str | None) -> str:
    """归一化「相对 root 的子目录」写法。空串表示 root 本身。

    纯文本校验，**不碰远端**：加根不该要求设备此刻可达（离线也能先加好）。
    """
    text = str(raw or "").strip().replace("\\", "/")
    if "\x00" in text:
        raise RemoteConfigError("远端目录含空字节，已拒绝")
    if text.startswith("/"):
        raise RemoteConfigError(
            "「加入工作区」的目录必须是相对于连接 root 的**相对路径**（不要以 / 开头）。\n"
            "  例：root 是 /home/gdhd 时，要加入 /home/gdhd/项目/smart-writer，就填 `项目/smart-writer`。"
        )
    if text.startswith("-"):
        raise RemoteConfigError(f"远端目录不能以 - 开头（实际: {raw!r}）")
    # 必须看**原始**分段：normpath 会把 `..` 消解掉，那时再找永远找不到。
    segments = [seg for seg in text.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in segments):
        raise RemoteConfigError(f"远端目录含 .. 段（实际: {raw!r}）")
    normalized = posixpath.normpath(text)
    if normalized in ("", "."):
        return ""
    return normalized.lstrip("/")


@dataclass(frozen=True)
class RemoteRoot:
    """一个已登记的远端根。`sub` 恒为归一化后的相对路径（"" = root 本身）。"""

    connection: str
    sub: str = ""
    label: str = ""

    @property
    def key(self) -> str:
        """登记键：连接名 + 子目录。与 `ChangeEntry.key` 同范式。"""
        return f"{self.connection}:{self.sub}"

    def ns_path(self) -> str:
        """命名空间写法：`@remote/<连接名>[/<子目录>]`。

        注意**不带尾斜杠** —— 尾斜杠会让「根自己」与「根下的第一层」在字符串
        比对时混在一起（`@remote/c/` vs `@remote/c/x`），比对前必须先归一化。
        """
        base = f"{PREFIX}{self.connection}"
        return f"{base}/{self.sub}" if self.sub else base

    def abs_root(self, conn: Any) -> str:
        """远端绝对路径（用连接自己的 root 现算，不落盘 —— 免得 root 改了它还留着旧值）。"""
        root = getattr(conn, "root_normalized", "/") or "/"
        if not self.sub:
            return root
        return posixpath.join(root, self.sub)

    def to_dict(self) -> dict[str, Any]:
        return {"connection": self.connection, "sub": self.sub, "label": self.label}

    @classmethod
    def from_dict(cls, raw: Any) -> "RemoteRoot":
        """宽容反序列化：非字符串字段按空处理，不做结构校验（由 validate_root 兜底）。"""
        if not isinstance(raw, dict):
            raise RemoteConfigError(f"roots 条目需为对象，实际: {type(raw).__name__}")
        connection = raw.get("connection")
        sub = raw.get("sub")
        label = raw.get("label")
        return cls(
            connection=connection if isinstance(connection, str) else "",
            sub=sub if isinstance(sub, str) else "",
            label=label if isinstance(label, str) else "",
        )


@dataclass(frozen=True)
class RouteHit:
    """一次成功分流的结论：交给远端链路的所有信息。"""

    connection: str
    sub: str          # 命中的登记根（相对 conn.root）
    rel_to_root: str  # 交给 RemoteWorkspace 的 rel（相对 conn.root）
    ns_path: str      # 回填 meta["path"] 用（命名空间写法）

    def remote_rel(self) -> str:
        return self.rel_to_root


def validate_root(root: RemoteRoot, store: Any) -> str:
    """校验一个根是否可用。返回空串表示合法，否则是人话原因。

    只做**纯文本**校验：连接是否存在、sub 是否归一化合法。远端目录此刻是否存在
    不在这里判定 —— 那要连通性，属于「展开时才如实报错」的范畴。
    """
    if not root.connection:
        return "connection 为空"
    names = store.names()
    if root.connection not in names:
        available = "、".join(names) or "（无）"
        return f"未找到名为 {root.connection!r} 的远程连接。可用连接：{available}"
    try:
        normalized = normalize_sub(root.sub)
    except RemoteConfigError as exc:
        return str(exc)
    if normalized != root.sub:
        return f"子目录写法不合法（应归一化为 {normalized!r}，实际 {root.sub!r}）"
    return ""


def parse_ns_root(text: str | None) -> tuple[str, str]:
    """把 `@remote/<连接名>/<子目录>` 拆成 `(连接名, 子目录)`。

    子目录走 `normalize_sub`（拒 `..` / 拒前导 `-` / 拒空字节），因此从这个入口
    写不出一个逃出连接 root 的写法 —— 轻子目录那道校验，这边就是纯取值。
    """
    raw = str(text or "").strip().replace("\\", "/")
    if not raw:
        return ("", "")
    if not raw.startswith(PREFIX):
        raise RemoteConfigError(
            f"远程工作区须写成 {PREFIX}<连接名>/<子目录> 形式（实际: {text!r}）"
        )
    rest = raw[len(PREFIX) :].strip("/")
    if not rest:
        raise RemoteConfigError(f"{PREFIX} 后面还要跟连接名（实际: {text!r}）")
    connection, _, sub = rest.partition("/")
    if not connection:
        raise RemoteConfigError(f"远程工作区缺少连接名（实际: {text!r}）")
    return (connection, normalize_sub(sub))


def default_root_rel(sub: str, raw_path: object, *, root: str = "/") -> str:
    """把模型写的路径收敛成「相对连接 root 的 rel」。

    支持两种写法，因为模型很可能直接照着远端报错里的绝对路径抄：

    - **相对写法**（多数情况）：`src/app.py` → `join(sub, "src/app.py")`
    - **远端绝对写法**：`/home/dev/项目/app/src/app.py`，要求它确实落在
      `<root>/<sub>` 之内，剪掉这段前缀后与相对写法同路。

    判定顺序刻意是「先拼接 → 再整体归一化 → 最后看 `..`」：路径在这里是**拼出来的**，
    光看入参的原始分段抓不到 `sub + "../.."` 这种组合式逃逸。拼完归一化之后
    `..` 只可能出现在**开头**，这时的判定是完备的 —— 与 `normalize_sub` 那套
    「必须看得见原始 `..`」的纪律是同一条，只是换了个手工序。

    `root` 是连接的 `root_normalized`，只在绝对路径分支用得上。
    """
    if isinstance(raw_path, bool) or not isinstance(raw_path, str):
        raise RemoteConfigError(f"路径需为字符串（实际: {type(raw_path).__name__}）")
    text = raw_path.strip().replace("\\", "/")
    if "\x00" in text:
        raise RemoteConfigError("路径含空字节，已拒绝")
    base_sub = sub.strip("/")

    if text.startswith("/"):
        root_text = str(root or "/").strip().rstrip("/")
        prefix = f"{root_text}/{base_sub}".rstrip("/") if base_sub else (root_text or "/")
        if not (text == prefix or text.startswith(prefix + "/")):
            raise RemoteConfigError(
                f"绝对路径不在远程工作区之内：{text}\n"
                f"  远程工作区根：<主机>:{prefix}\n"
                f"  请改用相对写法（相对该根的 POSIX 路径）。"
            )
        rel_in_sub = text[len(prefix) :].strip("/")
    else:
        rel_in_sub = text.strip("/")

    if not rel_in_sub:
        # 既可能是「写了裸根」，也可能是绝对路径恰好等于工作区根 —— 两种都不是
        # 一个文件，让它去 Jail 里报「文件不存在」没有意义，这里直接讲清。
        raise RemoteConfigError("路径为空：请给出远程工作区内的具体文件或目录")

    joined = posixpath.normpath(posixpath.join(base_sub, rel_in_sub) if base_sub else rel_in_sub)
    if joined == ".." or joined.startswith("../"):
        raise RemoteConfigError(
            f"路径逃出了远程工作区根目录（../  越界）：{raw_path!r}\n"
            f"  远程工作区子目录：{base_sub or '/（连接 root 本身）'}"
        )
    if joined in ("", "."):
        raise RemoteConfigError("路径逃出了远程工作区根目录：请给出根之内的相对路径")
    return joined


__all__ = [
    "PREFIX",
    "RemoteRoot",
    "RouteHit",
    "default_root_rel",
    "is_ns_path",
    "normalize_sub",
    "parse_ns_root",
    "validate_root",
]
