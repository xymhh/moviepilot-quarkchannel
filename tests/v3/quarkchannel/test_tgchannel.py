"""Telegram 频道监控单元测试：全部使用假消息与假客户端，不联网。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from quarkchannel import QuarkChannel
from quarkchannel.sources import tgchannel as tg_module
from quarkchannel.sources.tgchannel import TgIndex, TgMonitor
from quarkchannel.torrents import ShareEntry


class _Message:
    """最小 Telegram 消息桩。"""

    def __init__(self, msg_id: int, text: str, created_at: float | None = None):
        self.id = msg_id
        self.message = text
        self.date = created_at if created_at is not None else time.time()


class _FakeClient:
    """按调用参数返回固定消息列表的客户端桩。"""

    def __init__(self, messages: list[_Message], fail_times: int = 0,
                 error: Exception | None = None):
        self.messages = messages
        self.fail_times = fail_times
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.failures = 0

    def iter_messages(self, entity, **kwargs):
        """记录调用；需要时先抛出一次异常。"""
        self.calls.append((entity, dict(kwargs)))
        if self.error is not None and self.failures < self.fail_times:
            self.failures += 1
            raise self.error
        rows = [item for item in self.messages
                if int(getattr(item, "id", 0)) > int(kwargs.get("min_id") or 0)]
        if not kwargs.get("reverse"):
            rows = list(reversed(rows))
        limit = kwargs.get("limit")
        return rows[: int(limit)] if limit else rows


class _FakeIndex:
    """搜索合并测试用的索引桩。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = []

    def search(self, keyword, limit=5):
        """返回预设命中。"""
        self.calls.append((keyword, limit))
        return list(self.rows)


class _FakePansou:
    """搜索合并测试用的盘搜桩。"""

    ready = True

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = []

    def search(self, keyword, limit=20):
        """返回预设命中。"""
        self.calls.append((keyword, limit))
        return list(self.rows)


def _entry(sid: str, title: str) -> ShareEntry:
    """构造可包装成种子的分享快照。"""
    return ShareEntry(
        sid=sid,
        provider="quark",
        share_url=f"https://pan.quark.cn/s/{sid}",
        title=title,
        files=[{"name": f"{title}.mkv", "size": 10, "fid": "f1", "share_token": "t1"}],
        total_size=10,
    )


def test_parse_message_with_passcode():
    """单链接消息解析出标题、sid、提取码与时间。"""
    stamp = time.time() - 100
    rows = TgMonitor.parse_message(
        "演示电影\nhttps://pan.quark.cn/s/abc123 提取码: x9y8", "chan", 7, stamp,
    )
    assert len(rows) == 1
    assert rows[0]["sid"] == "abc123"
    assert rows[0]["share_url"] == "https://pan.quark.cn/s/abc123"
    assert rows[0]["passcode"] == "x9y8"
    assert rows[0]["title"] == "演示电影"
    assert rows[0]["channel"] == "chan"
    assert rows[0]["msg_id"] == 7
    assert rows[0]["created_at"] == stamp


def test_parse_message_multiple_links_dedupes():
    """多链接消息按 sid 去重，并复用同一提取码。"""
    text = (
        "[合集] https://pan.quark.cn/s/one?pwd=abcd\n"
        "https://pan.quark.cn/s/two\n"
        "https://pan.quark.cn/s/one\n"
    )
    rows = TgMonitor.parse_message(text, "chan", 8)
    assert [item["sid"] for item in rows] == ["one", "two"]
    assert {item["passcode"] for item in rows} == {"abcd"}
    assert all(item["title"] == "[合集]" for item in rows)


def test_parse_message_without_link_is_ignored():
    """纯文本消息不产生索引条目。"""
    assert TgMonitor.parse_message("今天没有资源", "chan", 9) == []
    assert TgMonitor._parse_message("也没有 https://example.com/a", "chan", 10) == []


def test_poll_once_sets_watermark_on_first_run(tmp_path):
    """首次接入只把游标定位到最新消息，不回补历史。"""
    monitor = TgMonitor(tmp_path, 1, "hash", "session", ["chan"])
    client = _FakeClient([
        _Message(1, "A\nhttps://pan.quark.cn/s/a"),
        _Message(2, "无链接"),
        _Message(3, "B\nhttps://pan.quark.cn/s/b"),
    ])
    added = asyncio.run(monitor.poll_once(client))
    assert added == []
    assert monitor.index.cursor["chan"] == 3
    assert monitor.index.entries == []
    assert client.calls[0][0] == "chan"
    assert client.calls[0][1] == {"limit": 100}


