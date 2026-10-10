"""静态页服务（Q 阶段）的测试。

登录页是服务端**唯一的 HTML 出口**，也是唯一一个不需要认证就能访问的页面 ——
这两件事凑在一起，最危险的不是「登录失败」，而是**页面本身**出问题：
路径穿越读到不该读的文件、安全头缺失导致令牌随 Referer 外泄、或页面在未登录时
反而打不开。所以这里是本阶段的防线。

四条钉死（对应计划 §14 Q5）：
  ① `/login` 返回 200 + `text/html` + 含 `Referrer-Policy: no-referrer`；
  ② 静态路径穿越（含编码过的 `%2e%2e%2f`）被拒；
  ③ 未认证可访问（公开页）；
  ④ CSP 头存在。

外加一条**防漂移**：`login.js` 里写死的接口路径必须等于 `routes.API` ——
两边各写一份字符串，漂移不会有任何报错，只会「点了没反应」。
"""

from __future__ import annotations

import unittest

from tests.support import TempDirTestCase, http_get, serve_app

from vibe_server.api.routes import API


class StaticPageTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app()
        self.base = serve_app(self, self.app)

    # ---------------------------------------------------------------- ① 登录页
    def test_login_page_is_html_with_referrer_policy(self) -> None:
        status, body, headers = http_get(self.base, "/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        # Referrer-Policy 由 `httpd._send()` 对**每个**响应统一附加：
        # 放在统一出口比放在每个 handler 可靠（handler 会漏，出口不会）。
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("登录", body)

    # ---------------------------------------------------------------- ④ CSP
    def test_login_page_carries_a_csp(self) -> None:
        status, _body, headers = http_get(self.base, "/login")
        self.assertEqual(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        # 登录页用 fetch 提交凭据，因此 connect-src 必须放开同源；
        # 但表单不得原生外发（form-action 'none'），帧不得嵌入（frame-ancestors）。
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_asset_is_served_with_csp(self) -> None:
        status, body, headers = http_get(self.base, "/assets/login.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertIn("fetch", body)

    # ---------------------------------------------------------------- ③ 公开
    def test_public_pages_need_no_token(self) -> None:
        """不带 Authorization 也必须能打开 —— 打开登录页的人正是「还没有令牌的人」。"""
        for path in ("/login", "/assets/login.js"):
            with self.subTest(path=path):
                status, _body, _headers = http_get(self.base, path)  # 不传 token
                self.assertEqual(status, 200)

    # ---------------------------------------------------------------- ② 穿越
    def test_path_traversal_is_rejected(self) -> None:
        """`%2e%2e%2f` 会被 Router 在匹配 `{name}` 时解码成 `../`，必须在这里现形被拒。"""
        for path in (
            "/assets/..%2fpyproject.toml",       # 编码过的 ../，落在 {name} 里
            "/assets/%2e%2e%2fREADME.md",
            "/assets/..%2f..%2fconfig.py",
        ):
            with self.subTest(path=path):
                status, _body, _headers = http_get(self.base, path)
                self.assertEqual(status, 403)

    def test_missing_asset_is_404(self) -> None:
        status, _body, _headers = http_get(self.base, "/assets/nope.js")
        self.assertEqual(status, 404)

    def test_uncoded_traversal_does_not_match_a_route(self) -> None:
        """未编码的 `..` 会多出一个路径段，匹配不上任何路由 → 404（同样是拒绝）。"""
        status, _body, _headers = http_get(self.base, "/assets/../login.html")
        self.assertEqual(status, 404)

    # ---------------------------------------------------------------- 路由冲突
    def test_login_route_does_not_collide_with_auth(self) -> None:
        """`/login` 是顶层静态页；`/api/v1/auth/login` 仍是账号接口，两者互不干扰。"""
        self.assertIsNotNone(self.app.router.resolve("GET", "/login"))
        self.assertEqual(self.app.router.resolve("GET", "/login")[0].template, "/login")
        auth = self.app.router.resolve("POST", API + "/auth/login")
        self.assertIsNotNone(auth)
        self.assertEqual(auth[0].template, API + "/auth/login")


class LoginScriptContractTests(unittest.TestCase):
    """`login.js` 里写死的接口路径必须与 `routes.API` 一致。

    这两份字符串没有任何机制保证同步 —— 服务端改了前缀、脚本没跟上，
    症状是「点了没反应」，排查成本极高。用一条静态契约把它钉死。
    """

    def _script(self) -> str:
        from vibe_server.api.static import STATIC_DIR

        return (STATIC_DIR / "login.js").read_text(encoding="utf-8")

    def test_login_script_uses_the_real_api_prefix(self) -> None:
        script = self._script()
        self.assertIn(f"'{API}/auth/login'", script)
        self.assertIn(f"'{API}/auth/oidc/begin'", script)

    def test_login_script_stores_tokens_in_session_storage(self) -> None:
        """计划要求：成功把 token 存 sessionStorage。"""
        script = self._script()
        self.assertIn("sessionStorage", script)
        self.assertIn("accessToken", script)


if __name__ == "__main__":
    unittest.main()