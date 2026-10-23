"""更新编排（M9）。

`UpdateService` 把版本比较、双通道选源、验签、反降级、下载、落盘串成
一条链，并对外只暴露三件事：`check` / `download` / `status`（外加 `skip`）。

几处刻意的取舍：

  · **编排在内核，执行在壳**。本模块做到「产出一个已验签的安装器路径」为止，
    真正的「覆盖安装 + 重启」由桌面壳完成（`apps/desktop/src/update.js`）。
    内核永远不 spawn 安装器 —— 那是壳的职责，也是薄壳原则的边界。
  · **`update_disabled=1` 时零网络请求**。不是「请求了但不用」，而是**根本不发**。
    内网高合规环境里，一个「关掉了却还在偷偷 check」的更新器是不可接受的。
  · **活动 run 时拒绝下载**。下载会占带宽、写大文件，且在任务执行中触发
    「重启换版本」会丢用户现场。所以下载闸门返回 409（由 host 层映射）。
  · **公钥缺失 = 不更新**。没有可信公钥时验签必然失败（fail-closed），
    这里把它归类成一次 failed check，而不是「跳过校验继续装」。
"""

from __future__ import annotations

import platform as _platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..errors import UpdateError
from .download import (
    Opener,
    cleanup_stale_tmp,
    download_to_staging,
    package_filename,
    prune_staged,
    sha256_file,
)
from .keys import load_pubkey
from .manifest import Manifest
from .source import SourceDecision, collect
from .state import UpdateState, accept, allows, load, save
from .version import at_least, is_newer

#: 默认落盘根：`~/.vibe/updates`（用户级，跨工作区共享）。
DEFAULT_UPDATES_DIR = Path.home() / ".vibe" / "updates"

#: 默认本机上限（512 MiB）。
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


