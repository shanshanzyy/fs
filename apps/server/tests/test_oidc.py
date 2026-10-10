"""OIDC 对接的协议层测试（M2 收尾 E3）。

`auth/oidc.py` 里写着「必须做对的四点」：PKCE、state、ID Token 声明校验、
用 JWKS 验签。这四条如果只靠「读代码觉得对」，等于没有防线 ——
所以这里起一个**假 IdP**，把整条授权码流真的走一遍。

关键在于这个假 IdP **自己也真的在验**，否则测试没有意义：

1. `/token` 用授权请求里的 `code_challenge` 核对 `code_verifier`
   （只发「挑战值对得上」的令牌）—— 这样 PKCE 是不是真的生效，是验出来的，
   不是断言出来的；
2. 校验 `client_secret` 与 `redirect_uri`；
3. ID Token 用 RS256 **真签名**，让服务端的 JWKS 取密钥 + 验签链路被真的走一遍。

反面同样重要：只测「能登录成功」会漏掉 SSO 最危险的形态 ——
**一个谁都能过的入口**。所以负面用例（state 重放、nonce 不符、邮箱未验证、
IdP 未配置、令牌里自称管理员）与正面用例同等重要。

不引第三方 crypto：标准库的 `pow(sig, e, n)` 足以做 PKCS#1 v1.5 的 RS256，
和服务端那侧的做法对称。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from tests.support import TempDirTestCase, http_get, serve_app

from vibe_server import rbac

CLIENT_ID = "vibe-desktop"
CLIENT_SECRET = "s3cret-from-ops"
REDIRECT_URI = "http://127.0.0.1:9/auth/callback"
SUBJECT = "idp-subject-0001"
EMAIL = "dev@corp.example"
NAME = "Dev Example"

KID = "test-key-1"

# PKCS#1 v1.5 的 DigestInfo 前缀（SHA-256），RFC 8017 §9.2。
# 刻意在测试里**另写一份**而不是 import 被测模块的常量：两边共用同一个常量的话，
# 常量写错时两边一起错、测试照样绿。这里的定义是自检的 —— 只要前缀不对，
# 服务端的验签就必然失败。
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

_SMALL_PRIMES = (
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79,
    83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151, 157, 163, 167,
    173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233, 239, 241, 251,
)


# --------------------------------------------------------------------------- RSA 夹具
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


def _prime(bits: int, exponent: int, exclude: int = 0) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if candidate != exclude and (candidate - 1) % exponent and _is_probable_prime(candidate):
            return candidate


def _generate_rsa_key() -> tuple[int, int, int]:
    """生成一对 2048 位 RSA 密钥（n, e, d），纯标准库。

    不在测试里硬编码一对密钥：硬编码会让人分不清「验签过了」是因为实现正确，
    还是因为常量恰好拼得上。生成一对约 0.3 秒，模块级做一次即可。
    """
    e = 65537
    while True:
        p = _prime(1024, e)
        q = _prime(1024, e, exclude=p)
        n = p * q
        # 两个 1024 位素数之积**有一半概率是 2047 位**，而服务端会拒收
        # 不足 2048 位的模数（这正是它该做的）。不补足的话，这套夹具会
        # 随机地整批失败，看起来像「OIDC 偶发不通」。
        if n.bit_length() == 2048:
            break
    lam = (p - 1) * (q - 1) // math.gcd(p - 1, q - 1)
    return n, e, pow(e, -1, lam)


_RSA_N, _RSA_E, _RSA_D = _generate_rsa_key()
_KEY_SIZE = (_RSA_N.bit_length() + 7) // 8


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign_rs256(signing_input: bytes) -> bytes:
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    padding = _KEY_SIZE - len(digest_info) - 3
    assert padding >= 8, "测试密钥太短，装不下 PKCS#1 填充"
    encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return pow(int.from_bytes(encoded, "big"), _RSA_D, _RSA_N).to_bytes(_KEY_SIZE, "big")


# --------------------------------------------------------------------------- 假 IdP
class FakeIdP:
    """最小但会真的拒绝的 OIDC 提供方。

    只实现被测链路真正用到的三个端点：发现文档、JWKS、令牌兑换。
    `/authorize` 有意不实现 —— 授权发生在浏览器里，服务端从不访问它，
    而本测试要验的正是「服务端这一侧」。测试直接从 begin 的返回里取出
    `code_challenge` 交给 IdP，等价于「用户真的在 IdP 页面上完成了授权」。
    """

    def __init__(self, *, client_id: str, client_secret: str = "", redirect_uri: str = "") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        #: code -> 该 code 的期望（PKCE 挑战值、nonce、声明）
        self.codes: dict[str, dict[str, Any]] = {}
        #: 每次 /token 收到的表单，供断言「服务端到底发了什么」
        self.token_requests: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "FakeIdP":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/.well-known/openid-configuration":
                    self._send(
                        200,
                        {
                            "issuer": outer.base,
                            "authorization_endpoint": outer.base + "/authorize",
                            "token_endpoint": outer.base + "/token",
                            "jwks_uri": outer.base + "/jwks",
                            "response_types_supported": ["code"],
                            "code_challenge_methods_supported": ["S256"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                        },
                    )
                elif self.path == "/jwks":
                    n = _RSA_N.to_bytes(_KEY_SIZE, "big")
                    e = _RSA_E.to_bytes((_RSA_E.bit_length() + 7) // 8, "big")
                    self._send(
                        200,
                        {
                            "keys": [
                                {
                                    "kty": "RSA",
                                    "use": "sig",
                                    "alg": "RS256",
                                    "kid": KID,
                                    "n": _b64url(n),
                                    "e": _b64url(e),
                                }
                            ]
                        },
                    )
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/token":
                    self._send(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
                outer.token_requests.append(form)
                self._send(*outer.handle_token(form))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---------------------------------------------------------------- 期望登记
    def expect_code(
        self,
        code: str,
        *,
        code_challenge: str,
        nonce: str = "",
        subject: str = SUBJECT,
        email: str = EMAIL,
        email_verified: bool = True,
        name: str = NAME,
        extra_claims: dict[str, Any] | None = None,
        expires_in: int = 300,
    ) -> None:
        """登记一个「已授权」的 code 及其应签发的声明。

        `nonce` 由调用方传入 —— 传成别的值就能模拟「令牌被掉包」，
        这正是 nonce 存在的意义。
        """
        self.codes[code] = {
            "code_challenge": code_challenge,
            "nonce": nonce,
            "subject": subject,
            "email": email,
            "email_verified": email_verified,
            "name": name,
            "extra_claims": dict(extra_claims or {}),
            "expires_in": expires_in,
        }

    # ---------------------------------------------------------------- 令牌兑换
    def handle_token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """校验兑换请求并签发 ID Token。返回 (状态码, 响应体)。"""
        if form.get("grant_type") != "authorization_code":
            return 400, {"error": "unsupported_grant_type"}
        entry = self.codes.get(form.get("code") or "")
        if entry is None:
            return 400, {"error": "invalid_grant", "error_description": "未知的 code"}
        verifier = form.get("code_verifier") or ""
        # PKCE 真的在验：只有 sha256(verifier) 能对上授权时的挑战值才发令牌
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not verifier or challenge != entry["code_challenge"]:
            return 400, {
                "error": "invalid_grant",
                "error_description": "code_verifier 与 code_challenge 不匹配",
            }
        if form.get("client_id") != self.client_id:
            return 401, {"error": "invalid_client"}
        if self.client_secret and form.get("client_secret") != self.client_secret:
            return 401, {"error": "invalid_client"}
        if self.redirect_uri and form.get("redirect_uri") != self.redirect_uri:
            return 400, {"error": "invalid_request", "error_description": "redirect_uri 不符"}

        issued = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.base,
            "aud": self.client_id,
            "sub": entry["subject"],
            "email": entry["email"],
            "email_verified": entry["email_verified"],
            "name": entry["name"],
            "nonce": entry["nonce"],
            "iat": issued,
            "exp": issued + int(entry["expires_in"]),
        }
        claims.update(entry["extra_claims"])
        return 200, {"token_type": "Bearer", "expires_in": entry["expires_in"], "id_token": self._sign(claims)}

    @staticmethod
    def _sign(claims: dict[str, Any]) -> str:
        header = {"alg": "RS256", "typ": "JWT", "kid": KID}
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        ]
        signing_input = ".".join(segments).encode("ascii")
        return ".".join(segments + [_b64url(_sign_rs256(signing_input))])


# --------------------------------------------------------------------------- HTTP 小工具
def post_json(base: str, path: str, body: dict[str, Any]) -> tuple[int, str, Any]:
    """POST 一个 JSON。4xx/5xx 也当正常结果返回（负面用例正是被测对象）。"""
    import urllib.error

    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


_OIDC_ENV_KEYS = (
    "VIBE_SRV_OIDC_ISSUER",
    "VIBE_SRV_OIDC_CLIENT_ID",
    "VIBE_SRV_OIDC_CLIENT_SECRET",
    "VIBE_SRV_OIDC_REDIRECT_URI",
)


# --------------------------------------------------------------------------- 用例
class OidcTests(TempDirTestCase):
    """整条授权码流。每个用例一份独立的库、IdP 与配置。"""

    def setUp(self) -> None:
        super().setUp()
        self.idp = FakeIdP(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI
        ).start()
        self.addCleanup(self.idp.stop)

    # ---------------------------------------------------------------- 夹具
    def make_governed_app(self, **env_overrides: str) -> Any:
        """按「已对接 IdP」的形态起一个 app（生产走的就是这几个环境变量）。"""
        env = {
            "VIBE_SRV_OIDC_ISSUER": self.idp.base,
            "VIBE_SRV_OIDC_CLIENT_ID": CLIENT_ID,
            "VIBE_SRV_OIDC_CLIENT_SECRET": CLIENT_SECRET,
            "VIBE_SRV_OIDC_REDIRECT_URI": REDIRECT_URI,
        }
        env.update(env_overrides)
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.make_app()

    def make_unconfigured_app(self) -> Any:
        """按「没对接 IdP」的形态起 app：**必须先清掉环境变量**，
        否则本机恰好配过 OIDC 时这条用例会神奇地变成另一件事。"""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _OIDC_ENV_KEYS:
            os.environ.pop(key, None)
        return self.make_app()

    def begin(self, base: str) -> dict[str, str]:
        """走 begin，返回 authorizationUrl 的查询参数。"""
        status, body, _ = http_get(base, "/api/v1/auth/oidc/begin")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        # 只回授权地址：state / code_verifier 留在服务端，绝不交给客户端保管
        self.assertEqual(sorted(payload), ["authorizationUrl"])
        url = urllib.parse.urlparse(payload["authorizationUrl"])
        return dict(urllib.parse.parse_qsl(url.query))

    def callback(
        self, base: str, *, state: str, code: str, device_name: str = "test-device"
    ) -> tuple[int, Any]:
        status, body, _ = post_json(
            base,
            "/api/v1/auth/oidc/callback",
            {"state": state, "code": code, "deviceName": device_name, "platform": "test"},
        )
        return status, json.loads(body)

    # ---------------------------------------------------------------- 正面
    def test_full_flow_begin_callback_then_me(self) -> None:
        """begin → callback → 拿令牌 → `/auth/me`，整条链真的走通。

        参数里钉住三件事：`state` 非空、`code_challenge_method=S256`、
        授权地址指向 IdP 的 authorization_endpoint。
        """
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["client_id"], CLIENT_ID)
        self.assertEqual(params["redirect_uri"], REDIRECT_URI)
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertTrue(params["state"])
        self.assertTrue(params["nonce"])
        self.assertNotEqual(params["code_challenge"], params["state"])
        self.assertIn("openid", params["scope"].split())

        self.idp.expect_code(
            "code-happy", code_challenge=params["code_challenge"], nonce=params["nonce"]
        )
        status, payload = self.callback(base, state=params["state"], code="code-happy")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["accessToken"])

        # 服务端交给 IdP 的兑换请求必须带上 PKCE verifier 与机密客户端凭证
        self.assertEqual(len(self.idp.token_requests), 1)
        sent = self.idp.token_requests[0]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["client_id"], CLIENT_ID)
        self.assertEqual(sent["client_secret"], CLIENT_SECRET)
        self.assertEqual(sent["redirect_uri"], REDIRECT_URI)
        self.assertTrue(sent["code_verifier"])

        status, body, _ = http_get(base, "/api/v1/auth/me", token=payload["accessToken"])
        self.assertEqual(status, 200, body)
        actor = json.loads(body)["actor"]
        self.assertEqual(actor["roles"], [rbac.ROLE_DEVELOPER])
        self.assertFalse(actor["orgWide"])
        self.assertTrue(actor["teams"], "SSO 账号必须至少有一条 membership，否则登录进来什么都干不了")
        self.assertEqual({team["role"] for team in actor["teams"]}, {rbac.ROLE_DEVELOPER})

        # 账号确实落了库，且**没有**本地口令（SSO 用户不能用口令登录）
        user = app.store.find_user_by_external_id(SUBJECT)
        self.assertIsNotNone(user)
        self.assertEqual(user["password_hash"], "")

    def test_second_login_reuses_account_by_external_id(self) -> None:
        """同一个 sub 再次登录要接上原账号 —— 改了邮箱不该凭空多出一个账号。"""
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.idp.expect_code("code-1", code_challenge=params["code_challenge"], nonce=params["nonce"])
        _, first = self.callback(base, state=params["state"], code="code-1")

        # 第二次：sub 不变，邮箱变了
        params = self.begin(base)
        self.idp.expect_code(
            "code-2",
            code_challenge=params["code_challenge"],
            nonce=params["nonce"],
            email="moved@corp.example",
        )
        status, second = self.callback(base, state=params["state"], code="code-2")
        self.assertEqual(status, 200, second)

        self.assertEqual(second["user"]["id"], first["user"]["id"])
        self.assertIsNone(app.store.find_user_by_email("moved@corp.example"))
        self.assertEqual(
            str(app.store.find_user_by_external_id(SUBJECT)["id"]), str(first["user"]["id"])
        )

    # ---------------------------------------------------------------- 负面
    def test_state_replay_is_rejected(self) -> None:
        """state 一次性：重放第二次必须被拒，且**不再打 IdP**。

        这条挡的是授权码注入 —— 攻击者把自己的授权码塞进受害者的浏览器，
        让受害者登录成攻击者的账号（之后所有操作都记在攻击者名下）。
        重放能过的实现等于没做 CSRF 防护。
        """
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.idp.expect_code("code-a", code_challenge=params["code_challenge"], nonce=params["nonce"])
        self.assertEqual(self.callback(base, state=params["state"], code="code-a")[0], 200)

        self.idp.expect_code("code-b", code_challenge=params["code_challenge"], nonce=params["nonce"])
        status, payload = self.callback(base, state=params["state"], code="code-b")
        self.assertEqual(status, 403, payload)
        self.assertIn("state", payload["error"]["message"])
        self.assertEqual(len(self.idp.token_requests), 1, "重放的 state 不该走到兑换那一步")

        status, payload = self.callback(base, state="never-issued", code="code-b")
        self.assertEqual(status, 403, payload)

    def test_begin_returns_404_when_oidc_not_configured(self) -> None:
        """没配 OIDC 时两个端点都是 404 —— 不是 500，也不是「流程走到一半失败」。

        内网默认形态就是「口令 + 注册令牌」，此时 IdP 相关端点是**不存在**的，
        而不是坏掉的：让客户端能靠状态码直接判断该走哪条路。
        """
        app = self.make_unconfigured_app()
        base = serve_app(self, app)
        self.assertIsNone(app.oidc)

        status, body, _ = http_get(base, "/api/v1/auth/oidc/begin")
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        status, body = self.callback(base, state="whatever", code="whatever")
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_id_token_claims_never_grant_admin(self) -> None:
        """ID Token 里自称 owner / 塞进管理员组，**都不认**。

        SSO 只证明「你是谁」，不证明「你该有多大权限」。授权是治理面自己的事
        （成员表 + 角色），把角色映射交给 IdP 的声明等于把提权做成一个
        「改一下 IdP 上的组名」就能完成的动作。
        """
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.idp.expect_code(
            "code-priv",
            code_challenge=params["code_challenge"],
            nonce=params["nonce"],
            extra_claims={
                "role": "owner",
                "roles": ["owner", "team_admin"],
                "groups": ["platform-admins"],
                "vibe_role": "owner",
                "org_wide": True,
            },
        )
        status, payload = self.callback(base, state=params["state"], code="code-priv")
        self.assertEqual(status, 200, payload)

        status, body, _ = http_get(base, "/api/v1/auth/me", token=payload["accessToken"])
        actor = json.loads(body)["actor"]
        self.assertEqual(actor["roles"], [rbac.ROLE_DEVELOPER])
        self.assertFalse(actor["orgWide"])
        self.assertNotIn(rbac.PERM_POLICY_WRITE, actor["permissions"])
        self.assertNotIn(rbac.PERM_USER_WRITE, actor["permissions"])
        self.assertNotIn(rbac.PERM_SECRET_WRITE, actor["permissions"])
        self.assertIn(rbac.PERM_GATEWAY_USE, actor["permissions"])

    def test_nonce_mismatch_is_rejected(self) -> None:
        """nonce 对不上就拒 —— 这是「令牌被掉包」的唯一信号。

        攻击者拿到了一个**合法签名**的 ID Token（比如另一个会话的），
        但 nonce 对不上本次授权请求，就必须被挡在门外。
        """
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.idp.expect_code(
            "code-swap",
            code_challenge=params["code_challenge"],
            nonce="attacker-supplied-nonce",
        )
        status, payload = self.callback(base, state=params["state"], code="code-swap")
        self.assertEqual(status, 401, payload)
        self.assertIn("nonce", payload["error"]["message"])

    def test_unverified_email_is_rejected(self) -> None:
        """IdP 明确说「邮箱未验证」时拒登录。

        我们会用邮箱做账号合并，不校验等于把账号接管的口子留着：随便填一个
        别人的邮箱就能接上别人的账号。（缺失该字段时放行 —— 有些纯内网 IdP
        不返回它，这条由 `login_via_oidc` 的注释说明。）"""
        app = self.make_governed_app()
        base = serve_app(self, app)

        params = self.begin(base)
        self.idp.expect_code(
            "code-unverified",
            code_challenge=params["code_challenge"],
            nonce=params["nonce"],
            email_verified=False,
        )
        status, payload = self.callback(base, state=params["state"], code="code-unverified")
        self.assertEqual(status, 403, payload)
        self.assertIsNone(app.store.find_user_by_external_id(SUBJECT))


if __name__ == "__main__":
    unittest.main()