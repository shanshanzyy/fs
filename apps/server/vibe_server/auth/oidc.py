"""OIDC 对接（授权码 + PKCE + ID Token 验签）。

生产入口是企业 SSO，这一步不是为了「支持协议」而支持，而是为了
**不让治理面成为又一个密码库**：口令在 SSO 那边管，我们只认它签发的身份。

纯标准库实现 RS256 验签。这比看上去简单：JWKS 直接给出模数 n 与指数 e，
于是「验签」就是一次 `pow(sig, e, n)` 加一次 PKCS#1 v1.5 填充比对 ——
不需要引入 cryptography，也不需要解析 DER/PEM。

**必须做对的四点**（缺任何一点，SSO 反而会成为最弱的入口）：

1. **PKCE（S256）。** 授权码在重定向途中可能被截获，PKCE 让它单独无法兑换。
2. **校验 `state`。** 挡 CSRF —— 否则攻击者能把自己的授权码塞进受害者的浏览器，
   让受害者登录成攻击者的账号（后续所有操作都记在攻击者名下）。
3. **校验 ID Token 的 `nonce`、`aud`、`iss`、`exp`、`alg`。**
   `alg` 必须白名单校验，**绝不采信 header 里的算法**。
4. **用 JWKS 的公钥验签，而不是「解出 payload 就算数」。**
   不验签的 OIDC 实现等于认任何人的自签令牌。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..errors import BadRequest, Unauthenticated

# 只接受这些算法。RS256 是 OIDC 的事实标准，ES256 需要椭圆曲线实现，暂不支持。
_ALLOWED_ALGS = frozenset({"RS256"})

# PKCS#1 v1.5 的 DigestInfo 前缀（SHA-256）。签名验证靠它还原 ASN.1 结构。
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

# JWKS 缓存时长。太短会让每次登录都打 IdP，太长则在 IdP 轮换密钥后验签失败。
_JWKS_TTL = 3600

_MAX_JWKS_BYTES = 256 << 10


# --------------------------------------------------------------------------- 工具
def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise Unauthenticated("base64url 解码失败") from exc


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _fetch(url: str, *, timeout: int = 10) -> bytes:
    """不带代理地取远端文档。

    本地/内网 IdP 不该走系统代理 —— 和客户端那侧同样的坑，
    走代理会让「内网可达」变成「内网不可达」。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"} or host.endswith(".local"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    request = urllib.request.Request(url, headers={"User-Agent": "vibe-governance/0.1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read(_MAX_JWKS_BYTES)  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        raise Unauthenticated(f"IdP 返回 {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise Unauthenticated(f"无法连接 IdP: {exc.reason}") from exc


# --------------------------------------------------------------------------- JWKS
@dataclass
class _Jwk:
    kid: str
    n: int
    e: int

    def verify_rs256(self, signing_input: bytes, signature: bytes) -> bool:
        """RS256 验签：`pow(sig, e, n)` 得到填充后的 DigestInfo，比对其内容。"""
        try:
            key_size = (self.n.bit_length() + 7) // 8
            if len(signature) != key_size:
                return False
            value = pow(int.from_bytes(signature, "big"), self.e, self.n)
            encoded = value.to_bytes(key_size, "big")
        except (ValueError, OverflowError):
            return False

        if not encoded.startswith(b"\x00\x01"):
            return False
        separator = encoded.find(b"\x00", 2)
        if separator < 0:
            return False
        # 填充部分必须全是 0xFF 且长度至少 8 字节（PKCS#1 要求）
        if separator - 2 < 8 or encoded[2:separator] != b"\xff" * (separator - 2):
            return False

        digest = hashlib.sha256(signing_input).digest()
        return hmac.compare_digest(encoded[separator + 1 :], _SHA256_DIGEST_INFO + digest)


@dataclass
class JwksCache:
    jwks_uri: str
    _keys: dict[str, _Jwk] = field(default_factory=dict, repr=False)
    _fetched_at: int = 0
    _inflight: bool = False

    def keys(self, *, force: bool = False) -> dict[str, _Jwk]:
        now = int(time.time())
        if self._keys and not force and now - self._fetched_at < _JWKS_TTL:
            return self._keys
        return self._load()

    def _load(self) -> dict[str, _Jwk]:
        raw = _fetch(self.jwks_uri)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Unauthenticated("JWKS 文档不是合法 JSON") from exc
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list) or not entries:
            raise Unauthenticated("JWKS 文档里没有密钥")

        parsed: dict[str, _Jwk] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("kty") != "RSA":
                continue  # 只支持 RSA，别的类型直接跳过而不是猜
            use = entry.get("use")
            if use and use != "sig":
                continue
            alg = entry.get("alg")
            if alg and alg not in _ALLOWED_ALGS:
                continue
            kid = str(entry.get("kid") or "")
            try:
                n = int.from_bytes(_unb64url(str(entry["n"])), "big")
                e = int.from_bytes(_unb64url(str(entry["e"])), "big")
            except (KeyError, Unauthenticated):
                continue
            # 太小的模数不可能是认真签的证书，直接当无效
            if n.bit_length() < 2048 or e < 3:
                continue
            parsed[kid or f"key-{len(parsed)}"] = _Jwk(kid=kid, n=n, e=e)

        if not parsed:
            raise Unauthenticated("JWKS 里没有可用的 RSA 签名密钥")
        self._keys = parsed
        self._fetched_at = int(time.time())
        return parsed


