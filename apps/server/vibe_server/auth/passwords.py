"""口令哈希。

用 `hashlib.pbkdf2_hmac`（标准库）而不是 bcrypt/argon2：
内网离线环境装不上第三方轮子，而这个服务的口令只用于内部自助登录
（真正的生产入口是 SSO，见 oidc.py）。在这个威胁模型下，
PBKDF2-HMAC-SHA256 迭代 60 万次是够的 —— OWASP 2023 对 PBKDF2-SHA256
的建议值就是这个量级。

**格式里必须带算法与参数**（`pbkdf2_sha256$600000$salt$hash`），
否则将来提高迭代次数时无法兼容老口令：验证时要按口令里记的参数算，
而不是按当前常量算。少了这一点，调参之日就是全员密码失效之时。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32

# 常见弱口令的一小撮。生产靠 SSO + 密码策略，这里只挡住最离谱的。
_MIN_LENGTH = 10
_MAX_LENGTH = 256  # 上限是防「超长密码拖死 PBKDF2」的 DoS


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, KEY_BYTES)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """常量时间比对。任何格式异常都返回 False，不抛异常。

    不抛异常是刻意的：登录接口不该因为「库里的哈希坏了」而回一个
    和「密码错了」不同的响应——那等于告诉攻击者「这个账号存在且数据受损」。
    """
    if not password or not encoded:
        return False
    try:
        algorithm, raw_iterations, raw_salt, raw_hash = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(raw_iterations)
        if iterations <= 0 or iterations > 10_000_000:
            return False
        salt = _unb64(raw_salt)
        expected = _unb64(raw_hash)
    except (ValueError, TypeError):
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, len(expected) or KEY_BYTES
    )
    return hmac.compare_digest(derived, expected)


def needs_rehash(encoded: str) -> bool:
    """参数落后于当前标准时为 True，登录成功后应顺手升级存储。"""
    try:
        algorithm, raw_iterations, _, _ = encoded.split("$")
    except ValueError:
        return True
    return algorithm != ALGORITHM or int(raw_iterations) < ITERATIONS


def check_strength(password: str, *, email: str = "") -> None:
    """弱口令直接拒绝。抛 BadRequest，由 HTTP 层统一映射成 400。"""
    from ..errors import BadRequest

    if len(password) < _MIN_LENGTH:
        raise BadRequest(f"口令至少 {_MIN_LENGTH} 个字符")
    if len(password) > _MAX_LENGTH:
        raise BadRequest(f"口令不能超过 {_MAX_LENGTH} 个字符")
    lowered = password.lower()
    if email:
        local = email.split("@")[0].lower()
        # 只有够长的前缀才值得比对。`a@corp.com` 这种单字符 local part
        # 会让「包含字母 a」的口令全部被拒 —— 规则太蠢，用户只能绕开它，
        # 结果是把口令改得更可预测（比如统一加个后缀）。
        if len(local) >= 4 and local in lowered:
            raise BadRequest("口令不能包含邮箱前缀")
    if lowered in _COMMON:
        raise BadRequest("这个口令太常见了")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise BadRequest("口令需包含大小写字母、数字、符号中的至少三类")


_COMMON = frozenset(
    {
        "password123",
        "1234567890",
        "qwerty123456",
        "administrator",
        "letmein12345",
        "welcome12345",
        "vibecoding123",
    }
)


__all__ = ["check_strength", "hash_password", "needs_rehash", "verify_password"]