def test_poll_once_advances_cursor_and_dedupes(tmp_path):
    """水位之后的新消息才入索引，重复扫描不重复入索引。"""
    monitor = TgMonitor(tmp_path, 1, "hash", "session", ["chan"])
    monitor.index.set_cursor("chan", 3)
    client = _FakeClient([
        _Message(4, "C\nhttps://pan.quark.cn/s/c"),
        _Message(5, "D\nhttps://pan.quark.cn/s/d"),
    ])
    added = asyncio.run(monitor.poll_once(client))
    assert {item["sid"] for item in added} == {"c", "d"}
    assert monitor.index.cursor["chan"] == 5
    assert client.calls[0][1] == {"min_id": 3, "limit": 100, "reverse": True}

    again = asyncio.run(monitor.poll_once(client))
    assert again == []
    assert {item["sid"] for item in monitor.index.entries} == {"c", "d"}
    assert client.calls[1][1]["min_id"] == 5


def test_poll_once_skips_messages_at_or_below_cursor(tmp_path):
    """即使客户端返回旧消息，也不会处理并回退游标。"""
    monitor = TgMonitor(tmp_path, 1, "hash", "session", ["chan"])
    monitor.index.set_cursor("chan", 5)
    client = _FakeClient([
        _Message(4, "旧\nhttps://pan.quark.cn/s/old"),
        _Message(5, "边界\nhttps://pan.quark.cn/s/edge"),
        _Message(6, "新\nhttps://pan.quark.cn/s/new"),
    ])
    added = asyncio.run(monitor.poll_once(client))
    assert [item["sid"] for item in added] == ["new"]
    assert monitor.index.cursor["chan"] == 6
    assert "old" not in {item["sid"] for item in monitor.index.entries}
    assert client.calls[0][1]["min_id"] == 5


def test_poll_once_flood_wait_sleeps_then_continues(tmp_path):
    """FloodWaitError 睡眠后重试同一频道。"""
    monitor = TgMonitor(tmp_path, 1, "hash", "session", ["chan"])
    monitor.index.set_cursor("chan", 0)

    class FloodWaitError(Exception):
        seconds = 3

    client = _FakeClient(
        [_Message(1, "A\nhttps://pan.quark.cn/s/a")],
        fail_times=1,
        error=FloodWaitError(),
    )
    sleeps = []

    async def fake_sleep(seconds):
        """记录限流等待时间。"""
        sleeps.append(seconds)

    monitor._sleep = fake_sleep
    added = asyncio.run(monitor.poll_once(client))
    assert sleeps == [3]
    assert added == []
    assert monitor.index.cursor["chan"] == 1


def test_poll_once_other_error_keeps_cursor(tmp_path):
    """其它异常只告警，不推进频道游标。"""
    monitor = TgMonitor(tmp_path, 1, "hash", "session", ["chan"])
    monitor.index.set_cursor("chan", 2)
    client = _FakeClient([], fail_times=1, error=RuntimeError("boom"))
    assert asyncio.run(monitor.poll_once(client)) == []
    assert monitor.index.cursor["chan"] == 2


def test_index_search_case_space_and_order(tmp_path):
    """索引搜索忽略大小写与空格，并按时间倒序。"""
    index = TgIndex(tmp_path)
    now = time.time()
    index.add_entries([
        {"sid": "a", "title": "The Big Movie", "created_at": now - 10, "msg_id": 1},
        {"sid": "b", "title": "movie sequel", "created_at": now - 20, "msg_id": 2},
        {"sid": "c", "title": "Other", "created_at": now, "msg_id": 3},
    ])
    rows = index.search("THEBIGMOVIE")
    assert [item["sid"] for item in rows] == ["a"]
    rows = index.search("Mo ViE")
    assert [item["sid"] for item in rows] == ["a", "b"]
    assert index.search("   ") == []


