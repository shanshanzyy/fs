"""更新清单的解析与验签（M9）。

清单有**两份来源、同一种形状**：治理面 `check` 响应里的 `release` 对象，
与镜像目录下的 `<channel>/manifest.json`。两者都必须能验签通过才被采信，
而验签的输入是「清单核心字段的 canonical 字节」。

**署名域里没有 `url`。** 这是刻意的：下载地址由各源自己给（治理面给托管 URL、
镜像按目录布局推导），改变 URL 不该让签名失效 —— 同一份包可以放在任何地方。
字节的真伪由「被签名的 sha256」兜住，位置的真伪不由签名负责。

`canonical()` 直接复用 `governance/protocol.py` 那一份（它与服务端
`vibe_server/hashing.py::canonical` 逐字节一致，且已被跨端测试锁住）。
**这里绝不能再写第三份序列化实现** —— 每多一份就多一个「签名验不过」的理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import UpdateError
from ..governance.protocol import canonical
from .keys import decode_signature, verify_rs256

#: 被签名的核心字段。顺序无关（canonical 会 sort_keys），但**集合必须与服务端
#: `vibe_server/releases.py::manifest_core` 完全相同**：少一个键，
#: 客户端算出的字节就与服务端签名时的字节不一样，验签必然失败。
CORE_KEYS = (
    "version",
    "channel",
    "platform",
    "arch",
    "sha256",
    "size",
    "notes",
    "mandatory",
    "minVersion",
    "seq",
)

ALLOWED_CHANNELS = ("stable", "beta", "canary")


def core_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """从任意一份清单视图里抽出被签名的核心字段（缺失项补默认值）。

    补默认值是必须的：服务端 `manifest_core` 对 `notes`/`minVersion` 用
    `row.get(...) or ""`、对 `mandatory` 用 `bool(...)`，所以镜像里的
    `manifest.json` 即使省略空字段，抽出来的核心域仍与服务端逐字节一致。
    """
    return {
        "version": str(row.get("version") or ""),
        "channel": str(row.get("channel") or ""),
        "platform": str(row.get("platform") or ""),
        "arch": str(row.get("arch") or ""),
        "sha256": str(row.get("sha256") or ""),
        "size": int(row.get("size") or 0),
        "notes": str(row.get("notes") or ""),
        "mandatory": bool(row.get("mandatory")),
        "minVersion": str(row.get("minVersion") or row.get("min_version") or ""),
        "seq": int(row.get("seq") or 0),
    }


def signing_input(row: Mapping[str, Any]) -> bytes:
    """签名/验签的输入字节。服务端 `signing_input` 是它的镜像实现。"""
    return canonical(core_of(row)).encode("utf-8")


@dataclass(frozen=True)
class Manifest:
    """一份经形状校验、但**尚未验签**的清单。

    分两步是有意的：解析与验签分开，调用方才能对「清单格式坏」与
    「签名坏」分别处理（前者通常是源给错了东西，后者才是被篡改的信号）。
    """

    version: str
    channel: str
    platform: str
    arch: str
    sha256: str
    size: int
    notes: str
    mandatory: bool
    min_version: str
    seq: int
    signature: str = ""
    pubkey_kid: str = ""
    url: str = ""
    rollout_percent: int = 100
    revoked_at: int | None = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Manifest":
        """从清单文档构造。**只做形状校验，不验签**（见 `verify`）。"""
        if not isinstance(document, Mapping):
            raise UpdateError("清单不是 JSON 对象", kind="shape")
        core = core_of(document)
        if not core["version"]:
            raise UpdateError("清单缺少 version", kind="shape")
        if core["sha256"] and len(core["sha256"]) != 64:
            raise UpdateError(
                f"清单 sha256 长度非法（{len(core['sha256'])}，应为 64）", kind="shape"
            )
        if core["size"] <= 0:
            raise UpdateError("清单 size 必须大于 0", kind="shape")
        if core["channel"] and core["channel"] not in ALLOWED_CHANNELS:
            raise UpdateError(f"清单 channel 非法: {core['channel']!r}", kind="shape")
        revoked = document.get("revokedAt")
        return cls(
            version=core["version"],
            channel=core["channel"],
            platform=core["platform"],
            arch=core["arch"],
            sha256=core["sha256"],
            size=core["size"],
            notes=core["notes"],
            mandatory=core["mandatory"],
            min_version=core["minVersion"],
            seq=core["seq"],
            signature=str(document.get("signature") or ""),
            pubkey_kid=str(document.get("pubkeyKid") or document.get("pubkey_kid") or ""),
            url=str(document.get("url") or ""),
            rollout_percent=int(document.get("rolloutPercent") or 100),
            revoked_at=int(revoked) if revoked else None,
        )

    # ------------------------------------------------------------------ 视图
    def core(self) -> dict[str, Any]:
        return core_of(
            {
                "version": self.version,
                "channel": self.channel,
                "platform": self.platform,
                "arch": self.arch,
                "sha256": self.sha256,
                "size": self.size,
                "notes": self.notes,
                "mandatory": self.mandatory,
                "minVersion": self.min_version,
                "seq": self.seq,
            }
        )

    def signing_input(self) -> bytes:
        return canonical(self.core()).encode("utf-8")

    def matches(self, *, platform: str, arch: str) -> bool:
        """平台/架构是否与本机相符。清单里留空视为「不限」（镜像场景常见）。"""
        by_platform = not self.platform or self.platform == platform
        by_arch = not self.arch or self.arch == arch
        return by_platform and by_arch

    # ------------------------------------------------------------------ 验签
    def verify(self, pubkey: dict[str, Any] | None) -> None:
        """验签，失败抛 `UpdateError(kind="signature")`。

        **没有可信公钥 = 验不过**，不是「跳过验签」：把「没配」解释成
        「不校验」，等于把信任锚降级成可选项（见 keys.py 模块头第 3 条）。
        """
        if not self.signature:
            raise UpdateError("清单没有签名 —— 无法采信", kind="signature")
        if pubkey is None:
            raise UpdateError(
                "本机没有可信的发布公钥（构建期内置为空且未设 VIBE_UPDATE_PUBKEY），"
                "无法验证清单签名",
                kind="signature",
            )
        if self.pubkey_kid and self.pubkey_kid != str(pubkey.get("kid") or ""):
            raise UpdateError(
                f"清单声明的签名者（kid={self.pubkey_kid}）与本机内置公钥"
                f"（kid={pubkey.get('kid')}）不符",
                kind="signature",
            )
        signature = decode_signature(self.signature)
        if not verify_rs256(pubkey, self.signing_input(), signature):
            raise UpdateError("清单签名验证失败 —— 该源不可信", kind="signature")


__all__ = [
    "ALLOWED_CHANNELS",
    "CORE_KEYS",
    "Manifest",
    "core_of",
    "signing_input",
]
