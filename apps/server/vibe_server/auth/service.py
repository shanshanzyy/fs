"""认证服务：注册、登录、刷新轮换、设备绑定。

四个必须做对的点，每一个都对应一类真实攻击：

1. **刷新令牌一次性 + 重用检测（refresh token rotation + reuse detection）。**
   每次刷新都换一个新的，老的立即作废。如果某个已作废的令牌被再次使用，
   说明它被复制走了（要么被窃取，要么客户端有 bug），此时**吊销整族**——
   即这条设备链上所有令牌全部失效，逼用户重新登录。
   只作废单个令牌是没用的：攻击者手上往往还有更新的一枚。

2. **登录失败不区分「用户不存在」和「密码错误」。** 统一回同样的消息，
   且都走一次口令哈希（对不存在的用户算一个假哈希），
   否则响应时间差会泄漏「哪些邮箱是有效账号」。

3. **设备绑定。** 令牌与设备 id 绑定，撤设备即撤该设备所有会话。

4. **口令变更/重置后立即失效所有会话。** 用户改密码的心理预期就是
   「把别人踢下线」，做不到就是安全缺陷。
"""

from __future__ import annotations

import secrets

from ..errors import BadRequest, Conflict, Forbidden, NotFound, Unauthenticated
from ..ids import new_id
from .. import rbac
from . import passwords, tokens

# 一个恒定的假哈希：给「用户不存在」的分支算一次，抹平时间差。
_DUMMY_HASH = passwords.hash_password("dummy-password-for-timing")

# 默认团队：承载所有「未指定团队」的新账号。见 _default_team_id 的说明。
DEFAULT_TEAM_SLUG = "default"
DEFAULT_TEAM_NAME = "默认团队"


