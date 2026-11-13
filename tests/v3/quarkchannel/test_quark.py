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


def _folder_row(name: str = "MoviePilotQuarkChannel", fid: str = "dir-1") -> dict:
    """构造一个目录行。"""
    return {"fid": fid, "file_name": name, "dir": True}


def _save_data(task_id: str = "task-1", fids: list[str] | None = None) -> dict:
    """构造转存接口响应。"""
    data = {"task_id": task_id}
    if fids is not None:
        data["task_resp"] = {"data": {"save_as": {"save_as_top_fids": fids}}}
    return {"status": 200, "data": data}


def _task_data(status: int, fids: list[str] | None = None) -> dict:
    """构造任务轮询响应。"""
    data = {"status": status}
    if fids is not None:
        data["task_resp"] = {"data": {"save_as": {"save_as_top_fids": fids}}}
    return {"status": 200, "data": data}


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
            assert kwargs["json"]["pwd_id"] == "pwd"
            assert kwargs["json"]["passcode"] == "1234"
            assert kwargs["json"]["support_visit_limit_private_share"] is True
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
            detail_calls.append((params["pdir_fid"], params["_page"]))
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


def test_ensure_folder_reuses_existing_and_caches(monkeypatch):
    """根目录已有同名目录时直接复用，并在实例内缓存。"""
    sort_calls = []

    def handler(method, url, kwargs):
        """根目录返回已存在的转存目录。"""
        if method == "GET" and url.endswith("/file/sort"):
            sort_calls.append(kwargs["params"]["pdir_fid"])
            return _Resp({"status": 200, "data": {"count": 1, "list": [_folder_row()]}})
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    client = QuarkShareClient()
    assert client.ensure_folder() == "dir-1"
    assert client.ensure_folder() == "dir-1"
    assert sort_calls == ["0"]


def test_ensure_folder_creates_when_missing(monkeypatch):
    """根目录没有同名目录时创建，并缓存返回的 fid。"""
    create_calls = []

    def handler(method, url, kwargs):
        """根目录为空，创建接口返回新目录 fid。"""
        if method == "GET" and url.endswith("/file/sort"):
            return _Resp({"status": 200, "data": {"count": 0, "list": []}})
        if method == "POST" and url.endswith("/file"):
            create_calls.append(kwargs.get("json"))
            return _Resp({"status": 200, "data": {"fid": "new-dir"}})
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    client = QuarkShareClient()
    assert client.ensure_folder() == "new-dir"
    assert client.ensure_folder() == "new-dir"
    assert len(create_calls) == 1
    assert create_calls[0]["pdir_fid"] == "0"
    assert create_calls[0]["file_name"] == "MoviePilotQuarkChannel"


def test_save_files_batches_at_most_50(monkeypatch):
    """51 个文件拆成 50+1 两批转存，返回顺序与入参一致。"""
    save_payloads = []

    def handler(method, url, kwargs):
        """记录 save 请求并直接返回转存后的 fid。"""
        if method == "GET" and url.endswith("/file/sort"):
            return _Resp({"status": 200, "data": {"count": 1, "list": [_folder_row()]}})
        if method == "POST" and url.endswith("/share/sharepage/save"):
            body = kwargs.get("json") or {}
            save_payloads.append(body)
            saved = [f"saved-{fid}" for fid in body.get("fid_list", [])]
            return _Resp(_save_data(f"task-{len(save_payloads)}", saved))
        if method == "GET" and url.endswith("/task"):
            return _Resp(_task_data(2))
        return _Resp({"status": 500})

    _install_request(monkeypatch, handler)
    files = [
        ShareFile(fid=f"f{i}", name=f"n{i}.mkv", size=1, share_token=f"t{i}")
        for i in range(51)
    ]
    saved = QuarkShareClient().save_files("pwd", "tk", files)
    assert len(save_payloads) == 2
    assert [len(item["fid_list"]) for item in save_payloads] == [50, 1]
    assert save_payloads[0]["fid_token_list"] == [f"t{i}" for i in range(50)]
    assert save_payloads[1]["fid_list"] == ["f50"]
    assert save_payloads[0]["to_pdir_fid"] == "dir-1"
    assert saved == [f"saved-f{i}" for i in range(51)]


