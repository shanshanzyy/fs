"""客户端发布与检查（M9）。

治理面这一侧只做三件事：**存元数据、代签清单、判定灰度**。
不碰任何用户代码 / 提示词 / 模型输出（ADR-006）。

信任链只有一条：发布者用 `<DATA_DIR>/release-signing.key` 对「清单核心字段」
做 RS256 签名，客户端内置对应公钥验签（ADR-030）。`sha256` 只是「传输完整性」
的第二道锁 —— 它不在信任锚上，能改 sha256 的人必须同时能伪造签名才会生效。

署名域（`manifest_core`）**不含 `url`**：下载地址由各源自己给（治理面给托管 URL、
镜像按目录布局推导），字节的真伪由「被签名的 sha256」兜住。这样 URL 可以随
部署环境自由变动，而不影响签名。

**版本比较刻意在此处另写一份**（内核 `update/version.py` 也有一份）：
治理面与内核是两个零依赖包，`vibe_server` 不能 import `agent_core`。
两份实现口径写死在这里，改一处必须同步改另一处，测试里做交叉用例。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .errors import BadRequest, Forbidden, NotFound
from .features import bucket_of
from .hashing import canonical
from .release_keys import ReleaseKey

#: 支持的更新通道。
CHANNELS = ("stable", "beta", "canary")

#: 平台 / 架构白名单。今天只有 Windows 走到，字段留着备扩展（ADR-030 明确不做 mac/linux）。
PLATFORMS = ("windows", "darwin", "linux")
ARCHES = ("x64", "arm64")

#: 单个安装包体积上限（防呆，不是安全边界）。
MAX_PACKAGE_BYTES = 1024 ** 3

#: url 允许的协议前缀；相对路径（以 `/` 开头）用于治理面自托管。
_URL_PREFIXES = ("http://", "https://", "file://", "/")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# --------------------------------------------------------------------------- 版本
def parse_version(text: str) -> tuple[int, ...] | None:
    """解析 `[v]X[.Y[.Z[.W]]]` 形式的版本号；不合法返回 None。

    前导零（`1.02`）按数值比，`v` 前缀容忍；多于 4 段或出现非数字段一律拒绝 ——
    宁可拒绝一个奇怪的版本号，也不要让「比较结果」变成猜出来的。
    """
    raw = str(text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    if not raw or len(raw) > 40:
        return None
    parts = raw.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part.isascii() or not part.isdigit() or len(part) > 9:
            return None
        numbers.append(int(part))
    return tuple(numbers)


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """逐段比较，缺位按 0 补（`1.2` 等于 `1.2.0`）。"""
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else 0
        b = right[index] if index < len(right) else 0
        if a != b:
            return 1 if a > b else -1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    """`candidate` 是否严格新于 `current`。

    `current` 解析不出来时**返回 False**（不给更新）。反向失败会让「版本号写错」
    在客户端表现为「悄悄装了一个更旧的包」，代价远高于「这次不提示」。
    """
    new = parse_version(candidate)
    old = parse_version(current)
    if new is None or old is None:
        return False
    return _compare(new, old) > 0


def at_least(current: str, floor: str) -> bool:
    """`current >= floor`。任一侧解析失败时返回 True（不触发强制升级）。"""
    a = parse_version(current)
    b = parse_version(floor)
    if a is None or b is None:
        return True
    return _compare(a, b) >= 0


# --------------------------------------------------------------------------- 署名域
def manifest_core(row: dict[str, Any]) -> dict[str, Any]:
    """被签名的清单核心字段。

    与内核 `update/manifest.py` 的字段名**逐字节对齐**；`canonical()` 的
    排序/分隔符/非 ASCII 口径两端也必须一致（hashing.canonical 是共用口径）。
    """
    return {
        "version": str(row["version"]),
        "channel": str(row["channel"]),
        "platform": str(row["platform"]),
        "arch": str(row["arch"]),
        "sha256": str(row["sha256"]),
        "size": int(row["size"]),
        "notes": str(row.get("notes") or ""),
        "mandatory": bool(row.get("mandatory")),
        "minVersion": str(row.get("min_version") or ""),
        "seq": int(row["seq"]),
    }


def signing_input(row: dict[str, Any]) -> bytes:
    return canonical(manifest_core(row)).encode("utf-8")


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """给接口/控制台用的视图（含签名，**绝不含私钥**）。"""
    view = manifest_core(row)
    view.update(
        {
            "id": str(row.get("id") or ""),
            "url": str(row.get("url") or ""),
            "rolloutPercent": int(row.get("rollout_percent") or 0),
            "signature": str(row.get("signature") or ""),
            "signatureAlg": str(row.get("signature_alg") or "RS256"),
            "pubkeyKid": str(row.get("pubkey_kid") or ""),
            "publishedAt": int(row.get("published_at") or 0),
            "revokedAt": int(row["revoked_at"]) if row.get("revoked_at") else None,
            "createdBy": str(row.get("created_by") or ""),
        }
    )
    return view


# --------------------------------------------------------------------------- 字段校验
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BadRequest(message)


def validate_release_fields(
    *,
    version: str,
    channel: str,
    platform: str,
    arch: str,
    url: str,
    sha256: str,
    size: int,
    mandatory: bool,
    min_version: str,
    rollout_percent: int,
    notes: str,
) -> dict[str, Any]:
    """逐字段校验并归一（小写 sha256 / 收紧形态）。非法一律 400。"""
    _require(parse_version(version) is not None, "version 形态非法（示例 1.2.3）")
    _require(channel in CHANNELS, f"channel 必须是 {'/'.join(CHANNELS)} 之一")
    _require(platform in PLATFORMS, f"platform 必须是 {'/'.join(PLATFORMS)} 之一")
    _require(arch in ARCHES, f"arch 必须是 {'/'.join(ARCHES)} 之一")
    if url:
        _require(url.startswith(_URL_PREFIXES), "url 必须是 http(s):// / file:// 或 / 开头的相对路径")
    normalized_sha = sha256.strip().lower()
    _require(bool(_HEX64.match(normalized_sha)), "sha256 必须是 64 位小写十六进制")
    _require(0 < int(size) <= MAX_PACKAGE_BYTES, f"size 必须在 1..{MAX_PACKAGE_BYTES} 之间")
    if min_version:
        _require(parse_version(min_version) is not None, "minVersion 形态非法")
    _require(0 <= int(rollout_percent) <= 100, "rolloutPercent 必须在 0..100 之间")
    return {
        "version": version.strip(),
        "channel": channel,
        "platform": platform,
        "arch": arch,
        "url": url.strip(),
        "sha256": normalized_sha,
        "size": int(size),
        "mandatory": bool(mandatory),
        "min_version": min_version.strip(),
        "rollout_percent": int(rollout_percent),
        "notes": notes,
    }


# --------------------------------------------------------------------------- 服务
class ReleaseService:
    """发布 / 列表 / 撤销 / 检查。所有签名动作都收在这里，别处不许自己拼签名域。"""

    def __init__(
        self,
        *,
        store: Any,
        clock: Any,
        key: ReleaseKey | None = None,
        key_path: str | Path | None = None,
        key_generated: bool = False,
    ) -> None:
        self.store = store
        self.clock = clock
        self._key = key
        self._key_path = Path(key_path).expanduser() if key_path is not None else None
        self.key_generated = key_generated

    @property
    def key(self) -> ReleaseKey:
        """签名密钥**按需加载**。

        不在这里的 `__init__` 里生成，是为了避免「只是构建一次 app」就付一次
        2048 位 keygen（~0.3s）的代价 —— 测试套件会构建很多次 app。
        真正需要私钥的只有 publish 与 public_key 两条路径。
        """
        if self._key is None:
            if self._key_path is None:
                raise RuntimeError("ReleaseService 既没有 key 也没有 key_path，无法签名")
            from .release_keys import load_or_create

            key, generated = load_or_create(self._key_path)
            self._key = key
            self.key_generated = generated
        return self._key

    # ------------------------------------------------------------------ 查询
    def list(
        self,
        *,
        channel: str = "",
        platform: str = "",
        arch: str = "",
        include_revoked: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if channel:
            _require(channel in CHANNELS, "channel 取值非法")
        if platform:
            _require(platform in PLATFORMS, "platform 取值非法")
        if arch:
            _require(arch in ARCHES, "arch 取值非法")
        rows = self.store.list_releases(
            channel=channel,
            platform=platform,
            arch=arch,
            include_revoked=include_revoked,
            limit=max(1, min(int(limit), 500)),
        )
        return [public_view(row) for row in rows]

    def get(self, release_id: str) -> dict[str, Any]:
        row = self.store.get_release(release_id)
        if row is None:
            raise NotFound(f"发布记录不存在：{release_id}")
        return public_view(row)

    # ------------------------------------------------------------------ 发布
    def publish(
        self,
        *,
        version: str,
        channel: str,
        sha256: str,
        size: int,
        platform: str = "windows",
        arch: str = "x64",
        url: str = "",
        notes: str = "",
        mandatory: bool = False,
        min_version: str = "",
        rollout_percent: int = 100,
        created_by: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        from .ids import new_id

        fields = validate_release_fields(
            version=version,
            channel=channel,
            platform=platform,
            arch=arch,
            url=url,
            sha256=sha256,
            size=size,
            mandatory=mandatory,
            min_version=min_version,
            rollout_percent=rollout_percent,
            notes=notes,
        )
        stamp = int(now if now is not None else self.clock.now())
        # 先取 seq 再签名：seq 在署名域内，必须一次定死。
        seq = int(self.store.next_release_seq())
        release_id = new_id("rel")
        draft = {**fields, "id": release_id, "seq": seq, "created_by": created_by}
        signature = self.key.sign(signing_input(draft))
        row = self.store.insert_release(
            release_id,
            channel=fields["channel"],
            version=fields["version"],
            platform=fields["platform"],
            arch=fields["arch"],
            url=fields["url"],
            sha256=fields["sha256"],
            size=fields["size"],
            notes=fields["notes"],
            mandatory=fields["mandatory"],
            min_version=fields["min_version"],
            rollout_percent=fields["rollout_percent"],
            seq=seq,
            signature=base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            signature_alg="RS256",
            pubkey_kid=self.key.kid,
            created_by=created_by,
            now=stamp,
        )
        return public_view(row)

    # ------------------------------------------------------------------ 撤销
    def revoke(self, release_id: str, *, now: int | None = None) -> bool:
        stamp = int(now if now is not None else self.clock.now())
        return bool(self.store.revoke_release(release_id, stamp))

    # ------------------------------------------------------------------ 检查
    def check(
        self,
        *,
        channel: str,
        platform: str,
        arch: str,
        current: str,
        device_key: str = "",
    ) -> dict[str, Any]:
        """决策链：选同通道最新未撤销 release → 版本比对 → 灰度分桶 → 强制标记。"""
        _require(channel in CHANNELS, f"channel 必须是 {'/'.join(CHANNELS)} 之一")
        _require(platform in PLATFORMS, f"platform 必须是 {'/'.join(PLATFORMS)} 之一")
        _require(arch in ARCHES, f"arch 必须是 {'/'.join(ARCHES)} 之一")

        now = int(self.clock.now())
        row = self.store.latest_release(
            channel=channel, platform=platform, arch=arch, now=now
        )
        if row is None:
            return {"update": False, "eligible": False, "reason": "no_release", "release": None}

        view = public_view(row)
        if not is_newer(str(row["version"]), current):
            return {
                "update": False,
                "eligible": False,
                "reason": "up_to_date",
                "release": view,
            }

        percent = int(row.get("rollout_percent") or 0)
        if percent >= 100:
            matched = True
        elif percent <= 0 or not device_key:
            # 没有稳定标识就分不了桶。宁可不动 —— 反过来是「身份缺失即全量」。
            matched = False
        else:
            matched = bucket_of("update." + str(row["id"]), device_key) < percent

        min_version = str(row.get("min_version") or "")
        required = bool(row.get("mandatory")) or (
            bool(min_version) and not at_least(current, min_version)
        )
        return {
            # `update` 是**生效结果**：既有新版本、又通过灰度，客户端才会提示。
            # 分开回 `eligible` 只是为了让控制台与排查能看清是哪一步拦下的。
            "update": matched,
            "eligible": matched,
            "reason": "available" if matched else "not_in_rollout",
            "mandatory": required,
            "release": view,
            "rollout": {"percent": percent, "matched": matched},
        }

    # ------------------------------------------------------------------ 公钥
    def public_key(self) -> dict[str, Any]:
        """内置公钥视图。**公开无妨** —— 它只能验签，不能签名。"""
        return self.key.public_jwk()


# --------------------------------------------------------------------------- 自托管包
def packages_dir(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser() / "packages"


def safe_package_name(name: str) -> str:
    """安装器文件名白名单。穿越防护第一道，与 `static._serve_asset` 同口径。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise Forbidden("非法包名")
    if not _SAFE_NAME.match(name):
        raise Forbidden("包名只允许字母数字与 . _ -")
    return name


__all__ = [
    "ARCHES",
    "CHANNELS",
    "MAX_PACKAGE_BYTES",
    "PLATFORMS",
    "ReleaseService",
    "at_least",
    "is_newer",
    "manifest_core",
    "packages_dir",
    "parse_version",
    "public_view",
    "safe_package_name",
    "signing_input",
    "validate_release_fields",
]