# --------------------------------------------------------------------------- 环境
def detect_platform() -> str:
    """映射到治理面 `releases.PLATFORMS` 的取值。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def detect_arch() -> str:
    """映射到治理面 `releases.ARCHES` 的取值（x64 / arm64）。"""
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


# --------------------------------------------------------------------------- 选项
@dataclass(frozen=True)
class UpdateOptions:
    """从 `Config` 抽出的、与网络/落盘相关的少量参数。

    刻意不复用整个 `Config`：`update` 模块要能在**没有工作区**的场景下工作，
    而 `Config.load` 要求一个真实存在的工作区。把依赖收窄成这几个字段，
    `update status` / `update check` 才能照 `governance` 子命令的先例
    「不依赖工作区」运行。
    """

    updates_dir: Path
    channel: str = "stable"
    base_url: str = ""
    disabled: bool = False
    check_on_start: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    pubkey_override: str = ""
    current_version: str = ""
    platform: str = ""
    arch: str = ""

    @classmethod
    def from_config(cls, config: Any) -> "UpdateOptions":
        raw_dir = str(getattr(config, "update_dir", "") or "").strip()
        updates_dir = (
            Path(raw_dir).expanduser() if raw_dir else DEFAULT_UPDATES_DIR
        )
        raw_max = int(getattr(config, "update_max_bytes", 0) or 0)
        return cls(
            updates_dir=updates_dir,
            channel=str(getattr(config, "update_channel", "") or "stable").lower(),
            base_url=str(getattr(config, "update_base_url", "") or ""),
            disabled=bool(getattr(config, "update_disabled", False)),
            check_on_start=bool(getattr(config, "update_check_on_start", True)),
            max_bytes=raw_max if raw_max > 0 else DEFAULT_MAX_BYTES,
            pubkey_override=str(getattr(config, "update_pubkey", "") or ""),
            current_version=str(getattr(config, "current_version", "") or ""),
        )

    @classmethod
    def from_env(cls) -> "UpdateOptions":
        """直接从环境变量构建（**不依赖工作区**）。

        `update status/check` 要能在任意目录下运行（照 `governance` 子命令的先例），
        所以这里读 env 而不是 `Config.load`。env 名称与字段来源与 `config.py`
        逐一对齐（`_env_*` 辅助函数也直接复用，避免两处解析规则漂移）。
        """
        from ..config import (  # 延迟导入：避免 update 模块在 import 期拉入 config
            _UPDATE_CHANNELS,
            _env_bool,
            _env_choice,
            _env_int,
            _env_str,
        )

        raw_dir = _env_str("VIBE_UPDATE_DIR", "")
        updates_dir = Path(raw_dir).expanduser() if raw_dir else DEFAULT_UPDATES_DIR
        raw_max = _env_int("VIBE_UPDATE_MAX_BYTES", DEFAULT_MAX_BYTES)
        return cls(
            updates_dir=updates_dir,
            channel=_env_choice("VIBE_UPDATE_CHANNEL", "stable", _UPDATE_CHANNELS),
            base_url=_env_str("VIBE_UPDATE_URL", ""),
            disabled=_env_bool("VIBE_UPDATE_DISABLE", False),
            check_on_start=_env_bool("VIBE_UPDATE_CHECK_ON_START", True),
            max_bytes=raw_max if raw_max > 0 else DEFAULT_MAX_BYTES,
            pubkey_override=_env_str("VIBE_UPDATE_PUBKEY", ""),
        )


# --------------------------------------------------------------------------- 视图
def release_view(manifest: Manifest) -> dict[str, Any]:
    """清单的公开视图（含签名，签名本就可在客户端公开校验；绝不含私钥）。"""
    return {
        **manifest.core(),
        "url": manifest.url,
        "rolloutPercent": manifest.rollout_percent,
        "signature": manifest.signature,
        "pubkeyKid": manifest.pubkey_kid,
        "revokedAt": manifest.revoked_at,
    }


# --------------------------------------------------------------------------- 服务
class UpdateService:
    """更新链路的编排者。

    `client` 是治理面客户端（可选）：给了才有 S1 通道与灰度决策；
    `opener` 是取字节流的注入点（测试用）；
    `run_active` 返回「当前是否有任务在跑」，下载前用它挡 409。
    """

    def __init__(
        self,
        options: UpdateOptions,
        *,
        client: Any = None,
        opener: Opener | None = None,
        audit: Any = None,
        clock: Callable[[], int] | None = None,
        run_active: Callable[[], bool] | None = None,
    ) -> None:
        self.options = options
        self._client = client
        self._opener = opener
        self._audit = audit
        self._clock = clock or (lambda: int(time.time()))
        self._run_active = run_active
        self._cleaned = False
        self.current_version = options.current_version or __version__
        self.platform = options.platform or detect_platform()
        self.arch = options.arch or detect_arch()
        self._pubkey: dict[str, Any] | None = None
        self._pubkey_error = ""
        try:
            self._pubkey = load_pubkey(options.pubkey_override)
        except UpdateError as exc:
            self._pubkey_error = str(exc)

    # -------------------------------------------------------------- 内部工具
    def _collect(self) -> SourceDecision:
        fetcher = self._client.check_client_update if self._client is not None else None
        return collect(
            channel=self.options.channel,
            platform=self.platform,
            arch=self.arch,
            current=self.current_version,
            pubkey=self._pubkey,
            governance_fetcher=fetcher,
            base_url=self.options.base_url,
            opener=self._opener,
        )

    def _verdict(
        self,
        *,
        status: str,
        manifest: Manifest | None = None,
        mandatory: bool = False,
        ignored: bool = False,
        reason: str = "",
        source: str = "",
        notes: list[str] | None = None,
        state: UpdateState | None = None,
        disabled: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": status != "failed",
            "disabled": disabled,
            "update": manifest is not None,
            "status": status,
            "currentVersion": self.current_version,
            "channel": self.options.channel,
            "version": manifest.version if manifest else "",
            "mandatory": mandatory,
            "ignored": ignored,
            "reason": reason,
            "source": source,
            "notes": list(notes or []),
            "release": release_view(manifest) if manifest else None,
            "checkedAt": (state.last_check_at if state else self._clock()),
        }

    def _persist(self, state: UpdateState) -> None:
        try:
            save(self.options.updates_dir, state)
        except OSError:
            # 状态写不进去不该让一次 check 失败 —— 它只影响反降级记忆。
            pass

    def _maybe_cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            cleanup_stale_tmp(self.options.updates_dir)
        except Exception:  # noqa: BLE001 —— 卫生动作，永不冒泡
            pass

    # -------------------------------------------------------------- check
    def check(self, *, auto: bool = True) -> dict[str, Any]:
        """检查是否有更新。返回一个可直接序列化的结论字典。"""
        self._maybe_cleanup()
        if self.options.disabled:
            return self._verdict(
                status="idle", reason="自动更新已禁用", disabled=True
            )
        if auto and not self.options.check_on_start:
            return self._verdict(status="idle", reason="启动自动检查已关闭")

        state = load(self.options.updates_dir)
        state.last_check_at = self._clock()

        if self._pubkey_error:
            state.status = "failed"
            state.last_error = self._pubkey_error
            self._persist(state)
            audit = self._audit
            if audit is not None:
                audit.record("update.verify_failed", source="", kind="shape")
            return self._verdict(
                status="failed", reason=self._pubkey_error, state=state
            )

        decision = self._collect()

        # 验签失败的源逐个记审计（这是「被篡改」信号，必须留痕）。
        audit = self._audit
        if audit is not None:
            for src in decision.verify_failures:
                audit.record("update.verify_failed", source=src, kind="signature")

        if decision.hard_failure:
            state.status = "failed"
            state.available_version = ""
            state.last_error = "所有来源的清单验签均失败"
            self._persist(state)
            return self._verdict(
                status="failed", reason=state.last_error, notes=decision.notes, state=state
            )

        manifest = decision.manifest
        if manifest is None:
            state.status = "idle"
            state.available_version = ""
            state.last_error = ""
            self._persist(state)
            return self._verdict(
                status="idle",
                reason=decision.reason,
                source=decision.source,
                notes=decision.notes,
                state=state,
            )

        if not manifest.matches(platform=self.platform, arch=self.arch):
            state.status = "idle"
            state.available_version = ""
            state.last_error = ""
            self._persist(state)
            return self._verdict(
                status="idle",
                reason=f"清单版本不适配本机（{self.platform}/{self.arch}）",
                source=decision.source,
                notes=decision.notes,
                state=state,
            )

        allowed, why = allows(state, manifest)
        if not allowed:
            state.status = "idle"
            state.available_version = ""
            state.last_error = why
            self._persist(state)
            if audit is not None:
                audit.record(
                    "update.check",
                    result="rejected",
                    version=manifest.version,
                    source=decision.source,
                    seq=manifest.seq,
                )
            return self._verdict(
                status="idle", reason=why, source=decision.source, notes=decision.notes, state=state
            )

        if not is_newer(manifest.version, self.current_version):
            state.status = "idle"
            state.available_version = ""
            state.last_error = ""
            self._persist(state)
            if audit is not None:
                audit.record(
                    "update.check",
                    result="none",
                    version=manifest.version,
                    source=decision.source,
                    seq=manifest.seq,
                )
            return self._verdict(
                status="idle",
                reason=f"已是最新（v{self.current_version}）",
                source=decision.source,
                notes=decision.notes,
                state=state,
            )

        # 记住「见过这份清单」，供下次反降级比对。
        accept(state, manifest)
        mandatory = bool(manifest.mandatory) or not at_least(
            self.current_version, manifest.min_version
        )
        ignored = manifest.version == state.skipped_version and not mandatory
        state.status = "available"
        state.available_version = manifest.version
        state.last_error = ""
        self._persist(state)
        if audit is not None:
            audit.record(
                "update.check",
                result="available",
                version=manifest.version,
                source=decision.source,
                seq=manifest.seq,
            )
        return self._verdict(
            status="available",
            manifest=manifest,
            mandatory=mandatory,
            ignored=ignored,
            reason=decision.reason or "发现新版本",
            source=decision.source,
            notes=decision.notes,
            state=state,
        )

    # -------------------------------------------------------------- download
    def download(
        self,
        *,
        version: str = "",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """把目标版本下载到 staging，校验 sha256 后原子落盘。"""
        if self.options.disabled:
            raise UpdateError("自动更新已禁用（VIBE_UPDATE_DISABLE=1）", kind="state")
        if self._run_active is not None and self._run_active():
            raise UpdateError("有任务正在执行，请结束后再检查更新", kind="state")
        if self._pubkey_error:
            raise UpdateError(self._pubkey_error, kind="shape")

        state = load(self.options.updates_dir)
        decision = self._collect()
        audit = self._audit
        if audit is not None:
            for src in decision.verify_failures:
                audit.record("update.verify_failed", source=src, kind="signature")
        manifest = decision.manifest
        if manifest is None:
            if decision.hard_failure:
                raise UpdateError("所有来源的清单验签均失败，拒绝下载", kind="signature")
            raise UpdateError(
                decision.reason or "当前没有可下载的新版本", kind="no_release"
            )
        if version and manifest.version != version:
            raise UpdateError(
                f"来源当前提供的是 v{manifest.version}，与请求的 v{version} 不一致",
                kind="no_release",
            )
        if len(str(manifest.sha256 or "")) != 64:
            raise UpdateError("清单缺少有效 sha256，拒绝下载", kind="shape")
        url = decision.url or manifest.url
        if not url:
            raise UpdateError("清单没有可用的下载地址", kind="no_release")

        dest_dir = self.options.updates_dir / manifest.version
        filename = package_filename(manifest.version)
        existing = dest_dir / filename
        # 幂等复用：同版本已 staging 且 sha256 命中，直接返回。
        if existing.is_file():
            try:
                if sha256_file(existing) == manifest.sha256:
                    state.status = "staged"
                    state.staged_path = str(existing)
                    state.last_error = ""
                    self._persist(state)
                    return {
                        "ok": True,
                        "reused": True,
                        "version": manifest.version,
                        "stagingPath": str(existing),
                        "size": manifest.size,
                        "source": decision.source,
                    }
            except OSError:
                pass

        state.status = "downloading"
        state.available_version = manifest.version
        state.last_error = ""
        self._persist(state)
        if audit is not None:
            audit.record(
                "update.download",
                version=manifest.version,
                source=decision.source,
                size=int(manifest.size),
                result="started",
            )

        try:
            path = download_to_staging(
                url=url,
                dest_dir=dest_dir,
                filename=filename,
                expected_sha256=manifest.sha256,
                expected_size=int(manifest.size),
                max_bytes=self.options.max_bytes,
                opener=self._opener,
                on_progress=on_progress,
            )
        except UpdateError as exc:
            state.status = "failed"
            state.staged_path = ""
            state.last_error = str(exc)
            self._persist(state)
            if audit is not None:
                audit.record(
                    "update.download",
                    version=manifest.version,
                    source=decision.source,
                    size=int(manifest.size),
                    result="failed",
                )
                if exc.kind in ("sha256", "size", "signature"):
                    audit.record("update.verify_failed", source=decision.source, kind=exc.kind)
            raise

        state.status = "staged"
        state.staged_path = str(path)
        state.last_error = ""
        self._persist(state)
        # 保留最近 2 版安装器：新版启动即崩时，用户自救的唯一凭据是上一个版本的包。
        # 用 protect 钉住刚 staging 的目录，避免「刚下完就被自己清掉」。
        try:
            prune_staged(self.options.updates_dir, keep=2, protect=path.parent)
        except OSError:
            pass
        if audit is not None:
            audit.record(
                "update.staged",
                version=manifest.version,
                source=decision.source,
                size=int(manifest.size),
                sha256Digest=str(manifest.sha256)[:16],
            )
        return {
            "ok": True,
            "reused": False,
            "version": manifest.version,
            "stagingPath": str(path),
            "size": manifest.size,
            "source": decision.source,
        }

    # -------------------------------------------------------------- skip / status
    def skip(self, version: str) -> dict[str, Any]:
        """记下「忽略此版本」。mandatory 场景由前端拦下，不调用本方法。"""
        state = load(self.options.updates_dir)
        state.skipped_version = str(version or "")
        if state.available_version == state.skipped_version:
            state.status = "idle"
            state.available_version = ""
        self._persist(state)
        audit = self._audit
        if audit is not None:
            audit.record(
                "update.check", result="declined", version=str(version or ""), source="", seq=0
            )
        return self.status()

    def status(self) -> dict[str, Any]:
        state = load(self.options.updates_dir)
        staged = Path(state.staged_path) if state.staged_path else None
        return {
            "ok": True,
            "disabled": self.options.disabled,
            "channel": self.options.channel,
            "currentVersion": self.current_version,
            "status": state.status,
            "availableVersion": state.available_version,
            "stagedPath": state.staged_path,
            "stagedExists": bool(staged and staged.is_file()),
            "skippedVersion": state.skipped_version,
            "lastCheckAt": state.last_check_at,
            "lastError": state.last_error,
            "maxSeq": state.max_seq,
        }

    def record_applied(self, version: str) -> None:
        """壳侧确认「已更新到 vX」时调用（由 host 在启动读 marker 后触发）。"""
        state = load(self.options.updates_dir)
        state.status = "applied"
        state.available_version = ""
        state.staged_path = ""
        state.last_error = ""
        self._persist(state)
        audit = self._audit
        if audit is not None:
            audit.record("update.applied", version=str(version or ""))

    def record_apply_rejected(self, reason: str) -> None:
        """壳侧拒绝 staging 路径时调用（形态校验失败等）。"""
        audit = self._audit
        if audit is not None:
            audit.record("update.apply_rejected", reason=str(reason or "")[:120])


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_UPDATES_DIR",
    "UpdateOptions",
    "UpdateService",
    "detect_arch",
    "detect_platform",
    "release_view",
]