class AuthService:
    def __init__(self, *, store, config, clock) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self._pepper = b"vibe-refresh-pepper:" + bytes(config.jwt_secret[:16])

    # ============================================================== 注册 / 登录
    def bootstrap_org(self, *, email: str, password: str, display_name: str = "") -> dict:
        """创建第一个组织所有者。

        只在「一个用户都没有」时允许 —— 否则任何人都能给自己开个 owner。
        生产环境应该关掉这条路径改用 SSO，这里保留是为了让内网能自助起步。

        **必须同时建一个默认团队并把所有者放进去。** 只建用户的话，
        这个人没有任何 membership，而角色全部由 membership 推导 ——
        结果是「管理员建好了，但登录进去什么都点不动」（每个接口都 403）。
        这个坑很隐蔽：注册接口返回 201，看起来一切正常。
        """
        now = self.clock.now()
        passwords.check_strength(password, email=email)

        user_id = new_id("usr")
        email = email.lower().strip()
        # 建号 + 默认团队 + owner membership 必须在同一个事务里完成：
        # 分事务的话，并发 bootstrap 会各建一个 owner（先查后写）；
        # 进程在中间崩溃还会留下「有账号但没有 membership」的 owner
        # —— 角色全部由 membership 推导，表现为登录进去每个接口都 403。
        if not self.store.bootstrap_first_user(
            user_id,
            email,
            display_name or email.split("@")[0],
            passwords.hash_password(password),
            membership_id=new_id("mem"),
            role=rbac.ROLE_OWNER,
            team_id=new_id("tm"),
            team_slug=DEFAULT_TEAM_SLUG,
            team_name=DEFAULT_TEAM_NAME,
            now=now,
        ):
            raise Forbidden("组织已初始化，请改用注册令牌或 SSO 加入")
        team = self.store.find_team_by_slug(DEFAULT_TEAM_SLUG)
        team_id = str(team["id"]) if team is not None else ""
        self.store.record_auth_event(
            "user.bootstrap", user_id=user_id, detail=f"team={team_id}", now=now
        )
        return {"userId": user_id, "email": email, "teamId": team_id}

    def _default_team_id(self, *, now: int) -> str:
        """取（必要时创建）默认团队。

        为什么需要它：注册令牌可以不指定团队（内网自助开通的常见形态），
        此时用户会没有任何 membership，而所有权限都从 membership 推导 ——
        结果是账号能登录但什么都干不了。

        副作用是「默认团队」会承载所有未指定团队的新人。这是刻意的，
        比「静默给一个空权限账号」和「直接拒绝注册」都好：
        管理员随后调岗即可，而调岗只需要改 membership.role。
        """
        team = self.store.find_team_by_slug(DEFAULT_TEAM_SLUG)
        if team is not None:
            return str(team["id"])
        team_id = new_id("tm")
        self.store.create_team(team_id, DEFAULT_TEAM_SLUG, DEFAULT_TEAM_NAME, now=now)
        return team_id

    def _ensure_baseline_membership(
        self, *, user_id: str, role: str, team_id: str | None, now: int
    ) -> str:
        """保证新账号至少有一条 membership。返回实际使用的 team_id。"""
        if self.store.list_memberships(user_id):
            return team_id or ""
        target = team_id or self._default_team_id(now=now)
        effective_role = role if rbac.role_exists(role) else rbac.ROLE_DEVELOPER
        if effective_role == rbac.ROLE_OWNER:
            # 注册令牌不该能造出所有者：那等于把提权做成了一个自服务动作
            effective_role = rbac.ROLE_DEVELOPER
        self.store.add_membership(new_id("mem"), user_id, target, effective_role, now=now)
        return target

    def register_with_enrollment(
        self,
        *,
        enrollment_token: str,
        email: str,
        password: str,
        display_name: str = "",
        device_name: str = "",
        platform: str = "",
        fingerprint: str = "",
        client_ip: str = "",
    ) -> dict:
        """用一次性注册令牌开通账号并直接登录。

        注册令牌是内网分发的准入凭据（由管理员签发、可限次、可指定团队与角色）。
        """
        now = self.clock.now()
        raw = (enrollment_token or "").strip()
        if not raw:
            raise BadRequest("缺少注册令牌")

        row = self.store.find_enrollment_by_hash(tokens.hash_token(raw, pepper=self._pepper))
        if row is None:
            self.store.record_auth_event("enrollment.invalid", remote=client_ip, now=now)
            raise Unauthenticated("注册令牌无效")
        if row.get("revoked_at"):
            raise Unauthenticated("注册令牌已被吊销")
        if int(row.get("expires_at") or 0) < now:
            raise Unauthenticated("注册令牌已过期")
        if int(row.get("uses") or 0) >= int(row.get("max_uses") or 1):
            raise Unauthenticated("注册令牌已用尽")

        normalized = email.lower().strip()
        if not normalized or "@" not in normalized:
            raise BadRequest("邮箱格式不正确")
        if self.store.find_user_by_email(normalized):
            raise Conflict("该邮箱已注册")
        passwords.check_strength(password, email=normalized)

        # 先占额度再建用户：反过来的话，令牌用尽与建号成功之间有个窗口，
        # 并发请求会重复建号。占用失败就直接拒。
        if not self.store.consume_enrollment(str(row["id"]), now):
            raise Unauthenticated("注册令牌已用尽")

        user_id = new_id("usr")
        self.store.create_user(
            user_id,
            normalized,
            display_name or normalized.split("@")[0],
            passwords.hash_password(password),
            now=now,
        )

        team_id = row.get("team_id")
        role = str(row.get("role") or rbac.ROLE_DEVELOPER)
        effective_team = self._ensure_baseline_membership(
            user_id=user_id,
            role=role,
            team_id=str(team_id) if team_id else None,
            now=now,
        )

        self.store.record_auth_event(
            "user.register",
            user_id=user_id,
            detail=f"role={role} team={effective_team}",
            remote=client_ip,
            now=now,
        )
        return self.issue_session(
            user_id=user_id,
            email=normalized,
            device_name=device_name,
            platform=platform,
            fingerprint=fingerprint,
            client_ip=client_ip,
        )

    def login(
        self,
        *,
        email: str,
        password: str,
        device_name: str = "",
        platform: str = "",
        fingerprint: str = "",
        client_ip: str = "",
    ) -> dict:
        now = self.clock.now()
        normalized = (email or "").lower().strip()
        user = self.store.find_user_by_email(normalized) if normalized else None

        # 用户不存在时也走一遍哈希，抹平时间差（见文件头注释第 2 条）
        stored_hash = str(user["password_hash"]) if user else _DUMMY_HASH
        ok = passwords.verify_password(password, stored_hash)

        if user is None or not ok:
            self.store.record_auth_event(
                "login.failure",
                user_id=str(user["id"]) if user else "",
                detail=f"email={normalized}",
                remote=client_ip,
                now=now,
            )
            raise Unauthenticated("邮箱或口令不正确")

        status = str(user.get("status") or "active")
        if status != "active":
            self.store.record_auth_event(
                "login.blocked", user_id=str(user["id"]), detail=status, remote=client_ip, now=now
            )
            raise Forbidden(f"账号状态为 {status}，无法登录")

        # 参数落后就顺手升级（迭代次数调高之后，老用户不该一直用弱参数）
        if passwords.needs_rehash(stored_hash):
            self.store.update_user(
                str(user["id"]), password_hash=passwords.hash_password(password), now=now
            )

        return self.issue_session(
            user_id=str(user["id"]),
            email=str(user["email"]),
            device_name=device_name,
            platform=platform,
            fingerprint=fingerprint,
            client_ip=client_ip,
        )

    # ============================================================== 会话签发
    def issue_session(
        self,
        *,
        user_id: str,
        email: str,
        device_name: str = "",
        platform: str = "",
        fingerprint: str = "",
        client_ip: str = "",
    ) -> dict:
        now = self.clock.now()
        memberships = self.store.list_memberships(user_id)
        roles = [str(m["role"]) for m in memberships]

        # 同指纹的设备复用，避免每次登录都在设备列表里堆一条
        device_id = ""
        if fingerprint:
            for row in self.store.list_devices(user_id):
                if str(row.get("fingerprint") or "") == fingerprint and not row.get("revoked_at"):
                    device_id = str(row["id"])
                    break
        if not device_id:
            device_id = new_id("dev")
            self.store.create_device(
                device_id,
                user_id,
                device_name or "未命名设备",
                platform or "unknown",
                fingerprint,
                now=now,
            )
        self.store.touch_device(device_id, now=now)

        access, jti, expires_at = tokens.issue_access_token(
            secret=self.config.jwt_secret,
            issuer=self.config.jwt_issuer,
            audience=self.config.jwt_audience,
            user_id=user_id,
            email=email,
            roles=roles,
            device_id=device_id,
            ttl=self.config.access_ttl,
            now=now,
        )

        family_id = new_id("fam")
        refresh_raw = self._persist_refresh(
            user_id=user_id,
            device_id=device_id,
            family_id=family_id,
            client_ip=client_ip,
            now=now,
        )

        self.store.record_auth_event(
            "login.success", user_id=user_id, detail=f"device={device_id}", remote=client_ip, now=now
        )
        return self._session_payload(
            access=access,
            access_expires=expires_at,
            refresh=refresh_raw,
            user_id=user_id,
            email=email,
            device_id=device_id,
            jti=jti,
            memberships=memberships,
        )

    def _persist_refresh(
        self, *, user_id: str, device_id: str, family_id: str, client_ip: str, now: int
    ) -> str:
        raw = tokens.new_refresh_token()
        self.store.create_refresh_token(
            new_id("rft"),
            device_id,
            user_id,
            family_id,
            tokens.hash_token(raw, pepper=self._pepper),
            now,
            now + self.config.refresh_ttl,
            client_ip=client_ip,
        )
        return raw

    def _session_payload(
        self,
        *,
        access: str,
        access_expires: int,
        refresh: str,
        user_id: str,
        email: str,
        device_id: str,
        jti: str,
        memberships: list[dict],
    ) -> dict:
        actor = rbac.build_actor(
            user_id=user_id,
            email=email,
            display_name="",
            memberships=memberships,
            device_id=device_id,
            token_id=jti,
        )
        return {
            "accessToken": access,
            "refreshToken": refresh,
            "tokenType": "Bearer",
            "expiresIn": max(0, access_expires - self.clock.now()),
            "expiresAt": access_expires,
            "deviceId": device_id,
            "user": {
                "id": user_id,
                "email": email,
                "roles": list(actor.roles),
                "orgWide": actor.is_org_wide,
                "teams": [
                    {"id": m.team_id, "slug": m.team_slug, "name": m.team_name, "role": m.role}
                    for m in actor.memberships
                ],
            },
        }

    # ============================================================== 刷新
    def refresh(self, *, refresh_token: str, client_ip: str = "") -> dict:
        now = self.clock.now()
        raw = (refresh_token or "").strip()
        if not raw:
            raise Unauthenticated("缺少刷新令牌")

        row = self.store.find_refresh_by_hash(tokens.hash_token(raw, pepper=self._pepper))
        if row is None:
            self.store.record_auth_event("refresh.unknown", remote=client_ip, now=now)
            raise Unauthenticated("刷新令牌无效")

        token_id = str(row["id"])
        family_id = str(row["family_id"])
        user_id = str(row["user_id"])
        device_id = str(row["device_id"])

        # --- 重用检测：用过的令牌再次出现，整族作废 ---
        if row.get("used_at"):
            affected = self.store.revoke_refresh_family(family_id, now)
            self.store.record_auth_event(
                "refresh.reuse_detected",
                user_id=user_id,
                detail=f"family={family_id} revoked={affected}",
                remote=client_ip,
                now=now,
            )
            # 连带把该设备作废：令牌泄漏了，这条设备链不再可信
            self.store.revoke_device(device_id, now=now)
            raise Unauthenticated("检测到刷新令牌被重复使用，该设备的全部会话已失效，请重新登录")

        if row.get("revoked_at"):
            raise Unauthenticated("刷新令牌已失效")
        if int(row.get("expires_at") or 0) < now:
            raise Unauthenticated("刷新令牌已过期")

        device = self.store.get_device(device_id)
        if device is None or device.get("revoked_at"):
            raise Unauthenticated("设备已被撤销，请重新登录")

        user = self.store.get_user(user_id)
        if user is None or str(user.get("status") or "") != "active":
            raise Unauthenticated("账号不可用")

        # --- 轮换：发新令牌，作废旧的（同一族）---
        new_raw = tokens.new_refresh_token()
        new_id_value = new_id("rft")
        self.store.create_refresh_token(
            new_id_value,
            device_id,
            user_id,
            family_id,
            tokens.hash_token(new_raw, pepper=self._pepper),
            now,
            now + self.config.refresh_ttl,
            client_ip=client_ip,
        )
        self.store.mark_refresh_used(token_id, now, new_id_value)

        memberships = self.store.list_memberships(user_id)
        roles = [str(m["role"]) for m in memberships]
        access, jti, expires_at = tokens.issue_access_token(
            secret=self.config.jwt_secret,
            issuer=self.config.jwt_issuer,
            audience=self.config.jwt_audience,
            user_id=user_id,
            email=str(user["email"]),
            roles=roles,
            device_id=device_id,
            ttl=self.config.access_ttl,
            now=now,
        )
        self.store.touch_device(device_id, now=now)

        return self._session_payload(
            access=access,
            access_expires=expires_at,
            refresh=new_raw,
            user_id=user_id,
            email=str(user["email"]),
            device_id=device_id,
            jti=jti,
            memberships=memberships,
        )

    # ============================================================== 登出 / 撤销
    def logout(self, *, access_token: str, refresh_token: str = "", all_devices: bool = False) -> dict:
        now = self.clock.now()
        claims = None
        if access_token:
            try:
                # 必须传 `now`，用**服务自己的时钟**，与其他所有路径保持一致。
                # 不传的话会退回进程墙钟；两者一旦不一致（测试注入时钟、
                # 容器与宿主机时钟漂移、NTP 校时中途跳变），一个本来有效的
                # 访问令牌会被判成「已过期」→ jti 进不了吊销名单 → 登出形同虚设，
                # 而接口返回的还是 revoked=true。
                claims = tokens.verify_access_token(
                    access_token,
                    secret=self.config.jwt_secret,
                    issuer=self.config.jwt_issuer,
                    audience=self.config.jwt_audience,
                    now=now,
                )
            except Unauthenticated:
                # 校验失败（真过期 / 签名不对）：这个令牌本来就无法再通过
                # `authenticate()`，也就没有「提前吊销」可言。
                # 刻意不去解析未经验证的载荷来捞 jti —— 那等于允许任何人
                # 用一个自签令牌吊销别人的会话。
                claims = None

        revoked_jti = ""
        if claims is not None and claims.jti:
            self.store.revoke_access_jti(claims.jti, claims.expires_at, now)
            revoked_jti = claims.jti

        user_id = claims.subject if claims else ""
        device_id = claims.device_id if claims else ""

        if refresh_token:
            row = self.store.find_refresh_by_hash(
                tokens.hash_token(refresh_token.strip(), pepper=self._pepper)
            )
            if row is not None:
                user_id = user_id or str(row["user_id"])
                if all_devices:
                    self.store.revoke_refresh_family(str(row["family_id"]), now)
                else:
                    self.store.revoke_refresh_token(str(row["id"]), now)

        if all_devices and user_id:
            for device in self.store.list_devices(user_id):
                if not device.get("revoked_at"):
                    self.store.revoke_device(str(device["id"]), now=now)

        if user_id:
            self.store.record_auth_event(
                "logout", user_id=user_id, detail=f"jti={revoked_jti}", now=now
            )
        return {"revoked": bool(revoked_jti or refresh_token), "allDevices": all_devices}

    def revoke_device(self, *, actor: rbac.Actor, device_id: str) -> dict:
        """撤销一台设备（同时吊销它所有刷新令牌）。

        权限：本人可撤自己的设备；团队管理员可撤同团队成员的设备。

        吊销动作由存储层的 `revoke_device` 一并级联到该设备的刷新令牌 ——
        这一点必须落在**一次事务**里，分两步做的话中间那个窗口里
        「设备已废但令牌还能续期」，攻击者刚好能用上。
        """
        device = self.store.get_device(device_id)
        if device is None:
            raise NotFound("设备不存在")
        owner = str(device["user_id"])
        if owner != actor.user_id:
            actor.scope_user(owner, rbac.PERM_DEVICE_REVOKE)
        now = self.clock.now()
        self.store.revoke_device(device_id, now=now)
        self.store.record_auth_event(
            "device.revoke", user_id=owner, detail=f"device={device_id} by={actor.user_id}", now=now
        )
        return {"deviceId": device_id, "revoked": True}

    # ============================================================== SSO 登录
    def login_via_oidc(
        self,
        *,
        claims,
        device_name: str = "",
        platform: str = "",
        fingerprint: str = "",
        client_ip: str = "",
    ) -> dict:
        """用已验签的 ID Token 完成登录（首次登录自动开通账号）。

        三个刻意的选择：

        1. **要求 email_verified。** 未验证的邮箱意味着「随便填一个就能占住」，
           而我们会用邮箱做账号合并 —— 不校验就等于把账号接管的口子留着。
           有些 IdP 在纯内网场景下不返回这个字段，所以只在**明确为 False** 时拒，
           缺失时放行（并记一条审计）。
        2. **按 external_id 优先匹配，其次邮箱。** 用户改了邮箱不该变成新账号；
           但换了 IdP 的人（external_id 变了）该能按邮箱接上原账号。
        3. **不自动提权。** 首次登录建的是 developer + 无团队，不是 admin。
           SSO 只证明「你是谁」，不证明「你该有多大权限」。
        """
        now = self.clock.now()
        subject = str(getattr(claims, "subject", "") or "")
        email = str(getattr(claims, "email", "") or "").lower().strip()
        verified = getattr(claims, "email_verified", False)

        if not subject:
            raise Unauthenticated("ID Token 缺少 sub，无法建立身份")
        if verified is False:
            raise Forbidden("IdP 报告该邮箱未验证，拒绝登录")
        if not email:
            # 没有邮箱时用 sub 造一个占位，保证账号可建；但会记在审计里
            email = f"{subject}@sso.invalid"

        user = self.store.find_user_by_external_id(subject) or self.store.find_user_by_email(email)
        created = False
        if user is None:
            user_id = new_id("usr")
            self.store.create_user(
                user_id,
                email,
                str(getattr(claims, "name", "") or email.split("@")[0]),
                "",  # SSO 用户没有本地口令；password_hash 为空即不可用口令登录
                external_id=subject,
                now=now,
            )
            created = True
            # 同 bootstrap：不给 membership 的话，SSO 登录进来是个"什么都干不了"的账号
            self._ensure_baseline_membership(
                user_id=user_id,
                role=rbac.ROLE_DEVELOPER,
                team_id=None,
                now=now,
            )
            user = self.store.get_user(user_id)
            self.store.record_auth_event(
                "sso.provision", user_id=user_id, detail=f"sub={subject[:24]}", now=now
            )
        elif str(user.get("external_id") or "") != subject:
            # 按邮箱接上了老账号：补 external_id，后续就走快速路径
            self.store.update_user(str(user["id"]), external_id=subject, now=now)

        if user is None:
            raise Unauthenticated("账号建立失败")
        if str(user.get("status") or "") != "active":
            raise Forbidden(f"账号状态为 {user.get('status')}，无法登录")

        self.store.record_auth_event(
            "login.sso",
            user_id=str(user["id"]),
            detail=f"created={created}",
            remote=client_ip,
            now=now,
        )
        return self.issue_session(
            user_id=str(user["id"]),
            email=str(user["email"]),
            device_name=device_name,
            platform=platform,
            fingerprint=fingerprint,
            client_ip=client_ip,
        )

    # ============================================================== 校验（鉴权入口）
    def authenticate(self, token: str, request=None) -> rbac.Actor:
        """HTTP 层调用的认证钩子。返回 Actor 或抛 Unauthenticated。"""
        now = self.clock.now()
        claims = tokens.verify_access_token(
            token,
            secret=self.config.jwt_secret,
            issuer=self.config.jwt_issuer,
            audience=self.config.jwt_audience,
            now=now,
        )

        if claims.jti and self.store.is_access_jti_revoked(claims.jti):
            raise Unauthenticated("令牌已被吊销")

        user = self.store.get_user(claims.subject)
        if user is None:
            raise Unauthenticated("账号不存在")
        if str(user.get("status") or "") != "active":
            raise Unauthenticated("账号不可用")

        if claims.device_id:
            device = self.store.get_device(claims.device_id)
            if device is None or device.get("revoked_at"):
                raise Unauthenticated("设备已被撤销")

        memberships = self.store.list_memberships(claims.subject)
        return rbac.build_actor(
            user_id=claims.subject,
            email=str(user.get("email") or claims.email),
            display_name=str(user.get("display_name") or ""),
            memberships=memberships,
            device_id=claims.device_id,
            token_id=claims.jti,
        )

    # ============================================================== 团队/用户管理
    def create_team(self, *, actor: rbac.Actor, slug: str, name: str) -> dict:
        rbac.require_permission(actor, rbac.PERM_TEAM_WRITE)
        if not actor.is_org_wide:
            raise Forbidden("只有组织所有者可以创建团队")
        slug = (slug or "").strip().lower()
        if not slug or not slug.replace("-", "").replace("_", "").isalnum():
            raise BadRequest("团队标识只能包含字母、数字、连字符与下划线")
        if self.store.find_team_by_slug(slug):
            raise Conflict("团队标识已存在")
        now = self.clock.now()
        team_id = new_id("tm")
        self.store.create_team(team_id, slug, name or slug, now=now)
        # 创建者自动成为该团队的管理员
        self.store.add_membership(
            new_id("mem"), actor.user_id, team_id, rbac.ROLE_TEAM_ADMIN, now=now
        )
        return {"teamId": team_id, "slug": slug, "name": name or slug}

    def add_team_member(
        self, *, actor: rbac.Actor, team_id: str, user_id: str, role: str
    ) -> dict:
        rbac.require_permission(actor, rbac.PERM_TEAM_WRITE)
        if not rbac.role_exists(role):
            raise BadRequest(f"未知角色 {role}")
        if role == rbac.ROLE_OWNER:
            # 提权到 owner 等于给人全组织权限，不能由团队管理员顺手完成
            raise Forbidden("不能通过团队成员接口授予 owner 角色")
        actor.scope_team(team_id, rbac.PERM_TEAM_WRITE)

        target = self.store.get_user(user_id)
        if target is None:
            raise NotFound("用户不存在")
        if self.store.get_membership(user_id, team_id):
            raise Conflict("该用户已是团队成员")

        now = self.clock.now()
        self.store.add_membership(new_id("mem"), user_id, team_id, role, now=now)
        self.store.record_auth_event(
            "team.member_add",
            user_id=user_id,
            detail=f"team={team_id} role={role} by={actor.user_id}",
            now=now,
        )
        return {"teamId": team_id, "userId": user_id, "role": role}

    def remove_team_member(self, *, actor: rbac.Actor, team_id: str, user_id: str) -> dict:
        rbac.require_permission(actor, rbac.PERM_TEAM_WRITE)
        actor.scope_team(team_id, rbac.PERM_TEAM_WRITE)
        if user_id == actor.user_id:
            raise BadRequest("不能将自己移出团队，请让其他管理员操作")
        if not self.store.remove_membership(user_id, team_id):
            raise NotFound("该用户不在团队中")
        self.store.record_auth_event(
            "team.member_remove",
            user_id=user_id,
            detail=f"team={team_id} by={actor.user_id}",
            now=self.clock.now(),
        )
        return {"teamId": team_id, "userId": user_id, "removed": True}

    # ============================================================== 注册令牌
    def create_enrollment(
        self,
        *,
        actor: rbac.Actor,
        team_id: str = "",
        role: str = rbac.ROLE_DEVELOPER,
        max_uses: int = 1,
        ttl: int = 0,
        note: str = "",
    ) -> dict:
        rbac.require_permission(actor, rbac.PERM_ENROLLMENT_WRITE)
        if not rbac.role_exists(role):
            raise BadRequest(f"未知角色 {role}")
        if role in (rbac.ROLE_OWNER, rbac.ROLE_TEAM_ADMIN) and not actor.is_org_wide:
            raise Forbidden("只有组织所有者可以签发管理员级注册令牌")
        if team_id:
            actor.scope_team(team_id, rbac.PERM_ENROLLMENT_WRITE)

        max_uses = max(1, min(int(max_uses or 1), 100))
        now = self.clock.now()
        ttl = int(ttl) if int(ttl) > 0 else self.config.enrollment_ttl
        raw = tokens.new_enrollment_token()
        enrollment_id = new_id("enr")
        self.store.create_enrollment(
            enrollment_id,
            tokens.hash_token(raw, pepper=self._pepper),
            team_id=team_id or None,
            role=role,
            created_by=actor.user_id,
            now=now,
            expires_at=now + ttl,
            max_uses=max_uses,
        )
        self.store.record_auth_event(
            "enrollment.create",
            user_id=actor.user_id,
            detail=f"team={team_id} role={role} uses={max_uses} note={note[:60]}",
            now=now,
        )
        # 明文只在这里出现一次，服务端只存哈希
        return {
            "enrollmentToken": raw,
            "teamId": team_id,
            "role": role,
            "maxUses": max_uses,
            "expiresAt": now + ttl,
        }

    # ============================================================== 自省
    def list_devices(self, *, actor: rbac.Actor, user_id: str = "") -> list[dict]:
        rbac.require_permission(actor, rbac.PERM_DEVICE_READ)
        target = user_id or actor.user_id
        actor.scope_user(target, rbac.PERM_DEVICE_READ)
        return [
            {
                "id": str(d["id"]),
                "name": str(d.get("name") or ""),
                "platform": str(d.get("platform") or ""),
                "createdAt": int(d.get("created_at") or 0),
                "lastSeenAt": int(d.get("last_seen_at") or 0),
                "revoked": bool(d.get("revoked_at")),
            }
            for d in self.store.list_devices(target)
        ]

    def list_org_users(self, *, actor: rbac.Actor, limit: int = 100, offset: int = 0) -> list[dict]:
        rbac.require_permission(actor, rbac.PERM_USER_READ)
        rows = self.store.list_users(limit=limit, offset=offset)
        if actor.is_org_wide:
            visible = rows
        else:
            mine = set(actor.team_ids)
            visible = []
            for row in rows:
                uid = str(row["id"])
                if uid == actor.user_id:
                    visible.append(row)
                    continue
                if mine & set(self.store_teams_of(uid)):
                    visible.append(row)
        return [
            {
                "id": str(r["id"]),
                "email": str(r["email"]),
                "displayName": str(r.get("display_name") or ""),
                "status": str(r.get("status") or ""),
                "createdAt": int(r.get("created_at") or 0),
            }
            for r in visible
        ]

    def store_teams_of(self, user_id: str) -> list[str]:
        return [str(m["team_id"]) for m in self.store.list_memberships(user_id)]


__all__ = ["AuthService"]
