"""客户端模型选择器（M9-I）测试。

验证七条纪律（对齐 M9-PLAN §2-I / E6）：
1. `none` 策略（或未启用）逐字节一致 —— resolve 返回原模型、client 请求体 model 不变；
2. `static` 映射命中：simple→小模型、complex→大模型；
3. 未匹配（default 意图）回退默认模型；
4. **选型越界回退**：选出授权集合外的模型 → fail-closed 回退默认；
5. 策略非法回退 `none`（不选型，逐字节一致）；
6. 授权集合空 = 不限制；集合非空时收紧；
7. 审计只记 intent/model/reason（任务正文不出机）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.audit import AuditLog  # noqa: E402
from agent_core.llm.openai_compat import OpenAICompatClient  # noqa: E402
from agent_core.llm.router import ModelRouter, infer_intent  # noqa: E402


ALLOWED = ("qwen2.5-coder:7b", "qwen2.5-coder:32b", "tiny-coder:0.5b")


def _router(**kw) -> ModelRouter:
    base = dict(
        enabled=True,
        policy="static",
        default_model="qwen2.5-coder:7b",
        static_map={"simple": "tiny-coder:0.5b", "complex": "qwen2.5-coder:32b"},
        allowed_models=ALLOWED,
    )
    base.update(kw)
    return ModelRouter(**base)


class ResolveTests(unittest.TestCase):
    def test_disabled_returns_requested_model(self) -> None:
        r = ModelRouter(enabled=False, default_model="d", static_map={"simple": "s"})
        self.assertEqual(r.resolve("simple", "m"), ("m", "none"))

    def test_none_policy_returns_requested_model(self) -> None:
        r = _router(policy="none")
        self.assertEqual(r.resolve("simple", "m"), ("m", "none"))

    def test_static_simple_hits_small(self) -> None:
        r = _router()
        self.assertEqual(r.resolve("simple", "qwen2.5-coder:7b"), ("tiny-coder:0.5b", "static"))

    def test_static_complex_hits_large(self) -> None:
        r = _router()
        self.assertEqual(
            r.resolve("complex", "qwen2.5-coder:7b"), ("qwen2.5-coder:32b", "static")
        )

    def test_default_intent_falls_back(self) -> None:
        r = _router()
        self.assertEqual(r.resolve("default", "qwen2.5-coder:7b"), ("qwen2.5-coder:7b", "fallback"))

    def test_out_of_allowed_set_falls_back(self) -> None:
        # static_map 里的 complex 模型不在 allowed_models → 越界回退默认
        r = _router(
            static_map={"simple": "tiny-coder:0.5b", "complex": "forbidden-model"},
            allowed_models=("qwen2.5-coder:7b", "tiny-coder:0.5b"),
        )
        self.assertEqual(r.resolve("complex", "qwen2.5-coder:7b"), ("qwen2.5-coder:7b", "fallback"))

    def test_invalid_policy_falls_back_to_none(self) -> None:
        r = _router(policy="bogus")
        self.assertEqual(r.resolve("simple", "m"), ("m", "none"))

    def test_empty_allowed_set_is_unrestricted(self) -> None:
        r = _router(allowed_models=())
        self.assertEqual(r.resolve("complex", "qwen2.5-coder:7b"), ("qwen2.5-coder:32b", "static"))

    def test_default_model_used_when_requested_empty(self) -> None:
        r = _router()
        # requested 为空时，fallback 用 default_model
        self.assertEqual(r.resolve("default", ""), ("qwen2.5-coder:7b", "fallback"))


class InferIntentTests(unittest.TestCase):
    def test_complex_signals(self) -> None:
        self.assertEqual(
            infer_intent([{"role": "user", "content": "帮我重构这个模块的架构"}]), "complex"
        )

    def test_simple_signals(self) -> None:
        self.assertEqual(
            infer_intent([{"role": "user", "content": "改个变量名"}]), "simple"
        )

    def test_default_when_unknown(self) -> None:
        self.assertEqual(infer_intent([{"role": "user", "content": "看一下这个文件"}]), "default")

    def test_empty_returns_default(self) -> None:
        self.assertEqual(infer_intent([]), "default")


class ClientIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.audit = AuditLog(self.ws / "audit.jsonl", session_id="t", enabled=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self, router: ModelRouter | None) -> OpenAICompatClient:
        return OpenAICompatClient(
            base_url="http://127.0.0.1:1/v1",
            model="qwen2.5-coder:7b",
            api_key="x",
            router=router,
            audit=self.audit,
        )

    def test_no_router_payload_model_unchanged(self) -> None:
        c = self._client(None)
        # 不真正发请求，只验证选型辅助方法对 model 的处理
        self.assertEqual(c._resolve_model([{"role": "user", "content": "hi"}]), "qwen2.5-coder:7b")

    def test_router_simple_overrides_model(self) -> None:
        r = _router()
        c = self._client(r)
        model = c._resolve_model([{"role": "user", "content": "改个变量名"}])
        self.assertEqual(model, "tiny-coder:0.5b")

    def test_router_complex_overrides_model(self) -> None:
        r = _router()
        c = self._client(r)
        model = c._resolve_model([{"role": "user", "content": "帮我重构这个模块的架构"}])
        self.assertEqual(model, "qwen2.5-coder:32b")

    def test_audit_records_intent_model_reason_only(self) -> None:
        r = _router()
        c = self._client(r)
        c._resolve_model([{"role": "user", "content": "重构一下"}])
        lines = [
            json.loads(l)
            for l in self.ws.joinpath("audit.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        route_events = [e for e in lines if e["event"] == "model.route"]
        self.assertEqual(len(route_events), 1)
        ev = route_events[0]
        self.assertEqual(ev["intent"], "complex")
        self.assertEqual(ev["model"], "qwen2.5-coder:32b")
        self.assertEqual(ev["reason"], "static")
        # 任务正文不出机
        self.assertNotIn("重构一下", json.dumps(ev, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
