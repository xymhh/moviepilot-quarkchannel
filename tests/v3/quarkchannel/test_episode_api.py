"""分享聚合、集数查询与选集下载接口测试（全 mock）。"""

from __future__ import annotations

import time

from quarkchannel import QuarkChannel
from quarkchannel.downloader import DownloadTask, STATE_DOWNLOADING
from quarkchannel.torrents import ShareEntry, build_sid


class _Cache:
    """按 sid 返回预设分享快照。"""

    def __init__(self, entries: list[ShareEntry]):
        self.entries = {entry.sid: entry for entry in entries}

    def get(self, sid: str):
        """读取快照。"""
        return self.entries.get(sid)


class _Pansou:
    """固定盘搜候选。"""

    ready = True

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def search(self, keyword, limit=20, bypass_cache=False):
        """返回候选列表。"""
        return list(self.rows)


class _Manager:
    """记录提交次数并保留任务。"""

    def __init__(self):
        self.tasks: dict[str, DownloadTask] = {}
        self.submit_calls = 0

    def get(self, task_hash: str):
        """按 hash 返回已有任务。"""
        return self.tasks.get(task_hash)

    def submit(self, task: DownloadTask, share_url: str, passcode: str):
        """保存任务；相同 hash 直接复用。"""
        self.submit_calls += 1
        existing = self.tasks.get(task.hash)
        if existing:
            return existing
        task.share_url = share_url
        task.passcode = passcode
        task.total = sum(item.size for item in task.files)
        task.created_at = time.time()
        task.updated_at = time.time()
        task.state = STATE_DOWNLOADING
        self.tasks[task.hash] = task
        return task


def _entry(url: str = "https://pan.quark.cn/s/episode-api") -> ShareEntry:
    """构造含正常集、乱序集与无集号文件的分享。"""
    return ShareEntry(
        sid=build_sid(url),
        provider="quark",
        share_url=url,
        title="聚合剧集",
        files=[
            {"name": "Show.S01E02.mkv", "size": 202, "fid": "f2", "share_token": "t2"},
            {"name": "花絮.mkv", "size": 50, "fid": "fx", "share_token": "tx"},
            {"name": "Show.S01E01.mkv", "size": 101, "fid": "f1", "share_token": "t1"},
        ],
        total_size=353,
    )


