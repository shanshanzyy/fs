"""PostgreSQL v3 协议的最小客户端（**纯标准库**）。

为什么自己写协议而不是引 `psycopg`：
服务端沿用至今的红线是「**零第三方依赖、内网离线可跑**」。多实例部署需要一个
共享存储，而 `postgres://` 一旦交给第三方驱动，红线就破了。所以这里只实现
**服务端真正用到的一个子集**，不做通用客户端：

- startup（固定 `client_encoding=UTF8`）
- 认证：`SCRAM-SHA-256`（PG 10+ 默认）与 `MD5`；**明确拒绝**明文口令
- 简单查询（`Q`）：仅用于 `init_schema` 这种**无参数**的多语句 DDL
- 扩展查询（`P`/`B`/`D`/`E`/`S`）：**参数绑定的唯一入口**，一律用 `$1` 占位，
  不做任何字符串拼接 —— 这是防注入的根本做法，而不是「记得转义」
- 文本格式结果 + 保守的类型映射（INT8/INT4/INT2→int、BOOL→bool、其余→str、NULL→None）

安全上的三条底线（对应 M4+-PLAN §B）：

1. **不回传明文口令**：遇到 `AuthenticationCleartextPassword` 直接失败，
   宁可连不上，也不把口令明文丢到网络上。
2. **不静默降级**：`sslmode=require` 时服务端不支持 TLS 就直接失败；
   `prefer` 只在服务端明确拒绝 TLS（回 `N`）时才退回明文，TLS 握手失败会报错
   而不是偷偷改成明文。
3. **错误只暴露元信息**：`ErrorResponse` 只取 SQLSTATE / severity / message / hint，
   原始报文（可能含数据行）**不进日志**。

并发模型：**每线程一条连接**（`threading.local`）。PG 连接不是线程安全的，
而 HTTP 层是多线程的 —— 共享一条连接会随机出现「协议乱序」这种最难查的故障。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import ssl
import struct
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..errors import ConfigError, StoreError

# ============================================================================ 常量
_PROTOCOL_VERSION = 3 << 16          # 3.0
_SSL_REQUEST_CODE = 80877103         # 请求升级为 TLS 的魔数

# SSLRequest / CancelRequest 之外，前端消息都带 1 字节类型 + Int32 长度。

# 类型 OID（只列用得到的）
OID_BOOL = 16
OID_BYTEA = 17
OID_CHAR = 18
OID_NAME = 19
OID_INT8 = 20
OID_INT2 = 21
OID_INT4 = 23
OID_TEXT = 25
OID_FLOAT4 = 700
OID_FLOAT8 = 701
OID_VARCHAR = 1043
OID_BPCHAR = 1042
OID_NUMERIC = 1700
OID_TIMESTAMPTZ = 1184

_INT_OIDS = {OID_INT2, OID_INT4, OID_INT8}

# sslmode：接受 libpq 的常见取值。本项目只区分「要不要 TLS」与「服务端拒绝时能否退回」。
_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
_TLS_REQUIRED = {"require", "verify-ca", "verify-full"}

_POSTGRES_SCHEMES = {"postgres", "postgresql", "psql"}

# 幂等建表需要容忍的 SQLSTATE（并发建表竞态）
SQLSTATE_DUPLICATE_TABLE = "42P07"
SQLSTATE_DUPLICATE_OBJECT = "42710"
_IDEMPOTENT_DDL_STATES = {SQLSTATE_DUPLICATE_TABLE, SQLSTATE_DUPLICATE_OBJECT}


# ============================================================================ 异常
class PGError(StoreError):
    """数据库返回的错误（`ErrorResponse` 映射而来）。

    `sqlstate` 单独暴露，供上层做幂等判定（如 `duplicate_table`）。
    **不携带原始报文**：报文里可能有行数据，日志里不该出现。
    """

    def __init__(self, message: str, *, sqlstate: str = "", severity: str = "") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.severity = severity


class PGConnectionError(StoreError):
    """连接层面的故障（建连失败 / 对端断开 / 读写超时）。

    与 `PGError` 分开：前者可以「重连一次再试」，后者是语句本身的问题，重连无用。
    """


# ============================================================================ 编解码
def _i16(value: int) -> bytes:
    return struct.pack("!h", value)


def _i32(value: int) -> bytes:
    return struct.pack("!i", value)


def _u16(value: int) -> bytes:
    return struct.pack("!H", value)


def _u32(value: int) -> bytes:
    return struct.pack("!I", value)


def _frame(type_byte: bytes, payload: bytes) -> bytes:
    """前端消息：1 字节类型 + Int32 长度（含长度本身，不含类型）+ 载荷。"""
    return type_byte + _u32(len(payload) + 4) + payload


def split_statements(sql: str) -> list[str]:
    """把一段多语句 SQL 拆成单条（剥掉 `--` 行注释，尊重单引号字符串）。

    为什么需要：扩展查询协议一次只能 `Parse` **一条**语句，而 `schema.POSTGRES`
    是一整段 DDL。拆分后逐条执行，才能对单条 `duplicate_table` 做幂等容忍。
    只处理本项目 DDL 用到的语法（行注释 + 单引号），不追求通用 SQL 解析器。
    """
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            if end == -1:
                break
            i = end + 1
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":   # '' 转义
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _parse_error_fields(payload: bytes) -> dict[str, str]:
    """`ErrorResponse` / `NoticeResponse` 的字段列表：若干 (1 字节码 + cstring)。"""
    fields: dict[str, str] = {}
    i = 0
    n = len(payload)
    while i < n and payload[i] != 0:
        code = chr(payload[i])
        i += 1
        end = payload.find(b"\x00", i)
        if end == -1:
            end = n
        fields.setdefault(code, payload[i:end].decode("utf-8", "replace"))
        i = end + 1
    return fields


def _error_from_response(payload: bytes) -> PGError:
    fields = _parse_error_fields(payload)
    severity = fields.get("S", "ERROR")
    message = fields.get("M", "数据库返回未知错误")
    hint = fields.get("H", "")
    text = f"{severity}: {message}"
    if hint:
        text += f"（提示：{hint}）"
    return PGError(text, sqlstate=fields.get("C", ""), severity=severity)


def _parse_row_description(payload: bytes) -> list[tuple[str, int]]:
    """返回 [(列名, 类型 OID), ...]。"""
    (count,) = struct.unpack_from("!h", payload, 0)
    offset = 2
    fields: list[tuple[str, int]] = []
    for _ in range(count):
        end = payload.find(b"\x00", offset)
        if end == -1:
            raise PGConnectionError("RowDescription 报文损坏（列名未终止）")
        name = payload[offset:end].decode("utf-8", "replace")
        offset = end + 1
        # table OID(I) | 列号(h) | 类型 OID(I) | 类型长度(h) | 类型修饰(i) | 格式码(h)
        _, _, type_oid, _, _, _ = struct.unpack_from("!IhIhih", payload, offset)
        offset += 18
        fields.append((name, type_oid))
    return fields


def _parse_data_row(payload: bytes) -> list[bytes | None]:
    (count,) = struct.unpack_from("!h", payload, 0)
    offset = 2
    values: list[bytes | None] = []
    for _ in range(count):
        (length,) = struct.unpack_from("!i", payload, offset)
        offset += 4
        if length == -1:
            values.append(None)
        else:
            values.append(payload[offset:offset + length])
            offset += length
    return values


def _decode_value(type_oid: int, raw: bytes | None) -> Any:
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    if type_oid == OID_BOOL:
        return text in ("t", "true", "1")
    if type_oid in _INT_OIDS:
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _encode_param(value: Any) -> bytes | None:
    """参数一律以**文本格式**发送；`None` 表示 SQL NULL。"""
    if value is None:
        return None
    if isinstance(value, bool):           # 必须先于 int：bool 是 int 的子类
        return b"true" if value else b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, float):
        return repr(value).encode("ascii")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode("utf-8")


@dataclass
class PGResult:
    """一次扩展查询的结果。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    command: str = ""
    rowcount: int = 0


