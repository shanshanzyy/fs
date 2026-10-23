"""发布签名密钥（M9）。

这是治理面签发「客户端发布清单」用的私钥：**整个更新链路的唯一信任锚**。
客户端内置对应公钥，任何未通过验签的清单都不被采信（ADR-030）。

三条纪律，不是可选的美德：

1. **私钥永不入库、永不进 HTTP 请求体、永不返回给任何接口。**
   服务端只在进程内读它来签名；对外回的是签名结果（签名本身可在客户端公开校验）。
   一旦私钥泄了，任何人就能伪造「任意版本」的更新 —— 这是本功能最高危的一条。
2. **纯标准库。** 服务端运行期依赖为零（同 httpd / oidc 的取舍），
   RSA 的 keygen / sign 只用 `pow` 与 Miller-Rabin，与 `auth/oidc.py` 的验签数学同口径。
3. **模数不足 2048 位一律拒绝**（加载与生成都拒）。1024 位 RSA 今天可被分解，
   用它签名等于给伪造更新留了门；服务端的 OIDC 验签也同样拒收小模数。

`auth/oidc.py` 里只有验签侧数学在生产代码，签名/keygen 侧此前只在测试夹具里存在
（`tests/test_oidc.py`）。本模块把那一小份数学**落到生产代码**，供发布链路使用；
客户端那侧只需要验签（见 agent_core/update/keys.py）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

# PKCS#1 v1.5 的 DigestInfo 前缀（SHA-256），RFC 8017 §9.2。
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

#: 允许的最小模数位数。低于它一律拒绝（加载与生成）。
MIN_MODULUS_BITS = 2048

_PUBLIC_EXPONENT = 65537

#: 只接受这些算法。RS256 是事实标准；ES256 需要椭圆曲线实现，暂不支持。
ALLOWED_ALGS = frozenset({"RS256"})

_SMALL_PRIMES = (
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79,
    83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151, 157, 163, 167,
    173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233, 239, 241, 251,
)

#: 密钥文件名（与 jwt.key 同目录，见 config.data_dir）。
KEY_FILENAME = "release-signing.key"


# --------------------------------------------------------------------------- 编码
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"发布签名密钥里的 base64url 字段非法: {text[:16]!r}…") from exc


def _int_to_b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return _b64url(raw)


def _b64url_to_int(text: str) -> int:
    return int.from_bytes(_unb64url(text), "big")


def pubkey_kid(n: int, e: int) -> str:
    """公钥指纹（kid）。

    计算方式必须与客户端 `agent_core/update/keys.py` **逐字节一致** ——
    两边都要能从 `{n,e}` 算出同一个 kid，否则客户端内置公钥的 kid 与
    清单里声明的 kid 对不上，验签会以「未知 kid」失败。改这里就要同步改那边。
    """
    raw = (
        n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
        + e.to_bytes((e.bit_length() + 7) // 8 or 1, "big")
    )
    return hashlib.sha256(b"RSA-RS256\x00" + raw).hexdigest()[:16]


# --------------------------------------------------------------------------- 密钥
@dataclass(frozen=True)
class ReleaseKey:
    """一对 RSA 签名密钥。`d` 是私钥指数，**绝不外泄**。"""

    n: int
    e: int
    d: int
    kid: str

    @property
    def key_size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def public_jwk(self) -> dict[str, Any]:
        """对外可公开的公钥视图（不含 d）。"""
        return {
            "kty": "RSA",
            "n": _int_to_b64url(self.n),
            "e": _int_to_b64url(self.e),
            "kid": self.kid,
            "alg": "RS256",
        }

    def sign(self, signing_input: bytes) -> bytes:
        """RS256 签名（PKCS#1 v1.5 + SHA-256）。"""
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
        padding = self.key_size - len(digest_info) - 3
        if padding < 8:
            raise ConfigError("发布签名密钥过短，装不下 PKCS#1 v1.5 填充")
        encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
        return pow(int.from_bytes(encoded, "big"), self.d, self.n).to_bytes(
            self.key_size, "big"
        )

    def _document(self) -> dict[str, Any]:
        return {
            "kty": "RSA",
            "n": _int_to_b64url(self.n),
            "e": _int_to_b64url(self.e),
            "d": _int_to_b64url(self.d),
            "kid": self.kid,
        }

    def to_json(self) -> str:
        return json.dumps(self._document(), ensure_ascii=False, sort_keys=True)


def verify_rs256(public: dict[str, Any], signing_input: bytes, signature: bytes) -> bool:
    """用公钥视图验签（服务端只在自检时用到；客户端那侧有同口径实现）。

    数学与 `auth/oidc.py::_Jwk.verify_rs256` 完全一致：`pow(sig, e, n)` 还原
    填充后的 DigestInfo 再逐字节比对。
    """
    try:
        n = _b64url_to_int(str(public["n"]))
        e = _b64url_to_int(str(public["e"]))
    except (KeyError, ConfigError):
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