def _plugin(entry: ShareEntry, manager: _Manager | None = None) -> QuarkChannel:
    """构造可直接调用接口的插件实例。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"download_root": "/downloads", "max_results": 5}
    plugin._secret = "secret"
    plugin._cache = _Cache([entry])
    plugin._tg_index = None
    plugin._pansou = None
    plugin._manager = manager
    return plugin


def test_shares_lists_one_item_per_share_without_files():
    """/shares 按分享聚合，files 为文件数且不返回集列表。"""
    entry = _entry()
    plugin = _plugin(entry)
    plugin._pansou = _Pansou([
        {"url": entry.share_url, "title": "聚合剧集", "size": 999, "source": "pansou"},
        {"url": "https://pan.quark.cn/s/no-cache", "title": "无快照",
         "size": 2048, "source": "pansou"},
    ])
    body = plugin.api_shares(keyword="聚合", limit=20)
    items = body["data"]["items"]
    assert len(items) == 2
    assert items[0] == {
        "sid": entry.sid,
        "title": "聚合剧集",
        "size": 353,
        "files": 3,
        "site_name": "pansou",
        "share_url": entry.share_url,
    }
    assert items[1]["files"] == 0
    assert items[1]["size"] == 2048


def test_share_episodes_sorts_numbers_and_puts_other_last():
    """/shares/{sid}/episodes 按集号升序，无集号文件 episode=null 置后。"""
    entry = _entry()
    plugin = _plugin(entry)
    body = plugin.api_share_episodes(entry.sid)
    items = body["data"]["items"]
    assert body["data"]["total"] == 3
    assert [item["episode"] for item in items] == [1, 2, None]
    assert items[0]["fid"] == "f1"
    assert items[-1]["name"] == "花絮.mkv"
    assert items[-1]["episode"] is None


def test_share_episodes_fetches_snapshot_once_on_cache_miss(monkeypatch):
    """快照未命中时按候选登记信息现场取一次。"""
    entry = _entry()
    plugin = _plugin(entry)
    plugin._cache = _Cache([])
    plugin._remember_pending({
        "sid": entry.sid,
        "share_url": entry.share_url,
        "passcode": "",
        "title": entry.title,
    })
    calls: list[tuple] = []
    monkeypatch.setattr(
        plugin, "_ensure_entry",
        lambda url, passcode, title, force=False: calls.append((url, passcode, title)) or entry,
    )
    body = plugin.api_share_episodes(entry.sid)
    assert body["success"] is True
    assert len(calls) == 1
    assert calls[0][0] == entry.share_url


def test_download_episodes_by_episodes_and_idempotent():
    """按 episodes 选集提交，重复提交复用同一 hash。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    first = plugin.api_download_episodes({
        "sid": entry.sid,
        "episodes": [2, 1],
    })
    assert first["success"] is True
    assert first["data"]["count"] == 2
    assert first["data"]["size"] == 303
    task = manager.tasks[first["data"]["hash"]]
    assert [item.fid for item in task.files] == ["f2", "f1"]
    assert task.save_dir == "/downloads"
    assert [item.out_name for item in task.files] == [
        "聚合剧集 S01E02.mkv", "聚合剧集 S01E01.mkv",
    ]
    second = plugin.api_download_episodes({
        "sid": entry.sid,
        "episodes": [1, 2],
    })
    assert second["data"]["hash"] == first["data"]["hash"]
    assert manager.submit_calls == 1


def test_download_episodes_by_fids():
    """按 fids 精确选集，只提交指定文件。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    body = plugin.api_download_episodes({
        "sid": entry.sid,
        "fids": ["fx", "f1", "fx"],
    })
    assert body["success"] is True
    assert body["data"]["count"] == 2
    assert body["data"]["size"] == 151
    task = manager.tasks[body["data"]["hash"]]
    assert [item.fid for item in task.files] == ["fx", "f1"]


def test_download_episodes_whole_package_when_empty():
    """episodes/fids 都为空时提交整包所有文件。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    body = plugin.api_download_episodes({"sid": entry.sid})
    assert body["success"] is True
    assert body["data"]["count"] == 3
    assert body["data"]["size"] == 353
    task = manager.tasks[body["data"]["hash"]]
    assert [item.fid for item in task.files] == ["f2", "fx", "f1"]


