"""远端路径 Jail：把 Agent 在**远端设备**上的文件访问锁死在远端工作目录内。

与本地 `sandbox/paths.py` 的 `PathGuard` 是**同一套语义**（越狱防护 + 敏感文件
拦截 + 策略拒绝清单），但实现方式必须不同，原因只有一个：

    **本地 `Path.resolve()` 够不着远端的文件系统。**

本地 guard 的越狱判定依赖真实解析（符号链接会被展开，指到工作区外的链接会被
发现）。这条路在远端走不通 —— 我们只有字符串。于是这里退化为**文本级归一化**：

    posixpath.normpath 消解 `.` 与 `..` → 再与归一化后的 root 做前缀比对

这能挡住 `..` 逃逸、绝对路径逃逸、空字节，属于**必要但不充分**的防线。
它挡不住「工作区内一个指向外部目录的符号链接」—— 那需要远端配合执行
`realpath`。因此：

  - 本文件负责**离线可判定**的那部分（纯字符串，无 IO，可单测）；
  - 符号链接逃逸由 `workspace.py` 在远端能力允许时用 `realpath` 二次确认
    （见 `RemoteWorkspace.verify_realpath`），能力不可用时**显式记录降级**，
    而不是假装安全。

前缀比对是大小写敏感的：远端若挂的是大小写不敏感的文件系统（macOS 默认），
理论上存在大小写变体绕过前缀比对的可能。实践中 root 由用户显式给出、
路径由本 guard 生成，这一风险可接受；但值得知道它存在。
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field

from ..errors import SandboxViolation
from ..sandbox.paths import SENSITIVE_PATTERNS


def _looks_sensitive(rel_posix: str) -> bool:
    """与本地 guard 共用同一份敏感文件规则表（`SENSITIVE_PATTERNS`）。

    刻意复用而不是另抄一份：敏感文件清单是**安全红线**，两处各写一份必然
    会随时间漂移 —— 本地补上了 `.npmrc` 而远端没补，就是一条真实的泄漏口。
    """
    return any(pattern.search(rel_posix) for pattern in SENSITIVE_PATTERNS)


@dataclass
class RemotePathGuard:
    """远端工作区路径守卫。所有远端文件操作的入口都必须先过这里。"""

    root: str
    allow_sensitive: bool = False
    # 策略下发的额外拒绝清单（glob）。与本地 guard 同义：治理面的部署方
    # 自定义禁读禁写范围，与内置的通用敏感规则**并存、理由分开报**。
    denied_patterns: tuple[str, ...] = ()
    _normalized_root: str = field(init=False, repr=False, default="")
    _patterns: tuple[str, ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        raw_root = str(self.root or "").strip()
        if not raw_root.startswith("/"):
            raise SandboxViolation(
                f"远端工作区根必须是绝对 POSIX 路径: {raw_root!r}", kind="workspace"
            )
        # 必须看**原始**分段：normpath 会把 `..` 消解掉，
        # `/srv/../../etc` 归一化后是 `/etc`，之后再找 `..` 永远找不到。
        segments = [seg for seg in raw_root.split("/") if seg not in ("", ".")]
        if any(seg == ".." for seg in segments):
            raise SandboxViolation(
                f"远端工作区根含 .. 段: {raw_root!r}", kind="workspace"
            )
        normalized = posixpath.normpath(raw_root)
        self._normalized_root = "/" if normalized == "." else normalized
        self._patterns = tuple(
            p.strip().replace("\\", "/").lstrip("./").lower()
            for p in (self.denied_patterns or ())
            if str(p).strip()
        )

    # ------------------------------------------------------------------ 基础
    @property
    def root_resolved(self) -> str:
        """归一化后的 root（去尾斜杠、消解 `.`）。与本地 PathGuard 同名。"""
        return self._normalized_root

    def relative(self, abs_posix: str) -> str:
        """转为展示用的工作区相对路径。落不到 root 内时原样返回。"""
        text = str(abs_posix)
        if text == self._normalized_root:
            return "."
        prefix = self._normalized_root.rstrip("/") + "/"
        return text[len(prefix) :] if text.startswith(prefix) else text

    def is_inside(self, abs_posix: str) -> bool:
        if abs_posix == self._normalized_root:
            return True
        return abs_posix.startswith(self._normalized_root.rstrip("/") + "/")

    # ------------------------------------------------------------------ 解析
    def normalize(self, raw: str | None) -> str:
        """把模型/用户给的路径解析为 root 内的绝对 POSIX 路径。

        不要求目标已存在（写新文件时需要）。**纯字符串运算，不做任何 IO。**
        """
        if raw is None or str(raw).strip() == "":
            return self._normalized_root

        text = str(raw).strip().strip('"').strip("'")
        if "\x00" in text:
            raise SandboxViolation(
                "远端路径中包含空字节，已拒绝", kind="malformed", path=text[:200]
            )
        # 反斜杠在远端 POSIX 上是**合法文件名字符**，不是分隔符。
        # 不把它当分隔符转换（那是 Windows 思维）。
        if not text.startswith("/"):
            text = posixpath.join(self._normalized_root, text)

        # normpath 会消解 `.` 与 `..`；若 `..` 消解后仍留在开头，
        # 说明它试图逃到 root 之上 —— 此时结果不再以 `/` 开头或前缀比对失败，
        # 下面两条检查会分别兜住。
        normalized = posixpath.normpath(text)
        if not normalized.startswith("/") or not self.is_inside(normalized):
            raise SandboxViolation(
                "远端路径越出工作区边界，已拒绝：\n"
                f"  请求路径 : {raw}\n"
                f"  解析结果 : {normalized}\n"
                f"  工作区根 : {self._normalized_root}",
                kind="escape",
                path=str(raw)[:200],
            )
        return normalized

    # ------------------------------------------------------------------ 策略
    def matched_policy_deny(self, abs_posix: str) -> str:
        """命中策略拒绝清单时返回那条模式，否则空串。"""
        if not self._patterns:
            return ""
        rel = self.relative(abs_posix).lower()
        if rel.startswith("/") or rel.startswith("..") or rel == ".":
            return "<越出工作区>"
        for pattern in self._patterns:
            if fnmatch.fnmatch(rel, pattern):
                return pattern
            bare = pattern.rstrip("/*")
            if bare and (rel == bare or rel.startswith(bare + "/")):
                return pattern
        return ""

    def is_sensitive(self, abs_posix: str) -> bool:
        rel = self.relative(abs_posix)
        if rel.startswith("/") or rel.startswith(".."):
            return True  # 落不到工作区内，按最坏情况处理
        return _looks_sensitive(rel)

    # ------------------------------------------------------------------ 校验
    def check_read(self, raw: str | None) -> str:
        """读校验：越狱 + 策略拒绝清单 + 敏感文件。"""
        path = self.normalize(raw)
        matched = self.matched_policy_deny(path)
        if matched:
            raise SandboxViolation(
                f"远端路径命中治理策略的拒绝清单，已拒绝读入上下文：{self.relative(path)}\n"
                f"  命中模式: {matched}\n"
                "  该清单由服务端统一下发，客户端不能自行放宽。",
                kind="policy_deny",
                path=self.relative(path),
            )
        if self.is_sensitive(path) and not self.allow_sensitive:
            raise SandboxViolation(
                f"远端文件被策略标记为敏感，拒绝读入上下文：{self.relative(path)}\n"
                "  如确需读取，请显式开启敏感读取（默认关闭）。",
                kind="sensitive",
                path=self.relative(path),
            )
        return path

    def check_write(self, raw: str | None) -> str:
        """写校验：越狱 + 策略拒绝清单 + 敏感文件 + 禁止写远端 `.vibe`。"""
        path = self.normalize(raw)
        if path == self._normalized_root:
            raise SandboxViolation(
                "拒绝把远端工作区根目录当作文件写入", kind="root_write", path="."
            )
        matched = self.matched_policy_deny(path)
        if matched:
            raise SandboxViolation(
                f"远端路径命中治理策略的拒绝清单，已拒绝写入：{self.relative(path)}\n"
                f"  命中模式: {matched}",
                kind="policy_deny",
                path=self.relative(path),
            )
        if self.is_sensitive(path) and not self.allow_sensitive:
            raise SandboxViolation(
                f"远端文件被策略标记为敏感，拒绝写入：{self.relative(path)}",
                kind="sensitive",
                path=self.relative(path),
            )
        # 远端 `.vibe` 可能是**另一台机器上**的 Agent 自己的审计与检查点目录。
        # 覆盖它等于篡改别人机器上的审计链 —— 本地禁止的那条理由在这里同样成立，
        # 而且后果更重（跨机取证会被污染）。
        vibe_dir = self._normalized_root.rstrip("/") + "/.vibe"
        if path == vibe_dir or path.startswith(vibe_dir + "/"):
            raise SandboxViolation(
                "拒绝写入远端 .vibe 内部目录（可能是远端 Agent 的审计与检查点）",
                kind="audit_dir",
                path=self.relative(path),
            )
        return path

    def check_list(self, raw: str | None) -> str:
        """列目录校验：只做越狱检查（目录名本身可能含敏感词但列表无害）。"""
        return self.normalize(raw)


__all__ = ["RemotePathGuard"]
