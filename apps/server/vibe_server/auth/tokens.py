"""令牌签发与校验（JWT，HS256）。

为什么自己实现 JWT 而不是装 PyJWT：这个服务要在内网离线环境跑，
第三方依赖是部署成本；而 HS256 的 JWS 结构本身只有
`base64url(header).base64url(payload).base64url(HMAC-SHA256(...))` 三段，
用标准库实现反而比引入依赖更可控。

**安全上必须做对的三件事**（少任何一件，JWT 就是个假锁）：

1. **固定算法，不看 header 里的 `alg`。** 允许客户端指定算法就会引入
   `alg: none` 伪造、以及 RS256→HS256 混淆攻击。我们只认 HS256，
   header 里写了别的直接拒。
2. **校验 `exp` / `nbf` / `iat`，且校验 `iss` / `aud` / `typ`。**
   不校验 aud 的令牌可以被「给 A 服务签的令牌」拿去访问 B 服务。
3. **带 `jti` 并支持吊销。** 访问令牌虽短，但「刚发现泄漏 → 等 30 分钟
   自然过期」是不可接受的，需要有立即作废的能力。

刷新令牌**不是 JWT**，而是随机串 —— 它只需要「不可预测 + 能查表」，
做成 JWT 反而会诱使人不去查表，从而做不出「重用检测」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from ..errors import Unauthenticated

ALGORITHM = "HS256"
TOKEN_TYPE = "access"

# 时钟偏移容忍：客户端与服务端差几秒是常态，不该因此登录失败
LEEWAY = 60


# --------------------------------------------------------------------------- 编码
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise Unauthenticated("令牌编码非法") from exc


def _sign(signing_input: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def encode(claims: dict[str, Any], secret: bytes) -> str:
    header = {"alg": ALGORITHM, "typ": "JWT"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    segments.append(_b64url(_sign(signing_input, secret)))
    return ".".join(segments)


def decode_unverified(token: str) -> dict[str, Any]:
    """只解析不校验。**仅供诊断**（例如取 jti 去查吊销表）。

    名字故意起得刺眼：任何拿它当「认证通过」用的代码都是 bug。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthenticated("令牌结构非法")
    try:
        payload = json.loads(_unb64url(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unauthenticated("令牌载荷非法") from exc
    if not isinstance(payload, dict):
        raise Unauthenticated("令牌载荷不是对象")
    return payload


def peek_jti(token: str) -> str:
    """从（可能已过期的）令牌里取 jti，用于登出时精确吊销。"""
    try:
        return str(decode_unverified(token).get("jti") or "")
    except Unauthenticated:
        return ""


# --------------------------------------------------------------------------- 声明
@dataclass(frozen=True)
class TokenClaims:
    subject: str
    jti: str
    issuer: str
    audience: str
    issued_at: int
    expires_at: int
    device_id: str
    email: str
    roles: tuple[str, ...]
    raw: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "jti": self.jti,
            "iss": self.issuer,
            "aud": self.audience,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "deviceId": self.device_id,
            "email": self.email,
            "roles": list(self.roles),
        }


def issue_access_token(
    *,
    secret: bytes,
    issuer: str,
    audience: str,
    user_id: str,
    email: str,
    roles: list[str],
    device_id: str,
    ttl: int,
    now: int,
    jti: str = "",
) -> tuple[str, str, int]:
    """返回 (令牌, jti, 过期时间)。"""
    jti = jti or secrets.token_urlsafe(16)
    expires_at = now + ttl
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": user_id,
        "jti": jti,
        "typ": TOKEN_TYPE,
        "email": email,
        "roles": roles,
        "device": device_id,
        "iat": now,
        "nbf": now - LEEWAY,
        "exp": expires_at,
    }
    return encode(claims, secret), jti, expires_at


def verify_access_token(
    token: str,
    *,
    secret: bytes,
    issuer: str,
    audience: str,
    now: int | None = None,
) -> TokenClaims:
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthenticated("令牌结构非法")

    header_raw, payload_raw, signature_raw = parts

    # --- 1) 算法必须是我们认的那一个，且从签名反推而非采信 header ---
    try:
        header = json.loads(_unb64url(header_raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unauthenticated("令牌头部非法") from exc
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        raise Unauthenticated("不支持的签名算法")

    # --- 2) 验签（先验签，再信内容）---
    signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
    expected = _sign(signing_input, secret)
    try:
        provided = _unb64url(signature_raw)
    except Unauthenticated as exc:
        raise Unauthenticated("令牌签名非法") from exc
    if not hmac.compare_digest(expected, provided):
        raise Unauthenticated("令牌签名校验失败")

    # --- 3) 校验声明 ---
    try:
        claims = json.loads(_unb64url(payload_raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unauthenticated("令牌载荷非法") from exc
    if not isinstance(claims, dict):
        raise Unauthenticated("令牌载荷不是对象")

    current = int(time.time()) if now is None else now

    if claims.get("iss") != issuer:
        raise Unauthenticated("令牌签发方不匹配")
    if claims.get("aud") != audience:
        raise Unauthenticated("令牌受众不匹配")
    if claims.get("typ") != TOKEN_TYPE:
        raise Unauthenticated("令牌类型不匹配")

    subject = str(claims.get("sub") or "")
    if not subject:
        raise Unauthenticated("令牌缺少主体")

    try:
        expires_at = int(claims["exp"])
        issued_at = int(claims.get("iat") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise Unauthenticated("令牌时间声明非法") from exc

    if current > expires_at + LEEWAY:
        raise Unauthenticated("令牌已过期")
    not_before = int(claims.get("nbf") or 0)
    if not_before and current + LEEWAY < not_before:
        raise Unauthenticated("令牌尚未生效")
    # 签发时间在未来的令牌意味着时钟被拨动或有人在伪造
    if issued_at and issued_at > current + LEEWAY:
        raise Unauthenticated("令牌签发时间异常")

    roles = claims.get("roles") or []
    if not isinstance(roles, list):
        roles = []

    return TokenClaims(
        subject=subject,
        jti=str(claims.get("jti") or ""),
        issuer=str(claims.get("iss") or ""),
        audience=str(claims.get("aud") or ""),
        issued_at=issued_at,
        expires_at=expires_at,
        device_id=str(claims.get("device") or ""),
        email=str(claims.get("email") or ""),
        roles=tuple(str(r) for r in roles),
        raw=claims,
    )


# --------------------------------------------------------------------------- 随机串
def new_refresh_token() -> str:
    """刷新令牌：高熵随机串，配合服务端查表使用。"""
    return secrets.token_urlsafe(48)


def new_enrollment_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str, *, pepper: bytes = b"") -> str:
    """令牌入库只存哈希。

    用 SHA-256 而不是 PBKDF2：令牌本身是 48 字节高熵随机串，
    没有「猜」的空间，慢哈希在这里只是白白消耗性能。
    带 pepper 是为了「库被拖走但配置文件没被拖走」时依然安全。
    """
    return hashlib.sha256(pepper + raw.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


__all__ = [
    "ALGORITHM",
    "LEEWAY",
    "TokenClaims",
    "constant_time_equals",
    "decode_unverified",
    "encode",
    "hash_token",
    "issue_access_token",
    "new_enrollment_token",
    "new_refresh_token",
    "peek_jti",
    "verify_access_token",
]
