"""Anthropic 适配器的响应处理（用模拟客户端；未连接真实 API，真实调用标为未验证）。"""
from types import SimpleNamespace

from app.ai.providers import AnthropicProvider


class _Msgs:
    def __init__(self, resp):
        self.resp = resp
        self.kw = None

    def create(self, **kw):
        self.kw = kw
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _prov(resp):
    p = AnthropicProvider("sk-ant-test", "claude-opus-5-5", 5, 0)
    m = _Msgs(resp)
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=m))
    return p, m


def _resp(stop="end_turn", text='{"a":1}'):
    return SimpleNamespace(stop_reason=stop, model="claude-opus-5-5", content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def test_ok_request_shape_and_usage():
    p, m = _prov(_resp())
    r = p.complete({"x": 1})
    assert r.status == "OK" and r.text == '{"a":1}' and r.input_tokens == 100
    kw = m.kw
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in kw, "模型不获得任何工具"
    assert "temperature" not in kw and "thinking" not in kw
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]


def test_refusal_and_truncation_are_not_ok():
    assert _prov(_resp(stop="refusal"))[0].complete({}).status == "REFUSAL"
    assert _prov(_resp(stop="max_tokens"))[0].complete({}).status == "ERROR"


def test_timeout_maps_to_timeout():
    import anthropic
    import httpx2

    err = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    assert _prov(err)[0].complete({}).status == "TIMEOUT"
