"""内置发布公钥与 RS256 验签（M9）。

**这是整个更新链路的信任锚，也是本模块唯一不允许出错的数学。**

客户端从「治理面」或「内网镜像」拿到一份清单，清单里带 `sha256` 与
一份 RS256 签名。这两样东西的作用完全不同，不能混谈：

  · `sha256` 只是**传输完整性**的第二道锁 —— 能改清单的人同样能改 sha256，
    所以它挡不住「有人替换了镜像」，只能挡「文件传坏了」；
  · `signature` 才是**信任锚** —— 只有持有发布私钥的人能签出它，
    而私钥只存在于治理面主机的 `<DATA_DIR>/release-signing.key` 里。

于是「镜像不可信」成为可接受的威胁模型：镜像被换掉、被中间人篡改都无所谓，
验签过不去就作废那个源。这正是 ADR-030 的核心。

公钥来源有三条，优先级从高到低：

  1. `VIBE_UPDATE_PUBKEY`（显式覆盖）—— 自签内网镜像场景，需显式设置并记审计；
  2. `BUILTIN_PUBKEY`（构建期内置）—— 发行版由打包脚本写入本常量；
  3. 都没有 → **不具备可信公钥**，验签一律失败（fail-closed）。

第 3 条是刻意的：没有公钥时「验不过」而不是「跳过验签」。
把「没配」解释成「不校验」，等于把整个信任锚改成了可选项。
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from ..errors import UpdateError

# PKCS#1 v1.5 的 DigestInfo 前缀（SHA-256），RFC 8017 §9.2。
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

#: 允许的最小模数位数。与服务端 `release_keys.MIN_MODULUS_BITS` 一致。
MIN_MODULUS_BITS = 2048

#: 只接受这些签名算法。
ALLOWED_ALGS = frozenset({"RS256"})

#: **构建期内置公钥**。默认 None（未填 = 验签必然失败，见模块头第 3 条）。
#:
#: 发布流程：先在治理面主机跑 `gen_release_key.py` 生成 `release-signing.key`，
#: 再把它的公钥写进这里（打包脚本 `build_desktop.py --release-key` 负责写入），
#: 最后构建客户端。仓库里**不放任何真实密钥**，因此源码树上的这一项恒为 None；
#: 它只在发行版构建产物里被填上。
BUILTIN_PUBKEY: dict[str, Any] | None = None


# --------------------------------------------------------------------------- 编码
def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise UpdateError(f"公钥里的 base64url 字段非法: {str(text)[:16]!r}…", kind="shape") from exc


def _b64url_to_int(text: str) -> int:
    return int.from_bytes(_unb64url(text), "big")


def pubkey_kid(n: int, e: int) -> str:
    """公钥指纹（kid）。

    计算方式必须与服务端 `vibe_server/release_keys.py::pubkey_kid` **逐字节一致** ——
    两边都要能从 `{n,e}` 算出同一个 kid，否则客户端内置公钥的 kid 与清单里
    声明的 `pubkeyKid` 对不上，验签会以「未知 kid」失败。改这里就要同步改那边。
    """
    raw = (
        n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
        + e.to_bytes((e.bit_length() + 7) // 8 or 1, "big")
    )
    return hashlib.sha256(b"RSA-RS256\x00" + raw).hexdigest()[:16]


# --------------------------------------------------------------------------- 公钥
def parse_pubkey(document: Any) -> dict[str, Any]:
    """校验并归一一份公钥文档，返回 `{kty,n,e,kid,alg}`。

    校验口径与服务端 `_validate` 一致：**模数不足 2048 位一律拒**。
    1024 位 RSA 今天可被分解，用它当信任锚等于没有信任锚。
    """
    if not isinstance(document, dict) or document.get("kty") != "RSA":
        raise UpdateError("公钥不是 RSA 结构（缺 kty=RSA）", kind="shape")
    try:
        n = _b64url_to_int(str(document["n"]))
        e = _b64url_to_int(str(document["e"]))
    except (KeyError, UpdateError) as exc:
        raise UpdateError("公钥缺少 n / e 字段", kind="shape") from exc
    if n.bit_length() < MIN_MODULUS_BITS:
        raise UpdateError(
            f"公钥模数仅 {n.bit_length()} 位（要求 ≥ {MIN_MODULUS_BITS}）", kind="shape"
        )
    if e < 3:
        raise UpdateError(f"公钥公开指数非法（e={e}）", kind="shape")
    kid = pubkey_kid(n, e)
    declared = str(document.get("kid") or "")
    if declared and declared != kid:
        raise UpdateError(
            f"公钥 kid 与 {n.bit_length()} 位模数实算不符（声明 {declared!r}，实算 {kid!r}）",
            kind="shape",
        )
    alg = str(document.get("alg") or "RS256")
    if alg not in ALLOWED_ALGS:
        raise UpdateError(f"不支持的签名算法: {alg!r}", kind="shape")
    return {"kty": "RSA", "n": document["n"], "e": document["e"], "kid": kid, "alg": alg}


def decode_signature(text: str) -> bytes:
    """把清单里的 base64url 签名还原成字节（补回被去掉的 `=` 填充）。"""
    return _unb64url(text)


def verify_rs256(public: dict[str, Any], signing_input: bytes, signature: bytes) -> bool:
    """用公钥视图验签（PKCS#1 v1.5 + SHA-256）。

    数学与服务端 `release_keys.verify_rs256` / `auth/oidc.py::_Jwk.verify_rs256`
    完全一致：`pow(sig, e, n)` 还原填充后的 DigestInfo 再逐字节比对。
    **任何一步不满足都返回 False，不抛异常** —— 验签是安全判定，
    不该靠异常控制流，也不该把内部细节泄漏到日志里。
    """
    try:
        n = _b64url_to_int(str(public["n"]))
        e = _b64url_to_int(str(public["e"]))
    except (KeyError, UpdateError):
        return False
    key_size = (n.bit_length() + 7) // 8
    if len(signature) != key_size:
        return False
    try:
        value = pow(int.from_bytes(signature, "big"), e, n)
        encoded = value.to_bytes(key_size, "big")
    except (ValueError, OverflowError):
        return False
    if not encoded.startswith(b"\x00\x01"):
        return False
    separator = encoded.find(b"\x00", 2)
    if separator < 0 or separator - 2 < 8:
        return False
    if encoded[2:separator] != b"\xff" * (separator - 2):
        return False
    digest = hashlib.sha256(signing_input).digest()
    return encoded[separator + 1 :] == _SHA256_DIGEST_INFO + digest


# --------------------------------------------------------------------------- 选择
def load_pubkey(override: str = "") -> dict[str, Any] | None:
    """按优先级选出可信公钥。返回 None 表示**没有可信公钥**（验签必然失败）。

    `override`（来自 `VIBE_UPDATE_PUBKEY`）支持两种写法：
      · JSON 文本：`{"kty":"RSA","n":"…","e":"AQAB"}`；
      · 文件路径：指向一份同结构的 JSON 文件。
    """
    text = str(override or "").strip()
    if text:
        document: Any = None
        if text.startswith("{"):
            try:
                document = json.loads(text)
            except ValueError as exc:
                raise UpdateError(
                    "VIBE_UPDATE_PUBKEY 不是合法 JSON，也不是可读文件路径", kind="shape"
                ) from exc
        else:
            path = Path(text).expanduser()
            if not path.is_file():
                raise UpdateError(
                    f"VIBE_UPDATE_PUBKEY 指向的文件不存在：{path}", kind="shape"
                )
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UpdateError(f"VIBE_UPDATE_PUBKEY 文件不可读或不是 JSON：{path}", kind="shape") from exc
        return parse_pubkey(document)
    if BUILTIN_PUBKEY is not None:
        return parse_pubkey(BUILTIN_PUBKEY)
    return None


__all__ = [
    "ALLOWED_ALGS",
    "BUILTIN_PUBKEY",
    "MIN_MODULUS_BITS",
    "decode_signature",
    "load_pubkey",
    "parse_pubkey",
    "pubkey_kid",
    "verify_rs256",
]