# --------------------------------------------------------------------------- 生成
def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    """Miller-Rabin。先用小素数试除，绝大多数候选在这一步就被否掉。"""
    if n < 2:
        return False
    for prime in _SMALL_PRIMES:
        if n % prime == 0:
            return n == prime
    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        witness = 2 + secrets.randbelow(n - 3)
        value = pow(witness, d, n)
        if value in (1, n - 1):
            continue
        for _ in range(s - 1):
            value = value * value % n
            if value == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, exponent: int, *, exclude: int = 0) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if candidate == exclude:
            continue
        if (candidate - 1) % exponent == 0:
            continue  # e 与 p-1 不互素，直接跳过，省一次模逆失败
        if _is_probable_prime(candidate):
            return candidate


def generate() -> ReleaseKey:
    """生成一对 2048 位 RSA 密钥（纯标准库）。"""
    e = _PUBLIC_EXPONENT
    half = MIN_MODULUS_BITS // 2
    while True:
        p = _prime(half, e)
        q = _prime(half, e, exclude=p)
        n = p * q
        # 两个 1024 位素数之积有一半概率是 2047 位，而我们要的正是 2048 位。
        if n.bit_length() == MIN_MODULUS_BITS:
            break
    lam = (p - 1) * (q - 1) // math.gcd(p - 1, q - 1)
    d = pow(e, -1, lam)
    return ReleaseKey(n=n, e=e, d=d, kid=pubkey_kid(n, e))


# --------------------------------------------------------------------------- 校验
def _validate(key: ReleaseKey, *, source: str) -> None:
    if key.n.bit_length() < MIN_MODULUS_BITS:
        raise ConfigError(
            f"发布签名密钥模数仅 {key.n.bit_length()} 位（要求 ≥ {MIN_MODULUS_BITS}）：{source}。"
            "1024 位 RSA 可被分解，用它签名等于给伪造更新留门。"
        )
    if key.e < 3:
        raise ConfigError(f"发布签名密钥的公开指数非法（e={key.e}）：{source}")
    expect = pubkey_kid(key.n, key.e)
    if key.kid != expect:
        raise ConfigError(
            f"发布签名密钥的 kid 与公钥不符（文件里 {key.kid!r}，实算 {expect!r}）：{source}"
        )


def _selfcheck(key: ReleaseKey, *, source: str) -> None:
    """自签自验：不通过说明密钥对本身就是坏的（d 与 n 配不上）。"""
    probe = b"vibe-release-key-selfcheck"
    if not verify_rs256(key.public_jwk(), probe, key.sign(probe)):
        raise ConfigError(f"发布签名密钥自检失败（私钥与公钥不匹配）：{source}")


def _from_document(document: Any, *, source: str) -> ReleaseKey:
    if not isinstance(document, dict) or document.get("kty") != "RSA":
        raise ConfigError(f"发布签名密钥不是 RSA 结构：{source}")
    try:
        n = _b64url_to_int(str(document["n"]))
        e = _b64url_to_int(str(document["e"]))
        d = _b64url_to_int(str(document["d"]))
    except KeyError as exc:
        raise ConfigError(f"发布签名密钥缺少字段 {exc}：{source}") from exc
    kid = str(document.get("kid") or "")
    return ReleaseKey(n=n, e=e, d=d, kid=kid or pubkey_kid(n, e))


# --------------------------------------------------------------------------- 读写
def _restrict(path: Path) -> None:
    """尽最大努力把文件权限收紧到仅属主可读写。

    Windows 上 `chmod` 基本无效（ACL 才是真的），所以这里只是「尽力」，
    真正可靠的隔离靠目录权限 + 服务账号（README 已写明，同 jwt.key）。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def default_key_path(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser() / KEY_FILENAME


def load_or_create(path: str | Path, *, create: bool = True) -> tuple[ReleaseKey, bool]:
    """加载发布签名密钥；不存在时（`create=True`）生成一份。

    返回 `(key, generated)`。`generated=True` 说明这是**本次新生成**的密钥 ——
    调用方应据此打印警告：新生成的密钥与客户端内置公钥**必然对不上**，
    除非这一对本来就是发布者自己的密钥。
    """
    target = Path(path).expanduser()
    if target.exists():
        raw = target.read_text(encoding="utf-8")
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"发布签名密钥文件不是合法 JSON：{target}") from exc
        key = _from_document(document, source=str(target))
        _validate(key, source=str(target))
        _selfcheck(key, source=str(target))
        return key, False

    if not create:
        raise ConfigError(
            f"发布签名密钥不存在：{target}（先运行 packages/agent-core/scripts/gen_release_key.py 生成）"
        )

    key = generate()
    _validate(key, source="(新生成)")
    target.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：同目录临时文件 + os.replace，避免写到一半断电留下坏密钥文件。
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(key.to_json(), encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, target)
    _restrict(target)
    return key, True


__all__ = [
    "ALLOWED_ALGS",
    "KEY_FILENAME",
    "MIN_MODULUS_BITS",
    "ReleaseKey",
    "default_key_path",
    "generate",
    "load_or_create",
    "pubkey_kid",
    "verify_rs256",
]