def test_download_episodes_rejects_both_selectors_and_unknown_files():
    """episodes 与 fids 同时给出或没有命中文件时返回失败。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    both = plugin.api_download_episodes({
        "sid": entry.sid,
        "episodes": [1],
        "fids": ["f1"],
    })
    assert both["success"] is False
    missing = plugin.api_download_episodes({
        "sid": entry.sid,
        "fids": ["missing"],
    })
    assert missing["success"] is False
    assert manager.submit_calls == 0


class _Channels:
    """固定渠道列表。"""

    def list(self):
        """返回一个启用渠道。"""
        return [{"id": "ch1", "type": "pansou", "enabled": True, "priority": 10,
                 "config": {}}]


def test_status_and_form_expose_expand_in_search():
    """默认配置、/status 与 Python 表单都包含 expand_in_search。"""
    plugin = _plugin(_entry())
    plugin._config = {}
    plugin._manager = None
    plugin._cache = None
    assert plugin.api_status()["data"]["expand_in_search"] is True
    plugin._config = {"expand_in_search": True}
    assert plugin.api_status()["data"]["expand_in_search"] is True
    form, defaults = plugin.get_form()
    assert defaults["expand_in_search"] is True
    assert defaults["expand_max_files"] == 0
    assert "expand_in_search" in str(form)


def test_get_api_registers_share_routes():
    """三个新接口均注册并要求 bearer 鉴权。"""
    plugin = QuarkChannel()
    routes = {(row["path"], tuple(row["methods"])): row for row in plugin.get_api()}
    assert routes[("/shares", ("GET",))]["auth"] == "bear"
    assert routes[("/shares/{sid}/episodes", ("GET",))]["auth"] == "bear"
    assert routes[("/download_episodes", ("POST",))]["auth"] == "bear"


def test_shares_empty_keyword_and_no_sources_returns_empty():
    """/shares 在空关键词或无可用源时返回空列表。"""
    plugin = _plugin(_entry())
    assert plugin.api_shares(keyword="")["data"]["items"] == []
    assert plugin.api_shares(keyword="无源")["data"]["items"] == []


def test_shares_uses_alt_lookup_and_clamps_limit(monkeypatch):
    """/shares 支持渠道原始 sid 二次查表，并把 limit 限制在 100。"""
    entry = _entry()
    plugin = _plugin(entry)
    plugin._channels = _Channels()
    captured: list[int] = []
    candidate = {
        "sid": "legacy-sid",
        "lookup_sids": [entry.sid],
        "share_url": entry.share_url,
        "title": entry.title,
        "site_name": "tg",
        "size": 1,
    }
    monkeypatch.setattr(
        plugin, "_collect_channel_candidates",
        lambda channels, keyword, limit: captured.append(limit) or [candidate],
    )
    body = plugin.api_shares(keyword="聚合", limit=999)
    assert captured == [100]
    item = body["data"]["items"][0]
    assert item["sid"] == entry.sid
    assert item["files"] == 3
    assert item["size"] == 353


def test_share_episodes_rejects_empty_and_unknown_sid():
    """/shares/{sid}/episodes 对空 sid 与未知 sid 返回失败。"""
    plugin = _plugin(_entry())
    assert plugin.api_share_episodes("")["success"] is False
    assert plugin.api_share_episodes("missing")["success"] is False


def test_download_episodes_fetches_snapshot_on_cache_miss(monkeypatch):
    """下载时快照未命中会按候选登记信息现场取一次。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    plugin._cache = _Cache([])
    plugin._remember_pending({
        "sid": entry.sid,
        "share_url": entry.share_url,
        "passcode": "1234",
        "title": entry.title,
    })
    calls: list[tuple] = []
    monkeypatch.setattr(
        plugin, "_ensure_entry",
        lambda url, passcode, title, force=False: calls.append((url, passcode)) or entry,
    )
    body = plugin.api_download_episodes({"sid": entry.sid, "episodes": [1]})
    assert body["success"] is True
    assert calls == [(entry.share_url, "1234")]
    assert manager.submit_calls == 1


def test_download_episodes_rejects_invalid_episode_values():
    """episodes 全部非法时不回退为整包下载。"""
    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    body = plugin.api_download_episodes({
        "sid": entry.sid,
        "episodes": ["abc", 0, None],
    })
    assert body["success"] is False
    assert manager.submit_calls == 0


def test_download_episodes_requires_initialized_manager():
    """下载管理器未初始化时返回失败。"""
    entry = _entry()
    plugin = _plugin(entry)
    body = plugin.api_download_episodes({"sid": entry.sid})
    assert body["success"] is False
    assert "未初始化" in body["message"]


def test_download_episodes_retries_after_error_state():
    """同一批文件在失败状态下允许重新提交。"""
    from quarkchannel.downloader import STATE_ERROR

    entry = _entry()
    manager = _Manager()
    plugin = _plugin(entry, manager)
    first = plugin.api_download_episodes({"sid": entry.sid, "episodes": [1]})
    task = manager.tasks[first["data"]["hash"]]
    task.state = STATE_ERROR
    second = plugin.api_download_episodes({"sid": entry.sid, "episodes": [1]})
    assert second["data"]["hash"] == first["data"]["hash"]
    assert manager.submit_calls == 2
