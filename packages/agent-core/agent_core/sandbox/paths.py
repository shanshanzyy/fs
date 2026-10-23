"""路径沙箱（Jail）：把 Agent 的文件访问锁死在工作区目录内。

对应 docs/ARCHITECTURE.md 的 ADR-008 第 4 条：路径 jail。
两道防线：
    1. 越狱防护 —— 任何解析后落不到工作区内的路径一律拒绝（绝对路径逃逸、.. 逃逸、
       以及工作区内指向外部的符号链接，都会被 resolve() 展开后拦下）。
    2. 敏感文件拦截 —— .env / *.pem / id_rsa / .aws/ 等一律不允许读入上下文，
       避免模型把密钥带进 Prompt 进而外泄。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SandboxViolation

# 命中即视为敏感文件（对工作区相对路径做匹配，统一用正斜杠）
_SENSITIVE_SOURCES = (
    r"(^|/)\.env($|\.)",           # .env / .env.local / .env.production
    r"(^|/)\.envrc$",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"\.jks$",
    r"\.keystore$",
    r"(^|/)id_rsa",
    r"(^|/)id_dsa",
    r"(^|/)id_ecdsa",
    r"(^|/)id_ed25519",
    r"(^|/)\.ssh/",
    r"(^|/)\.aws/",
    r"(^|/)\.kube/",
    r"(^|/)\.gnupg/",
    r"(^|/)credentials$",
    r"(^|/)credentials\.json$",
    r"(^|/)secrets?\.(json|ya?ml|toml|ini|cfg)$",
    r"(^|/)\.npmrc$",
    r"(^|/)\.pypirc$",
    r"(^|/)\.netrc$",
    r"(^|/)\.git-credentials$",
    r"(^|/)\.docker/config\.json$",
    r"(^|/)\.vibe/audit\.jsonl$",
)

SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(src, re.IGNORECASE) for src in _SENSITIVE_SOURCES
)


def _looks_sensitive(rel_posix: str) -> bool:
    return any(p.search(rel_posix) for p in SENSITIVE_PATTERNS)


@dataclass
class PathGuard:
    """工作区路径守卫。所有文件工具的入口都必须先过这里。"""

    root: Path
    allow_sensitive: bool = False
    # 策略下发的额外拒绝模式（glob，如 `**/secrets/*`、`config/prod*`）。
    #
    # 为什么不复用内置的敏感判定：内置那套是**通用**规则（.env / *.pem / id_rsa），
    # 而策略里的拒绝清单是**部署方自己**要禁的东西（比如某个客户的
    # 特殊目录、某个业务库）。混进内置规则会让通用规则被业务特例污染，
    # 且「为什么这个文件被拒」会变得没法解释。两套并存、理由分开报，更好排查。
    denied_patterns: tuple[str, ...] = ()
    # 保留命名空间前缀（M12）：以这些前缀开头的路径不由本地工具处理。
    #
    # 默认 `()` = 零影响。启用远端路由时由 `Agent` 传入（如 `"@remote/"`）。
    # 它兜住的是**未路由**的工具（`repo_map` / `semantic_search` / `git_diff` /
    # `edit_files` / `run_command` …）收到命名空间路径的情况 —— 没有这条，
    # `@remote/devbox/src/app.py` 会被当成本地相对路径去解析，最后报一句
    # 「文件不存在」，用户与模型都不知道真正原因。这里明确报错，不静默。
    reserved_prefixes: tuple[str, ...] = ()
    _root_resolved: Path = field(init=False, repr=False)
    _patterns: tuple[str, ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        self._root_resolved = Path(self.root).expanduser().resolve()
        if not self._root_resolved.is_dir():
            raise SandboxViolation(
                f"工作区目录不存在: {self._root_resolved}", kind="workspace"
            )
        # 规范成小写、去掉前导 `./`，让策略作者写的 `./secrets/*` 与
        # `secrets/*` 等价 —— 否则清单里一条写错前缀就整条失效。
        self._patterns = tuple(
            p.strip().replace("\\", "/").lstrip("./").lower()
            for p in (self.denied_patterns or ())
            if str(p).strip()
        )

    # ------------------------------------------------------------------ 策略
    def matched_policy_deny(self, path: Path) -> str:
        """命中策略拒绝清单时返回那条模式，否则空串。"""
        if not self._patterns:
            return ""
        try:
            rel = self._to_posix_rel(path.resolve()).lower()
        except ValueError:
            return "<越出工作区>"
        for pattern in self._patterns:
            if fnmatch.fnmatch(rel, pattern):
                return pattern
            # 目录模式（以 / 结尾或本身是目录）也要能拦住整棵子树：
            # 策略里写 `secrets` 的人，期望的是「整个 secrets 目录都别碰」，
            # 而不是「只有名为 secrets 的文件」。
            bare = pattern.rstrip("/*")
            if bare and (rel == bare or rel.startswith(bare + "/")):
                return pattern
        return ""


    # ------------------------------------------------------------------ 基础
    @property
    def root_resolved(self) -> Path:
        return self._root_resolved

    def _to_posix_rel(self, path: Path) -> str:
        return path.relative_to(self._root_resolved).as_posix()

    def relative(self, path: Path) -> str:
        """转为展示用的工作区相对路径。"""
        try:
            return self._to_posix_rel(path.resolve())
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------ 解析
    def resolve(self, raw: str | Path | None) -> Path:
        """把用户/模型给的路径解析为绝对路径，并校验是否落在工作区内。

        不要求路径已存在（写新文件时需要）。
        """
        if raw is None or str(raw).strip() == "":
            return self._root_resolved

        text = str(raw).strip().strip('"').strip("'")
        if "\x00" in text:
            raise SandboxViolation("路径中包含空字节，已拒绝", kind="malformed", path=text)

        for prefix in self.reserved_prefixes:
            if isinstance(prefix, str) and prefix and text.startswith(prefix):
                raise SandboxViolation(
                    f"路径落在保留命名空间 {prefix!r} 内，本地工具不处理：{text}\n"
                    "  这类路径属于远端工作面，需由路由后的同名文件工具"
                    "（read_file / edit_file / grep …）处理。",
                    kind="reserved_ns",
                    path=text[:200],
                )

        try:
            candidate = Path(text)
        except (ValueError, OSError) as exc:
            raise SandboxViolation(
                f"路径无法解析: {raw!r} ({exc})", kind="malformed", path=str(raw)[:200]
            ) from exc

        if not candidate.is_absolute():
            candidate = self._root_resolved / candidate

        try:
            # strict=False：允许目标尚不存在；但符号链接会被展开，逃逸会被发现
            resolved = candidate.resolve()
        except OSError as exc:
            raise SandboxViolation(
                f"路径无法解析: {raw!r} ({exc})", kind="malformed", path=text[:200]
            ) from exc

        if resolved != self._root_resolved and self._root_resolved not in resolved.parents:
            raise SandboxViolation(
                "路径越出工作区边界，已拒绝：\n"
                f"  请求路径 : {raw}\n"
                f"  解析结果 : {resolved}\n"
                f"  工作区根 : {self._root_resolved}",
                kind="escape",
                path=text[:200],
            )
        return resolved

    # ------------------------------------------------------------------ 校验
    def is_sensitive(self, path: Path) -> bool:
        try:
            rel = self._to_posix_rel(path.resolve())
        except ValueError:
            return True  # 落不到工作区内，按最坏情况处理
        return _looks_sensitive(rel)

    def check_read(self, raw: str | Path | None) -> Path:
        """读操作校验：越狱 + 策略拒绝清单 + 敏感文件。"""
        path = self.resolve(raw)
        matched = self.matched_policy_deny(path)
        if matched:
            raise SandboxViolation(
                f"路径命中治理策略的拒绝清单，已拒绝读入上下文：{self.relative(path)}\n"
                f"  命中模式: {matched}\n"
                "  该清单由服务端统一下发，客户端不能自行放宽。",
                kind="policy_deny",
                path=self.relative(path),
            )
        if self.is_sensitive(path) and not self.allow_sensitive:
            raise SandboxViolation(
                f"文件被策略标记为敏感，拒绝读入上下文：{self.relative(path)}\n"
                "  如确需读取，请显式开启 allow_sensitive_read（默认关闭）。",
                kind="sensitive",
                path=self.relative(path),
            )
        return path

    def check_write(self, raw: str | Path | None) -> Path:
        """写操作校验：越狱 + 策略拒绝清单 + 敏感文件 + 禁止写审计目录。"""
        path = self.resolve(raw)
        if path == self._root_resolved:
            raise SandboxViolation(
                "拒绝把工作区根目录当作文件写入", kind="root_write", path="."
            )
        matched = self.matched_policy_deny(path)
        if matched:
            raise SandboxViolation(
                f"路径命中治理策略的拒绝清单，已拒绝写入：{self.relative(path)}\n"
                f"  命中模式: {matched}",
                kind="policy_deny",
                path=self.relative(path),
            )
        if self.is_sensitive(path) and not self.allow_sensitive:
            raise SandboxViolation(
                f"文件被策略标记为敏感，拒绝写入：{self.relative(path)}",
                kind="sensitive",
                path=self.relative(path),
            )
        vibe_dir = self._root_resolved / ".vibe"
        if path == vibe_dir or vibe_dir in path.parents:
            raise SandboxViolation(
                "拒绝写入 .vibe 内部目录（审计与检查点专用）",
                kind="audit_dir",
                path=self.relative(path),
            )
        return path

    def check_list(self, raw: str | Path | None) -> Path:
        """列目录校验：只做越狱检查（目录名本身可能含敏感词但列表无害）。"""
        return self.resolve(raw)