# --------------------------------------------------------------------------- ID Token
@dataclass(frozen=True)
class IdTokenClaims:
    subject: str
    email: str
    email_verified: bool
    name: str
    issuer: str
    audience: str
    nonce: str
    expires_at: int
    raw: dict[str, Any]


def verify_id_token(
    token: str,
    *,
    jwks: JwksCache,
    issuer: str,
    client_id: str,
    nonce: str = "",
    now: int | None = None,
    leeway: int = 120,
) -> IdTokenClaims:
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthenticated("ID Token 结构非法")
    header_raw, payload_raw, signature_raw = parts

    try:
        header = json.loads(_unb64url(header_raw).decode("utf-8"))
        claims = json.loads(_unb64url(payload_raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unauthenticated("ID Token 解析失败") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Unauthenticated("ID Token 结构异常")

    # --- 算法白名单：只信我们自己认的，不看对方写了什么 ---
    alg = str(header.get("alg") or "")
    if alg not in _ALLOWED_ALGS:
        raise Unauthenticated(f"ID Token 使用了不支持的算法 {alg or '(缺失)'}")
    if "none" == alg.lower():
        raise Unauthenticated("拒绝 alg=none 的 ID Token")

    kid = str(header.get("kid") or "")
    signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
    try:
        signature = _unb64url(signature_raw)
    except Unauthenticated as exc:
        raise Unauthenticated("ID Token 签名非法") from exc

    key = jwks.keys().get(kid)
    if key is None and kid:
        # IdP 轮换密钥后，旧缓存里没有这个 kid —— 强制刷新一次再试
        key = jwks.keys(force=True).get(kid)
    if key is None:
        # 没有 kid 或对不上：只在这一种情况下允许「试遍所有密钥」，
        # 且必须真的验通一个，而不是「找不到就当通过」。
        if not any(k.verify_rs256(signing_input, signature) for k in jwks.keys().values()):
            raise Unauthenticated("ID Token 签名无法用 IdP 公钥验证")
    elif not key.verify_rs256(signing_input, signature):
        jwks.keys(force=True)  # 可能是轮换，刷新缓存供下次使用
        raise Unauthenticated("ID Token 签名校验失败")

    # --- 声明校验 ---
    current = int(time.time()) if now is None else now
    if str(claims.get("iss") or "").rstrip("/") != issuer.rstrip("/"):
        raise Unauthenticated("ID Token 签发方不匹配")

    audience = claims.get("aud")
    aud_ok = audience == client_id if isinstance(audience, str) else (
        isinstance(audience, list) and client_id in audience
    )
    if not aud_ok:
        raise Unauthenticated("ID Token 受众不匹配")
    # aud 是列表时必须再看 azp，否则「给别人的令牌」也能用
    if isinstance(audience, list) and len(audience) > 1:
        if str(claims.get("azp") or "") != client_id:
            raise Unauthenticated("ID Token 的 azp 与客户端不匹配")

    try:
        expires_at = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Unauthenticated("ID Token 缺少 exp") from exc
    if current > expires_at + leeway:
        raise Unauthenticated("ID Token 已过期")
    if int(claims.get("iat") or 0) > current + leeway:
        raise Unauthenticated("ID Token 签发时间异常")

    if nonce and str(claims.get("nonce") or "") != nonce:
        raise Unauthenticated("ID Token 的 nonce 与授权请求不匹配")

    subject = str(claims.get("sub") or "")
    if not subject:
        raise Unauthenticated("ID Token 缺少 sub")

    return IdTokenClaims(
        subject=subject,
        email=str(claims.get("email") or ""),
        email_verified=bool(claims.get("email_verified")),
        name=str(claims.get("name") or claims.get("preferred_username") or ""),
        issuer=str(claims.get("iss") or ""),
        audience=client_id,
        nonce=str(claims.get("nonce") or ""),
        expires_at=expires_at,
        raw=claims,
    )


# --------------------------------------------------------------------------- 授权码流
@dataclass
class OidcProvider:
    """一个 IdP 的接入配置 + 发现文档缓存。"""

    issuer: str
    client_id: str
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid email profile"
    _discovery: dict[str, Any] = field(default_factory=dict, repr=False)
    _jwks: JwksCache | None = field(default=None, repr=False)

    def document(self, *, force: bool = False) -> dict[str, Any]:
        if self._discovery and not force:
            return self._discovery
        url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        raw = _fetch(url)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Unauthenticated("OIDC 发现文档不是合法 JSON") from exc
        if not isinstance(doc, dict) or "authorization_endpoint" not in doc:
            raise Unauthenticated("OIDC 发现文档缺少 authorization_endpoint")
        # 发现文档里的 issuer 必须和我们配置的一致，否则可能被指到别的 IdP
        discovered = str(doc.get("issuer") or "").rstrip("/")
        if discovered and discovered != self.issuer.rstrip("/"):
            raise Unauthenticated(
                f"发现文档的 issuer（{discovered}）与配置不一致（{self.issuer}）"
            )
        self._discovery = doc
        return doc

    def jwks(self) -> JwksCache:
        if self._jwks is None:
            uri = str(self.document().get("jwks_uri") or "")
            if not uri:
                raise Unauthenticated("OIDC 发现文档缺少 jwks_uri")
            self._jwks = JwksCache(jwks_uri=uri)
        return self._jwks

    # ------------------------------------------------------------------ 发起
    def begin(self) -> dict[str, str]:
        """生成授权 URL 与本次流程的状态。state / verifier 由调用方暂存。"""
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        nonce = secrets.token_urlsafe(24)

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        endpoint = str(self.document()["authorization_endpoint"])
        return {
            "authorizationUrl": endpoint + "?" + urllib.parse.urlencode(params),
            "state": state,
            "codeVerifier": verifier,
            "nonce": nonce,
        }

    # ------------------------------------------------------------------ 兑换
    def exchange(self, *, code: str, code_verifier: str, nonce: str = "") -> IdTokenClaims:
        doc = self.document()
        endpoint = str(doc.get("token_endpoint") or "")
        if not endpoint:
            raise Unauthenticated("OIDC 发现文档缺少 token_endpoint")
        if not code:
            raise BadRequest("缺少授权码")

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret

        body = urllib.parse.urlencode(form).encode("ascii")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "vibe-governance/0.1",
            },
        )
        try:
            with urllib.request.build_opener().open(request, timeout=15) as response:
                payload = json.loads(response.read(_MAX_JWKS_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            raise Unauthenticated(f"IdP 拒绝了授权码兑换（{exc.code}）: {detail}") from exc
        except (urllib.error.URLError, ValueError, UnicodeDecodeError) as exc:
            raise Unauthenticated(f"授权码兑换失败: {exc}") from exc

        if not isinstance(payload, dict):
            raise Unauthenticated("IdP 的令牌响应不是对象")
        if payload.get("error"):
            raise Unauthenticated(
                f"IdP 返回错误: {payload.get('error')} {payload.get('error_description') or ''}".strip()
            )

        id_token = str(payload.get("id_token") or "")
        if not id_token:
            raise Unauthenticated("IdP 未返回 id_token")

        # 注意：**不校验 access_token**，也不会拿它去调 userinfo。
        # 我们只需要身份，拿最小范围的信息，减少对 IdP 的信任面。
        return verify_id_token(
            id_token,
            jwks=self.jwks(),
            issuer=self.issuer,
            client_id=self.client_id,
            nonce=nonce,
        )


class OidcFlowStore:
    """暂存进行中的授权流程（state → code_verifier / nonce）。

    **必须是一次的（pop 即删）**：state 被重放一次就够攻击者完成一次
    授权码注入。带 TTL 是因为用户可能在 IdP 页面上停留很久，
    但也不该无限期留着 —— 那等于慢慢攒一堆可用的 state。
    """

    def __init__(self, *, ttl: int = 600, max_entries: int = 2000, clock=None) -> None:
        self._flows: dict[str, tuple[dict[str, str], int]] = {}
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock

    def _now(self) -> int:
        if self._clock is not None:
            return int(self._clock.now())
        return int(time.time())

    def put(self, state: str, flow: dict[str, str]) -> None:
        self._sweep()
        if len(self._flows) >= self.max_entries:
            # 满了就丢最早的那个，而不是报错。报错会让「有人刷 begin 接口」
            # 变成对其他人的拒绝服务。
            oldest = min(self._flows, key=lambda key: self._flows[key][1])
            self._flows.pop(oldest, None)
        self._flows[state] = (dict(flow), self._now())

    def pop(self, state: str) -> dict[str, str] | None:
        if not state:
            return None
        entry = self._flows.pop(state, None)
        if entry is None:
            return None
        flow, created = entry
        if self._now() - created > self.ttl:
            return None
        return flow

    def _sweep(self) -> None:
        now = self._now()
        expired = [key for key, (_, created) in self._flows.items() if now - created > self.ttl]
        for key in expired:
            self._flows.pop(key, None)

    def __len__(self) -> int:
        return len(self._flows)


__all__ = [
    "IdTokenClaims",
    "JwksCache",
    "OidcFlowStore",
    "OidcProvider",
    "verify_id_token",
]
