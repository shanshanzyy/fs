"""双通道更新源（M9）。

两条通道、同一种形状，但**信任判定逐源独立**：

  · **S1 治理面**（`governance`）：经 `GovernanceClient.check_client_update` 拿
    `{update, release, eligible, rollout, reason}`。它额外带来**灰度决策**
    （`eligible`）——这是下发通道独有的能力。
  · **S2 本地镜像**（`local`）：直接读 `<base>/<channel>/manifest.json`
    （`file://`、内网 http、裸路径一等对待）。没有服务端也能工作，
    代价是没有灰度（`eligible` 恒 True）。

**降级矩阵**（`collect` 里落实）：

  · 治理面未接入（`NotEnrolled`）→ 记 note，改走镜像；
  · 治理面不可达 / 403 / 5xx → 记 note，**继续走镜像**（可用性降级，不是信任降级）；
  · 治理面给出**合法且已验签**的答案，但 `eligible=false` → **就此打住，不再看镜像**。
    灰度是发布者的意图，绕过它去读镜像等于把灰度关掉；
  · 某源清单**验签失败** → **只作废该源**（`verify_failures`），继续下一个源。
    两源都拿到清单却都验签不过 → 硬失败（`hard_failure`），上层据此
    不通知、不下载、记 `update.verify_failed`；
  · 全部源没给出可用清单 → 「无可用更新来源」，静默不提示。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..errors import UpdateError
from ..governance.client import GovernanceError, NotEnrolled
from .download import Opener, fetch_text, package_filename
from .manifest import Manifest

GOVERNANCE = "governance"
LOCAL = "local"


# --------------------------------------------------------------------------- 结果
@dataclass
class SourceResult:
    """单个源的取回结果（**尚未验签**）。"""

    source: str
    manifest: Manifest | None = None
    eligible: bool = True
    reason: str = ""
    note: str = ""
    url: str = ""
    verified: bool = False

    def has_manifest(self) -> bool:
        return self.manifest is not None


@dataclass
class SourceDecision:
    """`collect()` 的结论：一个（至多一个）已验签、可用的候选。"""

    manifest: Manifest | None = None
    source: str = ""
    eligible: bool = True
    reason: str = ""
    url: str = ""
    notes: list[str] = field(default_factory=list)
    verify_failures: list[str] = field(default_factory=list)

    def has_update(self) -> bool:
        return self.manifest is not None

    @property
    def hard_failure(self) -> bool:
        """所有给出清单的源都验签失败 —— 这是「被篡改」的信号，不是「没更新」。"""
        return bool(self.verify_failures) and self.manifest is None


# --------------------------------------------------------------------------- 工具
def _join(base: str, *parts: str) -> str:
    """把镜像基址与路径段拼起来。基址已是 URL 或裸路径都适用。"""
    trimmed = str(base).rstrip("/")
    tail = "/".join(str(part).strip("/") for part in parts)
    return f"{trimmed}/{tail}"


def _looks_absolute(url: str) -> bool:
    """判断清单里的 `url` 是否是「自带协议/盘符」的绝对地址。

    刻意**不**把以 `/` 开头的服务端相对路径（`/api/v1/client/packages/x`）
    当成绝对地址 —— 那玩意儿只有配了治理面才能用，镜像场景下必须由基址推导。
    """
    if "://" in url:
        return True
    return len(url) > 2 and url[1] == ":" and url[2] in ("\\", "/")


def resolve_package_url(manifest: Manifest, base: str, channel: str) -> str:
    """决定这个候选的安装器下载地址。"""
    declared = str(manifest.url or "").strip()
    if declared and _looks_absolute(declared):
        return declared
    return _join(base, channel, package_filename(manifest.version))


# --------------------------------------------------------------------------- 治理面
def governance_result(
    fetcher: Callable[..., Mapping[str, Any]],
    *,
    channel: str,
    platform: str,
    arch: str,
    current: str,
) -> SourceResult:
    """从治理面取一个候选。**永不抛异常** —— 失败都折叠成 note。"""
    try:
        body = fetcher(channel=channel, platform=platform, arch=arch, current=current)
    except NotEnrolled:
        return SourceResult(source=GOVERNANCE, note="未接入治理面，改用本地镜像")
    except GovernanceError as exc:
        label = exc.code or (str(exc.status) if exc.status else "网络")
        return SourceResult(
            source=GOVERNANCE, note=f"治理面不可用（{label}）：{exc}，改用本地镜像"
        )
    except Exception as exc:  # noqa: BLE001 —— 源失败不该冒泡到流程
        return SourceResult(source=GOVERNANCE, note=f"治理面请求异常：{exc}")

    if not isinstance(body, Mapping):
        return SourceResult(source=GOVERNANCE, note="治理面响应不是 JSON 对象")
    if body.get("update") is False:
        return SourceResult(
            source=GOVERNANCE, reason=str(body.get("reason") or "治理面判定无需更新")
        )
    release = body.get("release")
    if not isinstance(release, Mapping) or not release:
        return SourceResult(
            source=GOVERNANCE, reason=str(body.get("reason") or "治理面未给出适用版本")
        )
    try:
        manifesto = Manifest.from_document(release)
    except UpdateError as exc:
        return SourceResult(source=GOVERNANCE, note=f"治理面清单格式非法：{exc}")
    return SourceResult(
        source=GOVERNANCE,
        manifest=manifesto,
        eligible=bool(body.get("eligible", True)),
        reason=str(body.get("reason") or "治理面下发新版本"),
        url=str(release.get("url") or ""),
    )


# --------------------------------------------------------------------------- 本地镜像
def local_result(
    base_url: str,
    *,
    channel: str,
    platform: str,
    arch: str,
    opener: Opener | None = None,
    timeout: int = 20,
) -> SourceResult:
    """从本地镜像目录取一个候选。**永不抛异常**。"""
    base = str(base_url or "").strip()
    if not base:
        return SourceResult(source=LOCAL, reason="未配置本地镜像基址")
    manifest_url = _join(base, channel, "manifest.json")
    try:
        text = fetch_text(manifest_url, timeout=timeout, opener=opener)
    except UpdateError as exc:
        return SourceResult(source=LOCAL, note=f"本地镜像不可读（{manifest_url}）：{exc}")
    try:
        document = json.loads(text)
    except ValueError:
        return SourceResult(source=LOCAL, note=f"本地镜像清单不是合法 JSON：{manifest_url}")
    try:
        manifesto = Manifest.from_document(document)
    except UpdateError as exc:
        return SourceResult(source=LOCAL, note=f"本地镜像清单格式非法：{exc}")
    if manifesto.revoked_at:
        return SourceResult(source=LOCAL, reason="本地镜像的该版本已被撤销")
    if not manifesto.matches(platform=platform, arch=arch):
        return SourceResult(
            source=LOCAL, reason=f"本地镜像版本不适配本机（{platform}/{arch}）"
        )
    return SourceResult(
        source=LOCAL,
        manifest=manifesto,
        eligible=True,
        reason="本地镜像提供新版本",
        url=resolve_package_url(manifesto, base, channel),
    )


# --------------------------------------------------------------------------- 验签
def verify(result: SourceResult, pubkey: dict[str, Any] | None) -> None:
    """逐源独立验签。失败抛 `UpdateError(kind="signature")`（调用方只作废该源）。"""
    if result.manifest is None:
        return
    result.manifest.verify(pubkey)
    result.verified = True


# --------------------------------------------------------------------------- 编排
def collect(
    *,
    channel: str,
    platform: str,
    arch: str,
    current: str,
    pubkey: dict[str, Any] | None,
    governance_fetcher: Callable[..., Mapping[str, Any]] | None = None,
    base_url: str = "",
    opener: Opener | None = None,
    timeout: int = 20,
) -> SourceDecision:
    """按序尝试各源，返回第一个「已验签且可用」的候选。"""
    notes: list[str] = []
    failures: list[str] = []

    candidates: list[SourceResult] = []
    if governance_fetcher is not None:
        candidates.append(
            governance_result(
                governance_fetcher, channel=channel, platform=platform, arch=arch, current=current
            )
        )
    if str(base_url or "").strip():
        candidates.append(
            local_result(
                base_url, channel=channel, platform=platform, arch=arch, opener=opener, timeout=timeout
            )
        )

    for cand in candidates:
        if cand.manifest is None:
            if cand.note:
                notes.append(cand.note)
            continue
        try:
            verify(cand, pubkey)
        except UpdateError as exc:
            failures.append(cand.source)
            notes.append(f"{cand.source} 清单验签失败：{exc}")
            continue
        if not cand.eligible:
            # 已验证签、但发布者说不该给它 —— 这是权威答案，不再看下一个源。
            return SourceDecision(
                manifest=None,
                source=cand.source,
                eligible=False,
                reason=cand.reason or "灰度未命中",
                notes=notes,
                verify_failures=failures,
            )
        return SourceDecision(
            manifest=cand.manifest,
            source=cand.source,
            eligible=True,
            reason=cand.reason,
            url=cand.url,
            notes=notes,
            verify_failures=failures,
        )

    return SourceDecision(
        manifest=None,
        reason="无可用的更新来源",
        notes=notes,
        verify_failures=failures,
    )


__all__ = [
    "GOVERNANCE",
    "LOCAL",
    "SourceDecision",
    "SourceResult",
    "collect",
    "governance_result",
    "local_result",
    "resolve_package_url",
    "verify",
]
