"""只读诊断：子进程输出解析（Mac 实机发现 SDK 关闭日志出现在结果 JSON 之后，导致 JSONDecodeError）。"""
import json
import subprocess

from app.broker import diagnostics, moomoo_adapter

RESULT = {"ok": True, "items": [{"key": "opend", "label": "OpenD 连接", "status": "PASS", "detail": "OpenD 版本 x"}],
          "accounts": []}
# 仿照 SDK 的日志格式；其中一行日志本身就是 JSON，但不是诊断结果
STARTUP = "2026-10-23 10:00:00,001 | 1234 | [open_context_base.py] InitConnect ok: conn_id=1"
CLOSE = "2026-10-23 10:00:01,002 | 1234 | [network_manager.py] Close: conn_id=1"
LOG_JSON = json.dumps({"event": "keepalive", "conn_id": 1})


def _run(monkeypatch, stdout, stderr=""):
    monkeypatch.setattr(moomoo_adapter, "tcp_probe", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, stderr))
    return diagnostics.run_diagnostics(symbols=["US.AAPL"])


def test_result_followed_by_sdk_close_log_is_parsed(env, monkeypatch):
    out = "\n".join([STARTUP, json.dumps(RESULT, ensure_ascii=False), CLOSE, LOG_JSON, ""])
    res = _run(monkeypatch, out)
    assert res["ok"] is True
    assert res["items"] == RESULT["items"]
    assert "opend" in res and "security_firm" in res


def test_last_result_wins_and_non_result_json_is_ignored(env, monkeypatch):
    older = {"ok": False, "items": [], "accounts": []}
    out = "\n".join([json.dumps(older), STARTUP, json.dumps(RESULT), "{not json", LOG_JSON])
    assert diagnostics.parse_runner_output(out) == RESULT


def test_no_result_reports_runner_failure_instead_of_success(env, monkeypatch):
    res = _run(monkeypatch, "\n".join([STARTUP, LOG_JSON, CLOSE]), stderr="Traceback: boom")
    assert res["ok"] is False
    assert res["items"][0]["key"] == "runner" and res["items"][0]["status"] == "FAIL"
    assert "boom" in res["items"][0]["detail"]
    assert diagnostics.parse_runner_output("") == {} and diagnostics.parse_runner_output(None) == {}