def test_save_files_polls_task_until_status_2(monkeypatch):
    """转存任务 status=1 时继续轮询，status=2 后取回 fid。"""
    poll_statuses = []

    def handler(method, url, kwargs):
        """第一次轮询返回进行中，第二次返回完成。"""
        if method == "GET" and url.endswith("/file/sort"):
            return _Resp({"status": 200, "data": {"count": 1, "list": [_folder_row()]}})
        if method == "POST" and url.endswith("/share/sharepage/save"):
            return _Resp(_save_data("task-1"))
        if method == "GET" and url.endswith("/task"):
            poll_statuses.append(kwargs["params"]["task_id"])
            if len(poll_statuses) == 1:
                return _Resp(_task_data(1))
            return _Resp(_task_data(2, ["saved-f1"]))
        return _Resp({"status": 500})

    monkeypatch.setattr(quark_module.time, "sleep", lambda *_: None)
    _install_request(monkeypatch, handler)
    item = ShareFile(fid="f1", name="a.mkv", size=1, share_token="t1")
    assert QuarkShareClient().save_files("pwd", "tk", [item]) == ["saved-f1"]
    assert poll_statuses == ["task-1", "task-1"]


def test_save_files_falls_back_to_folder_list_by_name(monkeypatch):
    """缺少 save_as_top_fids 时按转存目录文件名兜底并保持入参顺序。"""
    def handler(method, url, kwargs):
        """save 与 task 都不返回 fid，目录列表按名称反查。"""
        if method == "GET" and url.endswith("/file/sort"):
            if kwargs["params"]["pdir_fid"] == "0":
                return _Resp({"status": 200, "data": {"count": 1, "list": [_folder_row()]}})
            return _Resp({
                "status": 200,
                "data": {
                    "count": 2,
                    "list": [
                        {"fid": "list-f2", "file_name": "b.mkv", "dir": False},
                        {"fid": "list-f1", "file_name": "a.mkv", "dir": False},
                    ],
                },
            })
        if method == "POST" and url.endswith("/share/sharepage/save"):
            return _Resp(_save_data("task-1"))
        if method == "GET" and url.endswith("/task"):
            return _Resp(_task_data(2))
        return _Resp({"status": 500})

    monkeypatch.setattr(quark_module.time, "sleep", lambda *_: None)
    _install_request(monkeypatch, handler)
    files = [
        ShareFile(fid="old-1", name="a.mkv", size=1, share_token="t1"),
        ShareFile(fid="old-2", name="b.mkv", size=1, share_token="t2"),
    ]
    saved = QuarkShareClient().save_files("pwd", "tk", files)
    assert saved == ["list-f1", "list-f2"]


def test_download_url_saves_then_downloads_and_cleans(monkeypatch):
    """download_url 串行执行转存、取链、清理，并使用转存后的 fid。"""
    captured = {}

    def handler(method, url, kwargs):
        """记录转存、取链和删除请求。"""
        if method == "POST" and url.endswith("/token"):
            return _Resp({"status": 200, "data": {"stoken": "tk"}})
        if method == "GET" and url.endswith("/file/sort"):
            return _Resp({"status": 200, "data": {"count": 1, "list": [_folder_row()]}})
        if method == "POST" and url.endswith("/share/sharepage/save"):
            captured["save"] = kwargs.get("json")
            return _Resp(_save_data("task-1", ["saved-f1"]))
        if method == "GET" and url.endswith("/task"):
            return _Resp(_task_data(2))
        if method == "POST" and url.endswith("/file/download"):
            captured["download"] = kwargs.get("json")
            return _Resp({"status": 200, "data": [{"download_url": "https://dl.example/file"}]})
        if method == "POST" and url.endswith("/file/delete"):
            captured["delete"] = kwargs.get("json")
            return _Resp({"status": 200})
        return _Resp({"status": 500})

    monkeypatch.setattr(quark_module.time, "sleep", lambda *_: None)
    _install_request(monkeypatch, handler)
    client = QuarkShareClient()
    item = ShareFile(fid="f1", name="a.mkv", size=1, share_token="file-token")
    url, headers = client.download_url("https://pan.quark.cn/s/abcd", "1234", item)
    assert url == "https://dl.example/file"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert captured["save"]["fid_list"] == ["f1"]
    assert captured["save"]["fid_token_list"] == ["file-token"]
    assert captured["download"]["fids"] == ["saved-f1"]
    assert captured["delete"] == {"filelist": ["saved-f1"], "action_type": 2}


def test_delete_files_failure_only_warns(monkeypatch):
    """删除副本失败不向上抛异常。"""
    client = QuarkShareClient()

    def boom(*args, **kwargs):
        """模拟删除接口异常。"""
        raise RuntimeError("network down")

    monkeypatch.setattr(client, "_call", boom)
    assert client.delete_files(["f1"]) is False


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
