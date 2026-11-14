"""aria2 RPC 客户端与启动器单元测试：全部使用假响应，不访问网络。"""

from __future__ import annotations

import pytest

from quarkchannel import aria2 as aria2_module
from quarkchannel.aria2 import Aria2Launcher, Aria2Rpc


class _JsonResponse:
    """带 json() 的最小响应桩。"""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        """返回预设 JSON。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _rpc_for(handler):
    """用指定 post_res 行为构造 Aria2Rpc。"""

    class _Request:
        def __init__(self, *args, **kwargs):
            """兼容无参构造。"""

        def post_res(self, *args, **kwargs):
            """转交给测试处理器。"""
            return handler(*args, **kwargs)

    return _Request


def test_rpc_call_uses_token_and_returns_result(monkeypatch):
    """RPC 请求带 token，且正确取出 result。"""
    seen = {}

    def handler(url, json=None, **kwargs):
        seen["url"] = url
        seen["body"] = json
        return _JsonResponse({
            "jsonrpc": "2.0",
            "id": json["id"],
            "result": {"version": "1.36.0"},
        })

    monkeypatch.setattr(aria2_module, "RequestUtils", _rpc_for(handler))
    rpc = Aria2Rpc("http://127.0.0.1:6805/jsonrpc", "secret")
    assert rpc.version() == {"version": "1.36.0"}
    assert seen["url"] == "http://127.0.0.1:6805/jsonrpc"
    assert seen["body"]["method"] == "aria2.getVersion"
    assert seen["body"]["params"][0] == "token:secret"
    assert seen["body"]["id"]


def test_rpc_add_uri_returns_gid(monkeypatch):
    """add_uri 使用 URL 数组与选项，并返回 gid。"""
    seen = {}

    def handler(url, json=None, **kwargs):
        seen["body"] = json
        return _JsonResponse({"result": "gid-1"})

    monkeypatch.setattr(aria2_module, "RequestUtils", _rpc_for(handler))
    rpc = Aria2Rpc("http://127.0.0.1:6805/jsonrpc", "secret")
    assert rpc.add_uri("https://dl.example/a", {"out": "a.bin"}) == "gid-1"
    assert seen["body"]["method"] == "aria2.addUri"
    assert seen["body"]["params"][1] == ["https://dl.example/a"]
    assert seen["body"]["params"][2] == {"out": "a.bin"}


def test_rpc_tell_status_waiting_active_complete(monkeypatch):
    """tellStatus 依次返回等待、下载中、完成三种状态。"""
    statuses = iter([
        {"status": "waiting", "completedLength": "0", "downloadSpeed": "0"},
        {"status": "active", "completedLength": "5", "downloadSpeed": "1024"},
        {"status": "complete", "completedLength": "10", "downloadSpeed": "0"},
    ])

    def handler(url, json=None, **kwargs):
        return _JsonResponse({"result": next(statuses)})

    monkeypatch.setattr(aria2_module, "RequestUtils", _rpc_for(handler))
    rpc = Aria2Rpc("http://127.0.0.1:6805/jsonrpc", "secret")
    assert [rpc.tell_status("gid-1")["status"] for _ in range(3)] == [
        "waiting", "active", "complete",
    ]


def test_rpc_returns_none_on_error_or_non_json(monkeypatch):
    """JSON-RPC error、非 JSON 与请求异常都返回 None。"""
    responses = iter([
        _JsonResponse({"error": {"code": 1, "message": "bad"}}),
        _JsonResponse(ValueError("not json")),
    ])

    def handler(url, json=None, **kwargs):
        return next(responses)

    monkeypatch.setattr(aria2_module, "RequestUtils", _rpc_for(handler))
    rpc = Aria2Rpc("http://127.0.0.1:6805/jsonrpc", "secret")
    assert rpc.tell_status("gid-1") is None
    assert rpc.tell_status("gid-1") is None

    def broken(url, json=None, **kwargs):
        raise RuntimeError("network")

    monkeypatch.setattr(aria2_module, "RequestUtils", _rpc_for(broken))
    assert rpc.version() is None


def test_launcher_reuses_existing_instance(tmp_path, monkeypatch):
    """已有 RPC 可用时不启动新进程。"""
    launcher = Aria2Launcher(tmp_path, "/downloads", "secret", binary="/not/exist")
    monkeypatch.setattr(launcher.rpc, "version", lambda: {"version": "1.36.0"})
    called = []
    monkeypatch.setattr(
        aria2_module.subprocess, "Popen", lambda *args, **kwargs: called.append(args),
    )
    assert launcher.ensure() is True
    assert called == []
    assert launcher._process is None


def test_launcher_start_command_contains_required_options(tmp_path, monkeypatch):
    """需要启动时携带 6805 端口、64 线程和全部关键参数。"""
    launcher = Aria2Launcher(tmp_path, "/downloads", "secret", binary="/downloads/aria2c")
    monkeypatch.setattr(launcher, "_resolve_binary", lambda: "/downloads/aria2c")
    versions = iter([None, {"version": "1.36.0"}])
    monkeypatch.setattr(launcher.rpc, "version", lambda: next(versions, {"version": "1.36.0"}))
    commands = []

    class _Process:
        def poll(self):
            """尚未退出。"""
            return None

    def start(command, **kwargs):
        commands.append(command)
        return _Process()

    monkeypatch.setattr(aria2_module.subprocess, "Popen", start)
    monkeypatch.setattr(aria2_module.time, "sleep", lambda _seconds: None)
    assert launcher.ensure() is True
    command = commands[0]
    assert "--rpc-listen-port=6805" in command
    assert "--rpc-secret=secret" in command
    assert "--max-connection-per-server=64" in command
    assert "--split=64" in command
    assert "--min-split-size=1M" in command
    assert "--dir=/downloads" in command


def test_launcher_stop_does_not_log_secret(tmp_path, monkeypatch):
    """停止异常时日志只记录异常类型，不泄露 secret。"""
    logs = []

    class _Log:
        def warning(self, message, *args, **kwargs):
            """收集 warning。"""
            logs.append(str(message))

    class _Process:
        def poll(self):
            """尚未退出。"""
            return None

        def terminate(self):
            """模拟终止异常。"""
            raise RuntimeError("terminate failed")

        def kill(self):
            """模拟强杀。"""

    launcher = Aria2Launcher(tmp_path, "/downloads", "super-secret", binary="/not/exist")
    launcher._process = _Process()
    monkeypatch.setattr(aria2_module, "logger", _Log())
    launcher.stop()
    assert logs
    assert all("super-secret" not in line for line in logs)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
