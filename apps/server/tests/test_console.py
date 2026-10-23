"""治理面 Web 控制台（M8）的测试。

分两段：

① **后端读口**：控制台要列团队、列成员、列邀请码台账，为此新增了三个只读接口
   （`GET /teams`、`GET /teams/{id}/members`、`GET /enrollments`）。这些接口都是
   「把管理数据暴露给更多人」的动作，最危险的从来不是「返回 500」，而是**越权**：
   开发者翻到别的团队、或把签发新账号的邀请码台账看光。所以这段测的是权限边界，
   以及 /enrollments **永不回显 token_hash**（ADR-006 红线：服务端不存明文凭证）。

② **前端契约**（见文件末尾）：控制台是零构建静态页，它的正确性有一半在
   「脚本里写死的接口路径与服务端一致」「不出现内联脚本（CSP 锁死）」上 ——
   这两类漂移不会报错，只会「点了没反应」，只能靠静态契约钉死。
"""

from __future__ import annotations

import json
import unittest

from tests.support import (
    TempDirTestCase,
    actor_of,
    bootstrap_and_login,
    http_get,
    serve_app,
)

from vibe_server import rbac
from vibe_server.api.routes import API
from vibe_server.api.static import CONSOLE_DIR, CSP


class ConsoleReadApiTests(TempDirTestCase):
    """控制台三个只读接口（M8-a）的权限边界与形状。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app()
        self.base = serve_app(self, self.app)
        self.session = bootstrap_and_login(self.app)
        self.owner_token = self.session["accessToken"]
        self.owner = actor_of(self.app, self.owner_token)
        self.store = self.app.store
        # bootstrap 会自动建一个默认团队（承载所有未指定团队的新账号），
        # 它就是开发者要落进去的「本团队」alpha。
        self.alpha = str(self.store.find_team_by_slug("default")["id"])

    # ---------------------------------------------------------------- 夹具
    def _developer_token(self) -> str:
        """造一个「已在 alpha 团队」的开发者并登录。

        走真实的邀请码注册路径（而不是直接塞 membership），
        这样顺带覆盖了「邀请码 → 注册 → 落进指定团队」这条链路。
        """
        enrollment = self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=self.alpha, role=rbac.ROLE_DEVELOPER
        )
        dev = self.app.auth_service.register_with_enrollment(
            enrollment_token=enrollment["enrollmentToken"],
            email="dev@corp.example",
            password="Str0ng!Passw0rd",
            device_name="dev-device",
            platform="test",
            fingerprint="fp-dev-1",
        )
        return dev["accessToken"]

    def _json(self, path: str, *, token: str = "") -> tuple[int, dict]:
        status, body, _headers = http_get(self.base, path, token=token)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {"_raw": body}

    # ---------------------------------------------------------------- 鉴权
    def test_teams_requires_a_token(self) -> None:
        status, _body, _headers = http_get(self.base, API + "/teams")
        self.assertEqual(status, 401)

    def test_team_members_requires_a_token(self) -> None:
        status, _body, _headers = http_get(self.base, API + f"/teams/{self.alpha}/members")
        self.assertEqual(status, 401)

    def test_enrollments_requires_a_token(self) -> None:
        status, _body, _headers = http_get(self.base, API + "/enrollments")
        self.assertEqual(status, 401)

    # ---------------------------------------------------------------- /teams
    def test_owner_lists_teams_with_member_count(self) -> None:
        status, body = self._json(API + "/teams", token=self.owner_token)
        self.assertEqual(status, 200, body)
        teams = body["teams"]
        alpha = next(t for t in teams if t["id"] == self.alpha)
        self.assertEqual(alpha["slug"], "default")
        # owner 自己就是 alpha 的成员，成员数至少为 1
        self.assertGreaterEqual(alpha["memberCount"], 1)
        self.assertIn("createdAt", alpha)

    def test_developer_sees_only_own_team(self) -> None:
        """非组织级不得看到「组织里有哪些团队」——只回自己所属的。"""
        self.app.auth_service.create_team(actor=self.owner, slug="beta", name="Beta")
        beta = str(self.store.find_team_by_slug("beta")["id"])
        dev_token = self._developer_token()

        status, body = self._json(API + "/teams", token=dev_token)
        self.assertEqual(status, 200, body)
        ids = {t["id"] for t in body["teams"]}
        self.assertIn(self.alpha, ids)
        self.assertNotIn(beta, ids)

    # ---------------------------------------------------- /teams/{id}/members
    def test_owner_can_list_members_of_any_team(self) -> None:
        status, body = self._json(API + f"/teams/{self.alpha}/members", token=self.owner_token)
        self.assertEqual(status, 200, body)
        members = body["members"]
        self.assertTrue(members)
        self.assertIn(self.owner.user_id, {m["userId"] for m in members})
        self.assertEqual(members[0].get("role"), rbac.ROLE_OWNER)

    def test_developer_cannot_read_other_team_members(self) -> None:
        """水平越权入口：A 团队成员读 B 团队名册必须 403。"""
        self.app.auth_service.create_team(actor=self.owner, slug="beta", name="Beta")
        beta = str(self.store.find_team_by_slug("beta")["id"])
        dev_token = self._developer_token()

        status, _body, _headers = http_get(
            self.base, API + f"/teams/{beta}/members", token=dev_token
        )
        self.assertEqual(status, 403)

    def test_developer_can_read_own_team_members(self) -> None:
        dev_token = self._developer_token()
        status, body = self._json(API + f"/teams/{self.alpha}/members", token=dev_token)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["members"])

    # ---------------------------------------------------------------- /enrollments
    def test_developer_cannot_list_enrollments(self) -> None:
        """developer 没有 enrollment:write —— 邀请码台账不得可见。"""
        dev_token = self._developer_token()
        status, _body, _headers = http_get(self.base, API + "/enrollments", token=dev_token)
        self.assertEqual(status, 403)

    def test_owner_lists_enrollments_without_token_hash(self) -> None:
        self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=self.alpha, role=rbac.ROLE_DEVELOPER, max_uses=3
        )
        status, body, _headers = http_get(self.base, API + "/enrollments", token=self.owner_token)
        self.assertEqual(status, 200)
        # ADR-006 红线：台账只回展示所需字段，绝不回 token_hash / 明文令牌。
        self.assertNotIn("tokenHash", body)
        self.assertNotIn("token_hash", body)
        self.assertNotIn("enrollmentToken", body)
        rows = json.loads(body)["enrollments"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["teamId"], self.alpha)
        self.assertEqual(row["role"], rbac.ROLE_DEVELOPER)
        self.assertEqual(row["maxUses"], 3)
        self.assertEqual(row["uses"], 0)
        self.assertFalse(row["revoked"])
        self.assertEqual(
            set(row),
            {
                "id",
                "teamId",
                "role",
                "createdBy",
                "createdAt",
                "expiresAt",
                "maxUses",
                "uses",
                "revoked",
            },
        )

    def test_enrollments_filtered_by_team(self) -> None:
        self.app.auth_service.create_team(actor=self.owner, slug="beta", name="Beta")
        beta = str(self.store.find_team_by_slug("beta")["id"])
        self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=self.alpha, role=rbac.ROLE_DEVELOPER
        )
        self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=beta, role=rbac.ROLE_TEAM_ADMIN
        )

        status, body = self._json(
            API + f"/enrollments?teamId={beta}", token=self.owner_token
        )
        self.assertEqual(status, 200, body)
        rows = body["enrollments"]
        self.assertEqual([r["teamId"] for r in rows], [beta])

    def test_enrollments_visible_scope_excludes_foreign_team(self) -> None:
        """team_admin 只能看自己有 enrollment:write 的团队的台账。

        造一个 beta 团队的 team_admin：他能看 beta 的台账，但看不到 alpha 的。
        """
        self.app.auth_service.create_team(actor=self.owner, slug="beta", name="Beta")
        beta = str(self.store.find_team_by_slug("beta")["id"])
        # owner 签发 beta 团队管理员邀请码，注册出该管理员
        enrollment = self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=beta, role=rbac.ROLE_TEAM_ADMIN
        )
        admin = self.app.auth_service.register_with_enrollment(
            enrollment_token=enrollment["enrollmentToken"],
            email="admin@corp.example",
            password="Str0ng!Passw0rd",
            device_name="admin-device",
            platform="test",
            fingerprint="fp-admin-1",
        )
        # alpha 也建一条邀请码，用来验证「看不见别的团队」
        self.app.auth_service.create_enrollment(
            actor=self.owner, team_id=self.alpha, role=rbac.ROLE_DEVELOPER
        )

        status, body = self._json(API + "/enrollments", token=admin["accessToken"])
        self.assertEqual(status, 200, body)
        self.assertEqual({r["teamId"] for r in body["enrollments"]}, {beta})


class ConsoleStaticPageTests(TempDirTestCase):
    """控制台外壳与静态资源的出口契约（M8-b）。

    控制台是零构建静态页，它的正确性一大半在「出口」上：页面能打开、资源带 CSP、
    穿越被拒、不出现内联脚本。以及一条**防漂移**：脚本里写死的接口路径必须与
    `routes.API` 一致 —— 两边各写一份字符串，漂移不会报错，只会「点了没反应」。
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = self.make_app()
        self.base = serve_app(self, self.app)

    def _asset(self, name: str) -> str:
        return (CONSOLE_DIR / name).read_text(encoding="utf-8")

    # ---------------------------------------------------------------- ① 外壳
    def test_console_page_is_html_with_csp(self) -> None:
        status, body, headers = http_get(self.base, "/console")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertIn("控制台", body)

    # ---------------------------------------------------------------- ④ CSP
    def test_console_asset_is_served_with_csp(self) -> None:
        status, body, headers = http_get(self.base, "/console/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertIn("import", body)

    # ---------------------------------------------------------------- ③ 公开
    def test_console_pages_need_no_token(self) -> None:
        for path in ("/console", "/console/assets/app.js", "/console/assets/console.css"):
            with self.subTest(path=path):
                status, _body, _headers = http_get(self.base, path)
                self.assertEqual(status, 200)

    # ---------------------------------------------------------------- ② 穿越
    def test_console_path_traversal_is_rejected(self) -> None:
        for path in (
            "/console/assets/..%2fpyproject.toml",
            "/console/assets/%2e%2e%2f..%2fconfig.py",
            "/console/assets/..%2f..%2fstatic%2flogin.js",
        ):
            with self.subTest(path=path):
                status, _body, _headers = http_get(self.base, path)
                self.assertEqual(status, 403)

    def test_console_missing_asset_is_404(self) -> None:
        status, _body, _headers = http_get(self.base, "/console/assets/nope.js")
        self.assertEqual(status, 404)

    # ---------------------------------------------------------------- 路由冲突
    def test_console_routes_do_not_collide(self) -> None:
        """四个静态路由各自 resolve，登录页与控制台互不干扰。"""
        for path, template in (
            ("/login", "/login"),
            ("/console", "/console"),
            ("/assets/{name}", "/assets/{name}"),
            ("/console/assets/{name}", "/console/assets/{name}"),
        ):
            with self.subTest(path=path):
                resolved = self.app.router.resolve("GET", path)
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved[0].template, template)

    # ---------------------------------------------------------------- ⑧ 无内联
    def test_console_html_has_no_inline_script_or_handler(self) -> None:
        """CSP 剑指内联：页面里不得有内联 `<script>` 与 `onclick=` 等内联事件。"""
        html = (CONSOLE_DIR / "console.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertIn('<script type="module" src="/console/assets/app.js">', html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("onload=", html)

    # ---------------------------------------------------------------- ⑦ 防漂移
    def test_scripts_pin_the_auth_paths(self) -> None:
        api_js = self._asset("api.js")
        self.assertIn("'/api/v1/auth/me'", api_js)
        self.assertIn("'/api/v1/auth/refresh'", api_js)

    def test_panel_and_nav_paths_match_the_api_prefix(self) -> None:
        """面板里写死的数据接口路径都要落在 `routes.API` 前缀下。"""
        panels_js = self._asset("panels.js")
        for suffix in (
            "/users", "/enrollments", "/teams", "/devices",
            "/policy", "/quota", "/audit/summary", "/audit/events", "/usage", "/secrets",
            "/client/releases", "/client/releases/revoke",
        ):
            with self.subTest(suffix=suffix):
                self.assertIn("'" + API + suffix + "'", panels_js)

    # ---------------------------------------------------------------- ⑭ fetch
    def test_scripts_fetch_with_authorization_header(self) -> None:
        api_js = self._asset("api.js")
        self.assertIn("fetch(", api_js)
        self.assertIn("Authorization", api_js)
        self.assertIn("Bearer ", api_js)

    # ---------------------------------------------------------------- ⑮ 单飞刷新
    def test_api_js_has_single_flight_refresh(self) -> None:
        api_js = self._asset("api.js")
        self.assertIn("401", api_js)
        self.assertIn("refreshing", api_js)     # 单飞：并发 401 只刷一次
        self.assertIn("refreshOnce", api_js)

    # ---------------------------------------------------------------- ⑯ 会话键
    def test_scripts_share_the_login_session_keys(self) -> None:
        api_js = self._asset("api.js")
        self.assertIn("'vibe.accessToken'", api_js)
        self.assertIn("'vibe.refreshToken'", api_js)


if __name__ == "__main__":
    unittest.main()
