"""夸克分享客户端单元测试：全程 monkeypatch RequestUtils，不访问网络。"""

from __future__ import annotations

import pytest

from quarkchannel.shares import quark as quark_module
from quarkchannel.shares.quark import QuarkShareClient, ShareFile, extract_pwd_id


class _Resp:
    """最小响应桩。"""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        """返回预设 JSON。"""
        return self._payload


def _install_request(monkeypatch, handler):
    """把模块内的 RequestUtils 替换成带回调的假实现。"""

    class _Request:
        def __init__(self, *args, **kwargs):
            """兼容真实 RequestUtils 的无参构造。"""

        def get_res(self, url, **kwargs):
            """转发 GET 请求。"""
            return handler("GET", url, kwargs)

        def post_res(self, url, **kwargs):
            """转发 POST 请求。"""
            return handler("POST", url, kwargs)

    monkeypatch.setattr(quark_module, "RequestUtils", _Request)


def test_stoken_failure_returns_empty(monkeypatch):
    """stoken 接口失败时返回空串且不抛异常。"""

    def handler(method, url, kwargs):
        """所有域名都返回业务失败。"""
        return _Resp({"status": 400, "code": 1, "message": "share not found"})

    _install_request(monkeypatch, handler)
    client = QuarkShareClient()
    assert client.stoken("pwd", "code") == ""


def test_stoken_success(monkeypatch):
    """stoken 成功时原样返回。"""

    def handler(method, url, kwargs):
        """只响应 token 接口。"""
        if method == "POST" and url.endswith("/token"):
            assert kwargs.get("json") == {"pwd_id": "pwd", "passcode": "1234"}
            return _Resp({"status": 200, "data": {"stoken": "token-1"}})
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    assert QuarkShareClient().stoken("pwd", "1234") == "token-1"


def test_snapshot_recurses_dirs_and_keeps_share_token(monkeypatch):
    """目录会递归一层，文件上的 share_fid_token 透传到 ShareFile。"""
    detail_calls = []

    def handler(method, url, kwargs):
        """按 pdir_fid 返回根目录与子目录内容。"""
        if method == "POST" and url.endswith("/token"):
            return _Resp({"status": 200, "data": {"stoken": "tk"}})
        if method == "GET" and url.endswith("/detail"):
            params = kwargs["params"]
            detail_calls.append((params["pdir_fid"], params["pagenum"]))
            if params["pdir_fid"] == "0":
                return _Resp({
                    "status": 200,
                    "data": {
                        "count": 2,
                        "list": [
                            {"fid": "dir-1", "file_name": "Season 1", "size": 0, "dir": True},
                            {"fid": "file-root", "file_name": "S01E01.mkv", "size": 10,
                             "share_fid_token": "root-token"},
                        ],
                        "share": {"title": "演示剧集"},
                    },
                })
            if params["pdir_fid"] == "dir-1":
                return _Resp({
                    "status": 200,
                    "data": {
                        "count": 1,
                        "list": [
                            {"fid": "file-child", "file_name": "S01E02.mkv", "size": 20,
                             "share_fid_token": "child-token"},
                        ],
                    },
                })
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    snapshot = QuarkShareClient().snapshot("https://pan.quark.cn/s/abcd?pwd=1234", "1234")
    assert [item.name for item in snapshot.files] == ["S01E01.mkv", "S01E02.mkv"]
    assert snapshot.files[1].share_token == "child-token"
    assert snapshot.total_size == 30
    assert ("0", 1) in detail_calls and ("dir-1", 1) in detail_calls


def test_download_url_uses_file_download_entry_ft(monkeypatch):
    """取直链走 /file/download 且带 entry=ft（分享直下不需要转存）。"""
    captured = {}

    def handler(method, url, kwargs):
        """记录下载请求体与查询参数并返回直链。"""
        if method == "POST" and url.endswith("/token"):
            return _Resp({"status": 200, "data": {"stoken": "tk"}})
        if method == "POST" and url.endswith("/file/download"):
            captured.update(kwargs.get("json") or {})
            captured["params"] = kwargs.get("params") or {}
            return _Resp({"status": 200, "data": [{"download_url": "https://dl.example/file"}]})
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    client = QuarkShareClient()
    item = ShareFile(fid="f1", name="a.mkv", size=1, share_token="file-token")
    url, headers = client.download_url("https://pan.quark.cn/s/abcd", "1234", item)
    assert url == "https://dl.example/file"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert captured["fids"] == ["f1"]
    assert captured["params"]["entry"] == "ft"


@pytest.mark.parametrize(
    ("share_url", "expected"),
    [
        ("https://pan.quark.cn/s/abc123", "abc123"),
        ("https://pan.quark.cn/s/abc123?pwd=1234", "abc123"),
        ("https://pan.quark.cn/s/abc123#/list/share", "abc123"),
        ("abc123", "abc123"),
    ],
)
def test_extract_pwd_id_forms(share_url, expected):
    """四种链接形态（含提取码、hash、裸短码）都能解析 pwd_id。"""
    assert extract_pwd_id(share_url) == expected
