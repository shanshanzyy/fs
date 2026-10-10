"""PG 协议客户端（`store/pgproto.py`）的测试。

为什么不用真实 Postgres：协议实现最容易错的是**报文的边界与时序** —— 多读一个
字节、少发一条 `Sync`、SCRAM 少算一步，这些在真实库上表现为「偶发连接卡死」，
很难定位。所以这里用 stdlib `socket` 起一个**假 PG 服务器**，逐条钉死协议语义
（照搬 `test_oidc.py` 用假 IdP 的范式）。

钉死的几条（对应 M4+-PLAN §B）：
  ① startup + SCRAM-SHA-256 全流程能走通，`AuthenticationOk` 后拿到
     `server_version`；
  ② MD5 兼容路径；
  ③ **拒绝明文口令**（`AuthenticationCleartextPassword` 直接失败）；
  ④ `ErrorResponse` 的 SQLSTATE 映射到 `StoreError` 子类；
  ⑤ **参数注入不改语义**：注入串只作为 `$1` 绑定值出现，SQL 文本里看不到它；
  ⑥ `sslmode=require` 但服务端不支持 TLS 时**明确失败**，不静默降级。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import socketserver
import struct
import threading
import unittest

from tests.support import TempDirTestCase  # noqa: F401 - 触发 sys.path 引导

from vibe_server.errors import ConfigError, StoreError
from vibe_server.store import pgproto
from vibe_server.store.pgproto import (
    PGConnection,
    PGConnectionError,
    PGError,
    PGPool,
    parse_dsn,
    split_statements,
)
from vibe_server.store.schema import POSTGRES


# ============================================================================ 假 PG 服务器
def _i16(value: int) -> bytes:
    return struct.pack("!h", value)


def _i32(value: int) -> bytes:
    return struct.pack("!i", value)


def _cstr(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _msg(type_byte: bytes, payload: bytes) -> bytes:
    return type_byte + _i32(len(payload) + 4) + payload


class _Config:
    def __init__(self, *, auth: str = "scram", ssl_answer: str = "N",
                 password: str = "s3cret", iterations: int = 4096) -> None:
        self.auth = auth
        self.ssl_answer = ssl_answer.encode("ascii")
        self.password = password
        self.iterations = iterations
        self.salt = os.urandom(16)
        self.server_nonce = "servernonce" + os.urandom(4).hex()


class _FakePGHandler(socketserver.BaseRequestHandler):
    """一个「够用」的 PG 服务端：只认几条被测试用到的语句。"""

    # ------------------------------------------------------------ 底层 IO
    def _recv(self, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = self.request.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf.extend(chunk)
        return bytes(buf)

    def _read_message(self) -> tuple[bytes, bytes]:
        header = self._recv(5)
        length = struct.unpack_from("!i", header, 1)[0]
        payload = self._recv(length - 4) if length > 4 else b""
        return header[0:1], payload

    def _send(self, data: bytes) -> None:
        self.request.sendall(data)

    # ------------------------------------------------------------ 主流程
    def handle(self) -> None:  # noqa: C901
        self._sql = ""
        self._params: list[bytes | None] = []
        try:
            self._serve()
        except (ConnectionError, OSError, struct.error, ValueError):
            pass

    def _serve(self) -> None:
        cfg: _Config = self.server.cfg  # type: ignore[attr-defined]
        length = struct.unpack("!i", self._recv(4))[0]
        body = self._recv(length - 4)
        if length == 8 and struct.unpack("!i", body)[0] == 80877103:
            self._send(cfg.ssl_answer)          # SSLRequest 应答
            if cfg.ssl_answer == b"S":
                return                          # 假服务器不实现真 TLS
            length = struct.unpack("!i", self._recv(4))[0]
            self._recv(length - 4)              # 真正的 StartupMessage
        self._authenticate(cfg)
        # 认证通过后：若干 ParameterStatus + BackendKeyData + ReadyForQuery
        self._send(_msg(b"S", _cstr("server_version") + _cstr("16.3")))
        self._send(_msg(b"S", _cstr("client_encoding") + _cstr("UTF8")))
        self._send(_msg(b"K", _i32(1234) + _i32(5678)))
        self._ready()
        self._message_loop()

    def _ready(self) -> None:
        self._send(_msg(b"Z", b"I"))

    # ------------------------------------------------------------ 认证
    def _authenticate(self, cfg: _Config) -> None:
        if cfg.auth == "cleartext":
            self._send(_msg(b"R", _i32(3)))
            return                              # 客户端应当直接放弃并断开
        if cfg.auth == "md5":
            salt = b"\x01\x02\x03\x04"
            self._send(_msg(b"R", _i32(5) + salt))
            type_byte, payload = self._read_message()
            assert type_byte == b"p"
            inner = hashlib.md5((cfg.password + "tester").encode()).hexdigest()
            expected = b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()
            if payload != expected:
                self._send(_error("28P01", "password authentication failed"))
                return
            self._send(_msg(b"R", _i32(0)))
            return
        # SCRAM-SHA-256
        mechanisms = b"SCRAM-SHA-256\x00\x00"
        self._send(_msg(b"R", _i32(10) + mechanisms))
        type_byte, payload = self._read_message()
        assert type_byte == b"p"
        client_first = self._parse_sasl_initial(payload)
        client_bare = client_first[3:]                     # 去掉 "n,,"
        attrs = dict(p.split("=", 1) for p in client_bare.split(","))
        client_nonce = attrs["r"]
        combined = client_nonce + cfg.server_nonce
        salt_b64 = base64.b64encode(cfg.salt).decode()
        server_first = f"r={combined},s={salt_b64},i={cfg.iterations}"
        self._send(_msg(b"R", _i32(11) + server_first.encode()))

        type_byte, payload = self._read_message()
        assert type_byte == b"p"
        client_final = payload.decode()
        without_proof, _, proof_b64 = client_final.rpartition(",p=")
        auth_message = f"{client_bare},{server_first},{without_proof}"
        salted = hashlib.pbkdf2_hmac(
            "sha256", cfg.password.encode(), cfg.salt, cfg.iterations
        )
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        signature = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
        expected = base64.b64encode(
            bytes(a ^ b for a, b in zip(client_key, signature))
        ).decode()
        if proof_b64 != expected:
            self._send(_error("28P01", "SCRAM proof mismatch"))
            return
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        server_sig = base64.b64encode(
            hmac.new(server_key, auth_message.encode(), hashlib.sha256).digest()
        ).decode()
        self._send(_msg(b"R", _i32(12) + f"v={server_sig}".encode()))
        self._send(_msg(b"R", _i32(0)))

    @staticmethod
    def _parse_sasl_initial(payload: bytes) -> str:
        end = payload.index(b"\x00")
        length = struct.unpack_from("!i", payload, end + 1)[0]
        return payload[end + 5:end + 5 + length].decode()

    # ------------------------------------------------------------ 语句
    def _message_loop(self) -> None:
        while True:
            type_byte, payload = self._read_message()
            if type_byte == b"X":
                return
            if type_byte == b"Q":
                sql = payload[:-1].decode("utf-8", "replace")
                self._record(sql, [])
                if _is_error_sql(sql):
                    self._send(_error("42P01", 'relation "nope" does not exist'))
                else:
                    self._send(_msg(b"C", _cstr(_command_tag(sql))))
                self._ready()
            elif type_byte == b"P":
                self._sql = _parse_parse(payload)
                self._params = []
                self._send(_msg(b"1", b""))
            elif type_byte == b"B":
                self._params = _parse_bind(payload)
                self._send(_msg(b"2", b""))
            elif type_byte == b"D":
                self._send(_describe(self._sql, self._params))
            elif type_byte == b"E":
                self._record(self._sql, self._params)
                if _is_error_sql(self._sql):
                    self._send(_error("42P01", 'relation "nope" does not exist'))
                else:
                    self._send(_execute(self._sql, self._params))
            elif type_byte == b"S":
                self._ready()

    def _record(self, sql: str, params: list[bytes | None]) -> None:
        self.server.queries.append((sql, params))  # type: ignore[attr-defined]


def _parse_parse(payload: bytes) -> str:
    # name\0 + query\0 + ...
    _, _, rest = payload.partition(b"\x00")
    query, _, _ = rest.partition(b"\x00")
    return query.decode("utf-8", "replace")


def _parse_bind(payload: bytes) -> list[bytes | None]:
    offset = 0
    # portal\0 + statement\0
    for _ in range(2):
        end = payload.index(b"\x00", offset)
        offset = end + 1
    (n_formats,) = struct.unpack_from("!h", payload, offset)
    offset += 2 + n_formats * 2
    (count,) = struct.unpack_from("!h", payload, offset)
    offset += 2
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


def _is_error_sql(sql: str) -> bool:
    return "probe_error" in sql.lower()


def _command_tag(sql: str) -> str:
    head = sql.strip().lower()
    if head.startswith("insert"):
        return "INSERT 0 1"
    if head.startswith("update"):
        return "UPDATE 2"
    if head.startswith("select"):
        return "SELECT 0"
    return "SET"


def _columns_for(sql: str, params: list[bytes | None]) -> list[tuple[str, int]] | None:
    low = sql.lower()
    if "probe_types" in low:
        return [("i", pgproto.OID_INT4), ("b", pgproto.OID_BOOL),
                ("s", pgproto.OID_TEXT), ("n", pgproto.OID_TEXT)]
    if "probe_echo" in low:
        return [(f"c{index}", pgproto.OID_TEXT) for index in range(len(params))]
    head = low.strip()
    if head.startswith(("insert", "update", "delete", "create", "drop", "begin",
                        "commit", "rollback", "set", "alter")):
        return None                     # NoData
    return []


def _describe(sql: str, params: list[bytes | None]) -> bytes:
    columns = _columns_for(sql, params)
    if not columns:
        return _msg(b"n", b"")          # NoData
    payload = _i16(len(columns))
    for name, oid in columns:
        payload += _cstr(name) + _i32(0) + _i16(0) + _i32(oid) + _i16(-1) + _i32(-1) + _i16(0)
    return _msg(b"T", payload)


def _execute(sql: str, params: list[bytes | None]) -> bytes:
    columns = _columns_for(sql, params)
    low = sql.lower()
    body = b""
    if columns and "probe_types" in low:
        body += _data_row([b"42", b"t", b"hello", None])
    elif columns and "probe_echo" in low:
        body += _data_row(list(params))
    tag = _command_tag(sql)
    if columns is not None and columns:
        tag = "SELECT 1"
    return body + _msg(b"C", _cstr(tag))


def _data_row(values: list[bytes | None]) -> bytes:
    payload = _i16(len(values))
    for value in values:
        if value is None:
            payload += _i32(-1)
        else:
            payload += _i32(len(value)) + value
    return _msg(b"D", payload)


def _error(sqlstate: str, message: str) -> bytes:
    payload = b"S" + _cstr("ERROR") + b"C" + _cstr(sqlstate) + b"M" + _cstr(message) + b"\x00"
    return _msg(b"E", payload)


class FakePGServer:
    """监听随机端口、记录收到语句的假 PG。"""

    def __init__(self, **cfg_kwargs: object) -> None:
        cfg = _Config(**cfg_kwargs)  # type: ignore[arg-type]
        self.queries: list[tuple[str, list[bytes | None]]] = []
        outer = self

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def __init__(self) -> None:
                super().__init__(("127.0.0.1", 0), _FakePGHandler)
                self.cfg = cfg
                self.queries = outer.queries

        self._server = _Server()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def dsn(self) -> str:
        host, port = self._server.server_address[:2]
        return f"postgres://tester:s3cret@{host}:{port}/vibetest?sslmode=disable"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ============================================================================ 单元：DSN / 拆分
class DsnTests(unittest.TestCase):
    def test_parse_full_dsn(self) -> None:
        dsn = parse_dsn(
            "postgres://u:p%404@db.internal:6432/vibe?sslmode=require&connect_timeout=7"
        )
        self.assertEqual(dsn.host, "db.internal")
        self.assertEqual(dsn.port, 6432)
        self.assertEqual(dsn.user, "u")
        self.assertEqual(dsn.password, "p@4")          # 百分号解码
        self.assertEqual(dsn.database, "vibe")
        self.assertEqual(dsn.sslmode, "require")
        self.assertEqual(dsn.connect_timeout, 7.0)
        self.assertNotIn("p@4", dsn.label())           # label 不含口令

    def test_defaults_and_scheme_alias(self) -> None:
        dsn = parse_dsn("postgresql://bob@localhost/vibe")
        self.assertEqual(dsn.port, 5432)
        self.assertEqual(dsn.database, "vibe")
        self.assertEqual(dsn.sslmode, "prefer")
        # 省略库名时用用户名兜底
        self.assertEqual(parse_dsn("psql://bob@h").database, "bob")

    def test_reject_bad_dsn(self) -> None:
        for bad in ("mysql://u@h/db", "postgres://", "postgres://h/db?sslmode=weird"):
            with self.assertRaises(ConfigError):
                parse_dsn(bad)


class SplitStatementsTests(unittest.TestCase):
    def test_schema_ddl_splits(self) -> None:
        statements = split_statements(POSTGRES)
        self.assertGreater(len(statements), 10)
        self.assertTrue(all(";" not in s for s in statements))
        self.assertTrue(all(s.strip() for s in statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS users" in s for s in statements))

    def test_comments_and_quotes(self) -> None:
        sql = "-- 头注释\nSELECT 'a;b';\nSELECT 2; -- 尾注释"
        self.assertEqual(split_statements(sql), ["SELECT 'a;b'", "SELECT 2"])


# ============================================================================ 集成：假 PG
class PgProtocolTests(TempDirTestCase):
    def _server(self, **kwargs: object) -> FakePGServer:
        server = FakePGServer(**kwargs)
        self.addCleanup(server.stop)
        return server

    # ------------------------------------------------------------ 认证
    def test_scram_handshake_and_type_mapping(self) -> None:
        server = self._server(auth="scram")
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        conn.connect()
        self.assertFalse(conn.closed)
        self.assertEqual(conn.server_version, "16.3")     # ParameterStatus 已捕获

        result = conn.exec_params("SELECT i, b, s, n FROM probe_types")
        self.assertEqual(result.rows, [{"i": 42, "b": True, "s": "hello", "n": None}])
        self.assertEqual(result.command, "SELECT 1")
        self.assertEqual(result.rowcount, 1)

    def test_md5_handshake(self) -> None:
        server = self._server(auth="md5")
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        conn.connect()
        self.assertFalse(conn.closed)
        self.assertEqual(conn.exec_params("SELECT 1").rowcount, 0)

    def test_cleartext_password_is_refused(self) -> None:
        server = self._server(auth="cleartext")
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        with self.assertRaises(PGConnectionError) as ctx:
            conn.connect()
        self.assertIn("明文", str(ctx.exception))
        self.assertTrue(conn.closed)                     # 失败后不残留半开连接

    # ------------------------------------------------------------ 错误与参数
    def test_error_response_maps_to_store_error(self) -> None:
        server = self._server()
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        conn.connect()
        with self.assertRaises(PGError) as ctx:
            conn.exec_params("SELECT * FROM probe_error")
        self.assertIsInstance(ctx.exception, StoreError)
        self.assertEqual(ctx.exception.sqlstate, "42P01")
        self.assertIn("nope", str(ctx.exception))
        # 出错后连接仍可用（Sync 已把协议状态复位）
        self.assertFalse(conn.closed)
        self.assertEqual(conn.exec_params("SELECT 1").command, "SELECT 0")

    def test_parameters_are_never_interpolated(self) -> None:
        server = self._server()
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        conn.connect()
        payload = "'; DROP TABLE users; --"
        conn.exec_params("SELECT c0 FROM probe_echo WHERE c0 = $1", (payload,))

        sql, params = server.queries[-1]
        self.assertIn("$1", sql)
        self.assertNotIn("DROP TABLE", sql)              # 注入串不在 SQL 文本里
        self.assertEqual(params[0], payload.encode())    # 只作为绑定值出现

    def test_null_parameter_round_trips(self) -> None:
        server = self._server()
        conn = PGConnection(parse_dsn(server.dsn))
        self.addCleanup(conn.close)
        conn.connect()
        conn.exec_params("SELECT c0 FROM probe_echo WHERE c0 = $1", (None,))
        sql, params = server.queries[-1]
        self.assertEqual(params, [None])
        self.assertIn("$1", sql)

    # ------------------------------------------------------------ 传输安全
    def test_sslmode_require_fails_when_server_has_no_tls(self) -> None:
        server = self._server(ssl_answer="N")
        dsn = server.dsn.replace("sslmode=disable", "sslmode=require")
        conn = PGConnection(parse_dsn(dsn))
        self.addCleanup(conn.close)
        with self.assertRaises(PGConnectionError) as ctx:
            conn.connect()
        self.assertIn("TLS", str(ctx.exception))

    def test_sslmode_prefer_falls_back_to_plaintext(self) -> None:
        server = self._server(ssl_answer="N")
        dsn = server.dsn.replace("sslmode=disable", "sslmode=prefer")
        conn = PGConnection(parse_dsn(dsn))
        self.addCleanup(conn.close)
        conn.connect()
        self.assertFalse(conn.closed)

    # ------------------------------------------------------------ 连接池
    def test_pool_reuses_connection_per_thread(self) -> None:
        server = self._server()
        pool = PGPool(server.dsn)
        self.addCleanup(pool.close)
        first = pool.acquire()
        self.assertIs(pool.acquire(), first)             # 同线程复用
        self.assertFalse(first.closed)

    def test_pool_reconnects_after_drop(self) -> None:
        server = self._server()
        pool = PGPool(server.dsn)
        self.addCleanup(pool.close)
        first = pool.acquire()
        pool.drop()
        second = pool.acquire()
        self.assertIsNot(first, second)                  # 丢弃后重连
        self.assertTrue(first.closed)

    def test_run_simple_executes_ddl(self) -> None:
        server = self._server()
        pool = PGPool(server.dsn)
        self.addCleanup(pool.close)
        pool.run_simple("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY)")
        sql, _ = server.queries[-1]
        self.assertIn("CREATE TABLE", sql)


if __name__ == "__main__":
    unittest.main()