# ============================================================================ DSN
@dataclass(frozen=True)
class PGDSN:
    host: str
    port: int
    user: str
    password: str
    database: str
    sslmode: str = "prefer"
    connect_timeout: float = 10.0
    application_name: str = "vibe-governance"

    def label(self) -> str:
        """给日志/健康检查用的**不含口令**的描述。"""
        return f"{self.host}:{self.port}/{self.database}"


def parse_dsn(dsn: str) -> PGDSN:
    """解析 `postgres://user:pass@host:port/db?sslmode=...`。

    只支持 URL 形式（与 `open_store()` 的入口一致）；不做 libpq 的 key=value 解析。
    口令做百分号解码，且**永不回显**。
    """
    try:
        parsed = urlsplit(dsn)
        host = parsed.hostname or ""
        port = parsed.port or 5432
    except ValueError as exc:
        raise ConfigError(f"postgres DSN 不合法: {exc}") from exc

    if parsed.scheme.lower() not in _POSTGRES_SCHEMES:
        raise ConfigError(
            f"不是 postgres DSN（scheme={parsed.scheme!r}）；"
            f"支持: {', '.join(sorted(_POSTGRES_SCHEMES))}"
        )
    if not host:
        raise ConfigError("postgres DSN 缺少主机名，形如 postgres://user:pass@host:5432/db")

    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.lstrip("/")) or user

    query = parse_qs(parsed.query)
    sslmode = (query.get("sslmode", ["prefer"])[0] or "prefer").lower()
    if sslmode not in _SSLMODES:
        raise ConfigError(
            f"不支持的 sslmode={sslmode!r}，可选: {', '.join(sorted(_SSLMODES))}"
        )
    try:
        timeout = float(query.get("connect_timeout", ["10"])[0])
    except ValueError as exc:
        raise ConfigError("connect_timeout 需要是数字（秒）") from exc

    app_name = query.get("application_name", ["vibe-governance"])[0] or "vibe-governance"

    return PGDSN(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        sslmode=sslmode,
        connect_timeout=max(0.1, timeout),
        application_name=app_name,
    )