def test_index_purges_expired_and_respects_ttl(tmp_path):
    """14 天外条目清理，搜索的 max_age_days 进一步限制结果。"""
    index = TgIndex(tmp_path)
    now = time.time()
    index.add_entries([
        {"sid": "new", "title": "Show New", "created_at": now, "msg_id": 3},
        {"sid": "mid", "title": "Show Mid", "created_at": now - 2 * 86400, "msg_id": 2},
        {"sid": "old", "title": "Show Old", "created_at": now - 15 * 86400, "msg_id": 1},
    ])
    assert {item["sid"] for item in index.entries} == {"new", "mid"}
    assert [item["sid"] for item in index.search("show")] == ["new", "mid"]
    assert [item["sid"] for item in index.search("show", max_age_days=1)] == ["new"]
    reloaded = TgIndex(tmp_path)
    assert {item["sid"] for item in reloaded.entries} == {"new", "mid"}


def test_index_keeps_latest_entry_for_same_sid(tmp_path):
    """同一 sid 只保留 created_at/msg_id 更新的一条。"""
    index = TgIndex(tmp_path)
    now = time.time()
    index.add_entries([
        {"sid": "same", "title": "旧标题", "created_at": now - 10, "msg_id": 1},
        {"sid": "same", "title": "新标题", "created_at": now, "msg_id": 2},
        {"sid": "same", "title": "更旧", "created_at": now - 20, "msg_id": 0},
    ])
    assert len(index.entries) == 1
    assert index.entries[0]["title"] == "新标题"


def test_search_torrents_merges_tg_and_pansou_by_sid(tmp_path, monkeypatch):
    """索引优先，pansou 同 sid 去重，pri_order 分别为 10/0。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._tg_index = _FakeIndex([
        {"sid": "dup", "share_url": "https://pan.quark.cn/s/dup",
         "passcode": "", "title": "重复", "channel": "tg"},
        {"sid": "tg2", "share_url": "https://pan.quark.cn/s/tg2",
         "passcode": "abcd", "title": "TG 资源", "channel": "tg"},
    ])
    plugin._pansou = _FakePansou([
        {"url": "https://pan.quark.cn/s/dup", "source": "pansou"},
        {"url": "https://pan.quark.cn/s/pan2", "title": "盘搜资源", "source": "pansou"},
    ])
    calls = []
    monkeypatch.setattr(
        plugin, "_ensure_entry", lambda *args: calls.append(args),
    )
    rows = plugin.search_torrents(keyword="资源")
    assert [item.title for item in rows] == ["重复", "TG 资源", "盘搜资源"]
    assert [item.pri_order for item in rows] == [10, 10, 0]
    assert calls == []


def test_search_torrents_tg_hits_limit_before_pansou(tmp_path, monkeypatch):
    """索引命中达到上限时不再请求 pansou。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 2}
    plugin._secret = "secret"
    plugin._tg_index = _FakeIndex([
        {"sid": "a", "share_url": "https://pan.quark.cn/s/a", "title": "A"},
        {"sid": "b", "share_url": "https://pan.quark.cn/s/b", "title": "B"},
    ])
    plugin._pansou = _FakePansou([{"url": "https://pan.quark.cn/s/c", "source": "pansou"}])
    rows = plugin.search_torrents(keyword="x")
    assert [item.title for item in rows] == ["A", "B"]
    assert [item.pri_order for item in rows] == [10, 10]
    assert plugin._pansou.calls == []


def test_search_torrents_returns_pansou_whole_without_snapshot(tmp_path, monkeypatch):
    """pansou 未命中缓存时直接返回整包，不在搜索链取快照。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._tg_index = None
    plugin._pansou = _FakePansou([
        {"url": "https://pan.quark.cn/s/good", "title": "好资源",
         "size": 2048, "source": "pansou"},
    ])
    calls = []
    monkeypatch.setattr(
        plugin, "_ensure_entry", lambda *args: calls.append(args),
    )
    rows = plugin.search_torrents(keyword="资源")
    assert [item.title for item in rows] == ["好资源"]
    assert rows[0].size == 2048.0
    assert rows[0].pri_order == 0
    # 会尝试取一次清单，失败则退回整包
    assert len(calls) == 1


def test_api_status_reports_tg_index(tmp_path):
    """/status 返回索引条目数与最近轮询时间。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {}
    plugin._manager = None
    plugin._pansou = None
    index = TgIndex(tmp_path)
    index.add_entries([
        {"sid": "a", "title": "A", "created_at": time.time(), "msg_id": 1},
    ])
    index.last_poll = 123.0
    plugin._tg_index = index
    data = plugin.api_status()["data"]
    assert data["tg_entries"] == 1
    assert data["tg_last_poll"] == 123.0