# ============================================================================ SCRAM
def _parse_scram(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key] = value
    return out


class _ScramSession:
    """SCRAM-SHA-256 客户端一侧的状态机。

    PG 的 SCRAM 与 RFC 5802 一致，只是用户名不参与（用 startup 里的 user），
    因此 client-first-bare 的用户名段留空：`n=,r=<nonce>`。
    """

    def __init__(self, password: str, *, nonce: str | None = None) -> None:
        self._password = password.encode("utf-8")
        self._client_nonce = nonce or base64.b64encode(secrets.token_bytes(18)).decode("ascii")
        self._client_first_bare = f"n=,r={self._client_nonce}"
        self._auth_message = ""
        self._salted = b""

    @property
    def client_first(self) -> str:
        return "n,," + self._client_first_bare

    def process_server_first(self, server_first: str) -> str:
        attrs = _parse_scram(server_first)
        if "e" in attrs:
            raise PGError(f"SCRAM 认证失败: {attrs['e']}")
        nonce = attrs.get("r", "")
        salt_b64 = attrs.get("s", "")
        iteration = attrs.get("i", "")
        if not nonce or not salt_b64 or not iteration:
            raise PGError("SCRAM 服务端首条消息缺少 r/s/i 字段")
        if not nonce.startswith(self._client_nonce):
            raise PGError("SCRAM 服务端 nonce 未以客户端 nonce 开头，疑似被中间人篡改")
        try:
            salt = base64.b64decode(salt_b64, validate=True)
            iterations = int(iteration)
        except (ValueError, TypeError) as exc:
            raise PGError(f"SCRAM 服务端首条消息字段不合法: {exc}") from exc

        self._salted = hashlib.pbkdf2_hmac("sha256", self._password, salt, iterations)
        client_final_without_proof = f"c=biws,r={nonce}"
        self._auth_message = (
            f"{self._client_first_bare},{server_first},{client_final_without_proof}"
        )
        client_key = hmac.new(self._salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        client_signature = hmac.new(
            stored_key, self._auth_message.encode("utf-8"), hashlib.sha256
        ).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, client_signature))
        return f"{client_final_without_proof},p={base64.b64encode(proof).decode('ascii')}"

    def verify_server_final(self, server_final: str) -> None:
        attrs = _parse_scram(server_final)
        if "e" in attrs:
            raise PGError(f"SCRAM 认证失败: {attrs['e']}")
        signature_b64 = attrs.get("v", "")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise PGError("SCRAM 服务端末条消息缺少合法的 v 字段") from exc
        server_key = hmac.new(self._salted, b"Server Key", hashlib.sha256).digest()
        expected = hmac.new(
            server_key, self._auth_message.encode("utf-8"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise PGError("SCRAM 服务端签名校验失败，口令或链路不可信")


# ============================================================================ 连接
class PGConnection:
    """一条到 Postgres 的连接。非线程安全 —— 由 `PGPool` 保证一线程一条。"""

    def __init__(self, dsn: PGDSN) -> None:
        self.dsn = dsn
        self._sock: socket.socket | ssl.SSLSocket | None = None
        self._closed = True
        self._params: dict[str, str] = {}
        self._fields: list[tuple[str, int]] = []

    # ------------------------------------------------------------ 属性
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def server_version(self) -> str:
        return self._params.get("server_version", "")

    # ------------------------------------------------------------ 生命周期
    def connect(self) -> None:
        if not self._closed:
            return
        sock = self._open_socket()
        self._sock = sock
        try:
            self._send_startup()
            self._authenticate()
        except BaseException:
            self._hard_close()
            raise
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send(_frame(b"X", b""))
        except Exception:  # noqa: BLE001 - 关闭时失败无所谓
            pass
        self._hard_close()

    def _hard_close(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------ 建 socket
    def _open_socket(self) -> socket.socket | ssl.SSLSocket:
        mode = "allow" if self.dsn.sslmode == "allow" else self.dsn.sslmode
        try:
            sock = socket.create_connection(
                (self.dsn.host, self.dsn.port), timeout=self.dsn.connect_timeout
            )
        except OSError as exc:
            raise PGConnectionError(
                f"无法连接 Postgres {self.dsn.label()}: {exc}"
            ) from exc

        if mode == "disable":
            sock.settimeout(None)
            return sock

        try:
            sock.sendall(_u32(8) + _u32(_SSL_REQUEST_CODE))
            answer = self._recv_exact(sock, 1)
        except OSError as exc:
            sock.close()
            raise PGConnectionError(f"Postgres TLS 协商失败: {exc}") from exc

        if answer == b"S":
            # 用默认上下文（**带证书校验**）。服务端同意加密后握手失败，
            # 直接报错而不退回明文 —— 否则就是给降级攻击留门。
            context = ssl.create_default_context()
            try:
                wrapped = context.wrap_socket(sock, server_hostname=self.dsn.host)
            except ssl.SSLError as exc:
                sock.close()
                raise PGConnectionError(
                    f"Postgres TLS 握手失败（sslmode={self.dsn.sslmode}）: {exc}"
                ) from exc
            wrapped.settimeout(None)
            return wrapped

        if answer == b"N":
            if mode in _TLS_REQUIRED:
                sock.close()
                raise PGConnectionError(
                    f"sslmode={self.dsn.sslmode} 要求加密，但服务端不支持 TLS。"
                    "不会退回明文 —— 请为 Postgres 启用 TLS，或显式改用 sslmode=disable。"
                )
            sock.settimeout(None)
            return sock

        sock.close()
        raise PGConnectionError(f"Postgres 对 SSLRequest 返回了意外应答: {answer!r}")

    # ------------------------------------------------------------ 收发
    def _recv_exact(self, sock: Any, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise PGConnectionError("Postgres 连接已关闭（对端断开）")
            buf.extend(chunk)
        return bytes(buf)

    def _read_message(self) -> tuple[bytes, bytes]:
        assert self._sock is not None
        try:
            header = self._recv_exact(self._sock, 5)
            type_byte = header[0:1]
            (length,) = struct.unpack_from("!i", header, 1)
            if length < 4:
                raise PGConnectionError(f"Postgres 报文长度非法: {length}")
            payload = self._recv_exact(self._sock, length - 4) if length > 4 else b""
        except OSError as exc:
            self._hard_close()
            raise PGConnectionError(f"读取 Postgres 响应失败: {exc}") from exc
        return type_byte, payload

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self._hard_close()
            raise PGConnectionError(f"发送 Postgres 请求失败: {exc}") from exc

    # ------------------------------------------------------------ startup
    def _send_startup(self) -> None:
        params = [
            ("user", self.dsn.user),
            ("database", self.dsn.database),
            ("client_encoding", "UTF8"),
            ("application_name", self.dsn.application_name),
        ]
        payload = _u32(_PROTOCOL_VERSION)
        for key, value in params:
            payload += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
        payload += b"\x00"
        # StartupMessage 没有类型字节
        self._send(_u32(len(payload) + 4) + payload)

    def _authenticate(self) -> None:
        scram: _ScramSession | None = None
        while True:
            type_byte, payload = self._read_message()
            if type_byte == b"R":
                (code,) = struct.unpack_from("!i", payload, 0)
                extra = payload[4:]
                if code == 0:                       # AuthenticationOk
                    continue
                if code == 3:                       # CleartextPassword
                    raise PGConnectionError(
                        "服务端要求明文口令（AuthenticationCleartextPassword）。"
                        "本项目拒绝以明文回传口令：请把 pg_hba.conf 的认证方式改为 "
                        "scram-sha-256（推荐）或 md5。"
                    )
                if code == 5:                       # MD5Password
                    self._send_md5(extra)
                    continue
                if code == 10:                      # SASL
                    scram = _ScramSession(self.dsn.password)
                    self._send_sasl_initial(scram, extra)
                    continue
                if code == 11:                      # SASLContinue
                    if scram is None:
                        raise PGConnectionError("收到 SASLContinue 但未开始 SASL 会话")
                    client_final = scram.process_server_first(extra.decode("utf-8", "replace"))
                    self._send(_frame(b"p", client_final.encode("utf-8")))
                    continue
                if code == 12:                      # SASLFinal
                    if scram is None:
                        raise PGConnectionError("收到 SASLFinal 但未开始 SASL 会话")
                    scram.verify_server_final(extra.decode("utf-8", "replace"))
                    continue
                raise PGConnectionError(f"不支持的认证方式（AuthenticationRequest code={code}）")
            if type_byte == b"E":
                raise _error_from_response(payload)
            if type_byte == b"Z":                   # ReadyForQuery
                self._closed = False
                return
            if type_byte == b"S":                   # ParameterStatus
                key, _, rest = payload.partition(b"\x00")
                value, _, _ = rest.partition(b"\x00")
                self._params[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
                continue
            if type_byte in (b"K", b"N"):           # BackendKeyData / NoticeResponse
                continue
            # 其余消息在启动阶段不该出现，忽略以求稳健
        # not reached

    def _send_md5(self, salt: bytes) -> None:
        inner = hashlib.md5(
            (self.dsn.password + self.dsn.user).encode("utf-8")
        ).hexdigest()
        outer = hashlib.md5(inner.encode("ascii") + salt).hexdigest()
        self._send(_frame(b"p", b"md5" + outer.encode("ascii")))

    def _send_sasl_initial(self, scram: _ScramSession, payload: bytes) -> None:
        mechanisms: list[str] = []
        for part in payload.split(b"\x00"):
            if part:
                mechanisms.append(part.decode("utf-8", "replace"))
        if "SCRAM-SHA-256" not in mechanisms:
            raise PGConnectionError(
                f"服务端未提供 SCRAM-SHA-256（提供: {mechanisms or '无'}）；"
                "本项目不支持其它机制。"
            )
        initial = scram.client_first.encode("utf-8")
        body = (
            b"SCRAM-SHA-256\x00"
            + _i32(len(initial))
            + initial
        )
        self._send(_frame(b"p", body))

    # ------------------------------------------------------------ 执行
    def exec_simple(self, sql: str) -> None:
        """简单查询协议：可含多条语句，**不支持参数**。仅用于 DDL 批量执行。"""
        self._send(_frame(b"Q", sql.encode("utf-8") + b"\x00"))
        self._read_until_ready()

    def exec_params(self, sql: str, params: tuple[Any, ...] = ()) -> PGResult:
        """扩展查询协议：一条语句 + `$1` 占位参数。**唯一的参数化入口**。"""
        if self._closed:
            raise PGConnectionError("Postgres 连接已关闭，无法执行语句")

        placeholders = b"".join(_u32(0) for _ in params)   # 参数类型交给服务端推断
        parse = (
            b"\x00"                                          # 未命名语句
            + sql.encode("utf-8") + b"\x00"
            + _u16(len(params)) + placeholders
        )
        self._send(_frame(b"P", parse))

        bind = bytearray()
        bind += b"\x00"                                      # 未命名 portal
        bind += b"\x00"                                      # 未命名语句
        bind += _u16(0)                                      # 所有参数用文本格式
        bind += _u16(len(params))
        for value in params:
            encoded = _encode_param(value)
            if encoded is None:
                bind += _i32(-1)
            else:
                bind += _i32(len(encoded)) + encoded
        bind += _u16(0)                                      # 所有结果用文本格式
        self._send(_frame(b"B", bytes(bind)))

        self._send(_frame(b"D", b"P\x00"))                   # Describe portal
        self._send(_frame(b"E", b"\x00" + _u32(0)))          # Execute：取全部行
        self._send(_frame(b"S", b""))                        # Sync

        return self._read_result()

    def _read_until_ready(self) -> None:
        error: PGError | None = None
        while True:
            type_byte, payload = self._read_message()
            if type_byte == b"E":
                if error is None:
                    error = _error_from_response(payload)
            elif type_byte == b"Z":
                break
            elif type_byte == b"R":                         # 认证类消息不该出现在这里
                continue
        if error is not None:
            raise error

    def _read_result(self) -> PGResult:
        result = PGResult()
        fields: list[tuple[str, int]] = []
        error: PGError | None = None
        while True:
            type_byte, payload = self._read_message()
            if type_byte == b"T":
                fields = _parse_row_description(payload)
            elif type_byte == b"D":
                values = _parse_data_row(payload)
                result.rows.append(
                    {
                        name: _decode_value(oid, raw)
                        for (name, oid), raw in zip(fields, values)
                    }
                )
            elif type_byte == b"C":
                tag = payload[:-1].decode("utf-8", "replace") if payload else ""
                result.command = tag
                result.rowcount = _parse_command_rowcount(tag, len(result.rows))
            elif type_byte == b"E":
                if error is None:
                    error = _error_from_response(payload)
            elif type_byte == b"n":                         # NoData
                fields = []
            elif type_byte == b"S":
                key, _, rest = payload.partition(b"\x00")
                value, _, _ = rest.partition(b"\x00")
                self._params[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
            elif type_byte == b"Z":
                break
            # 1 ParseComplete / 2 BindComplete / 3 CloseComplete / I EmptyQuery / s PortalSuspended
        if error is not None:
            raise error
        return result


def _parse_command_rowcount(tag: str, fallback: int) -> int:
    """从 CommandComplete 标签取行数：`INSERT 0 3` / `UPDATE 2` / `SELECT 5`。"""
    if not tag:
        return fallback
    last = tag.rsplit(" ", 1)[-1]
    if last.isdigit():
        return int(last)
    return fallback


# ============================================================================ 连接池
class PGPool:
    """**每线程一条连接**的轻量池，带「失效重连一次」。

    为什么一线程一条而不是共享池：PG 连接同一时刻只能跑一条命令，
    多线程复用必须加锁排队 —— 那还不如让每个线程各自持有连接，
    让并发真正发生在数据库侧（这也是多实例的意义）。

    「重连一次」的边界很关键：只在**单条自动提交语句**上做。事务里的语句
    `BEGIN` 之后连接断了，绝不能悄悄重连重放 —— 那可能把已经生效的一半
    再执行一遍。因此事务路径（`acquire()` 之后自己发 BEGIN/COMMIT）
    **不做**自动重试，连接断开就报错，交给上层决定。
    """

    def __init__(self, dsn: str | PGDSN) -> None:
        self.dsn = dsn if isinstance(dsn, PGDSN) else parse_dsn(dsn)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[PGConnection] = []

    # ------------------------------------------------------------ 获取/释放
    def acquire(self) -> PGConnection:
        """拿到本线程的连接（必要时新建）。**不**带重试。"""
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            if conn is not None:
                self._forget(conn)
            conn = PGConnection(self.dsn)
            conn.connect()
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def _forget(self, conn: PGConnection) -> None:
        with self._lock:
            try:
                self._all.remove(conn)
            except ValueError:
                pass

    def drop(self) -> None:
        """丢弃本线程的连接（连接失效后调用，下次 acquire 会重连）。"""
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            self._forget(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """关闭已知连接（尽力而为：其它线程持有的连接会在其线程退出时回收）。"""
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._local.conn = None

    # ------------------------------------------------------------ 便捷执行
    def run(self, sql: str, params: tuple[Any, ...] = ()) -> PGResult:
        """自动提交的单条参数化语句；连接层故障时**重连一次**再试。"""
        try:
            return self.acquire().exec_params(sql, params)
        except PGConnectionError:
            self.drop()
            return self.acquire().exec_params(sql, params)

    def run_simple(self, sql: str) -> None:
        """自动提交的单条无参语句（DDL），连接层故障时重连一次再试。"""
        try:
            self.acquire().exec_simple(sql)
        except PGConnectionError:
            self.drop()
            self.acquire().exec_simple(sql)


__all__ = [
    "PGConnection",
    "PGConnectionError",
    "PGDSN",
    "PGError",
    "PGPool",
    "PGResult",
    "SQLSTATE_DUPLICATE_OBJECT",
    "SQLSTATE_DUPLICATE_TABLE",
    "parse_dsn",
    "split_statements",
]