"""插件判断题单元测试：选集、集号解析、页面兜底与下载幂等。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import DOWNLOAD_TASK_ALREADY_EXISTS
from quarkchannel import QuarkChannel
from quarkchannel.channels import ChannelStore
from quarkchannel.downloader import STATE_COMPLETED, STATE_DOWNLOADING, STATE_ERROR
from quarkchannel.sources import pansou as pansou_module
from quarkchannel.sources.pansou import PansouSource
from quarkchannel.sources.tgchannel import TgIndex
from quarkchannel.torrents import ShareEntry, TorrentCache, build_sid, render_torrent


class _Cache:
    """只读分享缓存桩。"""

    def __init__(self, entry: ShareEntry):
        self._entry = entry

    def get(self, sid: str):
        """无论 sid 都返回预设快照。"""
        return self._entry if sid == self._entry.sid else None


class _Pansou:
    """返回固定候选的盘搜桩。"""

    ready = True

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = []

    def search(self, keyword, limit=20, bypass_cache=False):
        """记录调用并返回预设候选。"""
        self.calls.append((keyword, limit, bypass_cache))
        return list(self.rows)


class _PansouResponse:
    """盘搜 HTTP 响应桩。"""

    status_code = 200
    payload = {
        "data": [
            {"title": "缓存资源", "url": "https://pan.quark.cn/s/cache", "size": 123},
        ],
    }

    def json(self):
        """返回固定 JSON。"""
        return self.payload


class _Manager:
    """最小任务管理器桩，模拟 DownloadManager 的幂等提交。"""

    def __init__(self):
        self.tasks = {}
        self.submit_count = 0

    def get(self, task_hash: str):
        """按 hash 取任务。"""
        return self.tasks.get(task_hash)

    def submit(self, task, share_url, passcode):
        """登记任务；下载中或已完成时复用原任务。"""
        self.submit_count += 1
        existing = self.tasks.get(task.hash)
        if existing and existing.state in {STATE_DOWNLOADING, STATE_COMPLETED}:
            return existing
        self.tasks[task.hash] = task
        return task


def _entry() -> ShareEntry:
    """构造带两集与一个无集号文件的小分享。"""
    return ShareEntry(
        sid="sid-1",
        provider="quark",
        share_url="https://pan.quark.cn/s/abc",
        title="演示剧集",
        files=[
            {"name": "S01E01.mkv", "size": 10, "fid": "f1", "share_token": "t1"},
            {"name": "S01E02.mkv", "size": 20, "fid": "f2", "share_token": "t2"},
            {"name": "花絮.mkv", "size": 5, "fid": "f3", "share_token": "t3"},
        ],
        total_size=35,
    )


def test_select_files_returns_empty_when_episode_misses():
    """episodes 非空但无文件命中时返回空，不能回退成整包。"""
    entry = _entry()
    assert [item["name"] for item in QuarkChannel._select_files(entry, {2})] == ["S01E02.mkv"]
    assert QuarkChannel._select_files(entry, {9}) == []
    assert len(QuarkChannel._select_files(entry, None)) == 3


def test_select_files_keeps_all_when_episode_set_unparsable():
    """episodes 集合里没有数字时维持旧语义：保留全部文件。"""
    entry = _entry()
    assert len(QuarkChannel._select_files(entry, {"abc"})) == 3


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Show.S01E01-E02.1080p.mkv", 1),
        ("Show.EP01-02.mkv", 1),
        ("Show.01-02.mkv", 1),
        ("Show.S02E05.mkv", 5),
        ("剧集 第3集.mp4", 3),
        ("Show.[04].mkv", 4),
        ("Show.1080p.mkv", None),
    ],
)
def test_episode_of_supports_ranges_first_episode(name, expected):
    """连集写法返回起始集号，普通写法保持原有解析。"""
    assert QuarkChannel._episode_of(name) == expected


def test_get_page_without_manager_uses_empty_tasks():
    """插件未初始化（_manager 为 None）时 get_page 仍可返回页面。"""
    plugin = QuarkChannel()
    plugin._manager = None
    plugin._pansou = None
    plugin._config = {}
    page = plugin.get_page()
    assert isinstance(page, list) and page
    assert "任务 0 个" in page[0]["props"]["text"]


def test_download_is_idempotent_for_existing_task(tmp_path: Path):
    """同一 hash 已在下载或已完成时直接返回原任务并标记已存在。"""
    entry = _entry()
    content = render_torrent(entry)
    manager = _Manager()
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"download_root": str(tmp_path / "downloads")}
    plugin._cache = _Cache(entry)
    plugin._manager = manager

    first = plugin.download(content, tmp_path)
    assert first[0] == "网盘直链"
    assert first[3] == ""
    assert manager.submit_count == 1

    second = plugin.download(content, tmp_path)
    assert second == ("网盘直链", first[1], "", DOWNLOAD_TASK_ALREADY_EXISTS)
    assert manager.submit_count == 1

    manager.tasks[first[1]].state = STATE_COMPLETED
    third = plugin.download(content, tmp_path)
    assert third == ("网盘直链", first[1], "", DOWNLOAD_TASK_ALREADY_EXISTS)
    assert manager.submit_count == 1


def test_download_retries_after_error_and_rejects_unmatched_episode(tmp_path: Path):
    """失败任务可重新提交；无匹配集数时不提交任何任务。"""
    entry = _entry()
    content = render_torrent(entry)
    manager = _Manager()
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"download_root": str(tmp_path / "downloads")}
    plugin._cache = _Cache(entry)
    plugin._manager = manager

    first = plugin.download(content, tmp_path)
    manager.tasks[first[1]].state = STATE_ERROR
    retried = plugin.download(content, tmp_path)
    assert retried[3] == ""
    assert manager.submit_count == 2

    rejected = plugin.download(content, tmp_path, episodes={99})
    assert rejected == (None, None, None, "没有匹配到需要下载的文件")
    assert manager.submit_count == 2


def test_search_prewarms_first_candidates_inline(tmp_path: Path, monkeypatch):
    """搜索链对前几个候选同步取一次清单，让单集能立即展开。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 3}
    plugin._secret = "secret"
    plugin._cache = TorrentCache(tmp_path)
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": "https://pan.quark.cn/s/fast", "title": "快速资源",
         "size": 512, "source": "pansou"},
    ])
    calls = []
    monkeypatch.setattr(plugin, "_ensure_entry", lambda *args: calls.append(args))
    try:
        rows = plugin.search_torrents(keyword="快速")
        assert len(rows) == 1
        assert rows[0].size == 512.0
        # 拿不到清单时退回整包，但仍会尝试过一次
        assert len(calls) == 1
    finally:
        plugin._stop_prewarm()


def test_pansou_search_post_json_and_cache_hit(monkeypatch):
    """pansou 使用 POST JSON，第二次相同查询命中缓存且不再请求。"""
    class _FakeRequestUtils:
        calls = []
        last_json = None

        def post_res(self, *args, **kwargs):
            type(self).calls.append(args)
            type(self).last_json = kwargs.get("json")
            return _PansouResponse()

    monkeypatch.setattr(pansou_module, "RequestUtils", _FakeRequestUtils)
    source = PansouSource(
        "http://pansou:8888", token="token", plugins="a,b", cloud_types="quark",
    )
    first = source.search("关键词", limit=1)
    second = source.search("关键词", limit=1)
    assert len(_FakeRequestUtils.calls) == 1
    expected = _PansouResponse.payload["data"][0] | {"passcode": "", "source": "pansou"}
    assert first == second == [expected]
    assert _FakeRequestUtils.last_json == {
        "kw": "关键词",
        "res": "merge",
        "src": "all",
        "plugins": ["a", "b"],
        "cloud_types": ["quark"],
    }

    source.search("关键词", limit=1, bypass_cache=True)
    assert len(_FakeRequestUtils.calls) == 2


def test_search_expands_cached_episodes_with_fid():
    """缓存命中时按集展开，enclosure 带 fid 且 pansou 使用 pri_order=5。"""
    url = "https://pan.quark.cn/s/expand"
    sid = build_sid(url)
    entry = ShareEntry(
        sid=sid,
        provider="quark",
        share_url=url,
        title="演示剧集",
        files=[
            {"name": "Show.S01E01.mkv", "size": 101, "fid": "f1"},
            {"name": "Show.S01E02.mkv", "size": 202, "fid": "f2"},
            {"name": "Show.S01E03.mkv", "size": 303, "fid": "f3"},
        ],
        total_size=606,
    )
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5, "expand_in_search": True}
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": url, "title": "演示剧集", "size": 606, "source": "pansou"},
    ])
    rows = plugin.search_torrents(keyword="演示")
    # 整包在前，后面是展开出的单集
    assert [item.title for item in rows] == [
        "演示剧集", "演示 S01E01", "演示 S01E02", "演示 S01E03",
    ]
    assert [item.size for item in rows] == [606.0, 101.0, 202.0, 303.0]
    assert [item.pri_order for item in rows] == [5, 5, 5, 5]
    assert "fid=" not in rows[0].enclosure
    assert ["fid=f1" in rows[1].enclosure, "fid=f2" in rows[2].enclosure,
            "fid=f3" in rows[3].enclosure] == [True, True, True]


def test_search_collapses_each_share_when_expand_disabled():
    """默认关闭展开时，一个分享只返回一条整包候选。"""
    url = "https://pan.quark.cn/s/collapse"
    sid = build_sid(url)
    entry = ShareEntry(
        sid=sid,
        provider="quark",
        share_url=url,
        title="聚合剧集",
        files=[
            {"name": "Show.S01E01.mkv", "size": 101, "fid": "f1"},
            {"name": "Show.S01E02.mkv", "size": 202, "fid": "f2"},
        ],
        total_size=303,
    )
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5, "expand_in_search": False}
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    plugin._tg_index = None
    plugin._pansou = _Pansou([{"url": url, "title": "聚合剧集", "size": 999,
                               "source": "pansou"}])
    rows = plugin.search_torrents(keyword="聚合")
    assert len(rows) == 1
    assert rows[0].title == "聚合剧集"
    assert rows[0].size == 303.0
    assert "2 个文件 · 可在插件内选集" in rows[0].description
    assert "fid=" not in rows[0].enclosure


def test_search_without_cache_returns_single_whole_candidate():
    """没有快照时只返回一条整包候选，大小来自 pansou，未知则为 0。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._cache = None
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": "https://pan.quark.cn/s/plain", "title": "整包资源",
         "size": 4096, "source": "pansou"},
    ])
    rows = plugin.search_torrents(keyword="资源")
    assert len(rows) == 1
    assert rows[0].title == "整包资源"
    assert rows[0].size == 4096.0
    assert "fid=" not in rows[0].enclosure


def test_search_does_not_expand_movie_snapshot():
    """文件数不超过 2 且无集号的电影保持一条整包候选。"""
    url = "https://pan.quark.cn/s/movie"
    sid = build_sid(url)
    entry = ShareEntry(
        sid=sid,
        provider="quark",
        share_url=url,
        title="演示电影",
        files=[
            {"name": "Movie.2160p.mkv", "size": 1000, "fid": "m1"},
            {"name": "Movie.1080p.mkv", "size": 800, "fid": "m2"},
        ],
        total_size=1800,
    )
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5, "expand_in_search": True}
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    plugin._tg_index = None
    plugin._pansou = _Pansou([{"url": url, "title": "演示电影", "source": "pansou"}])
    rows = plugin.search_torrents(keyword="电影")
    assert len(rows) == 1
    assert rows[0].title == "演示电影"
    assert rows[0].size == 1800.0
    assert "fid=" not in rows[0].enclosure


def test_search_dedupes_episode_and_keeps_largest_file():
    """同一集号只保留体积最大的文件，并按集号输出。"""
    url = "https://pan.quark.cn/s/dupep"
    sid = build_sid(url)
    entry = ShareEntry(
        sid=sid,
        provider="quark",
        share_url=url,
        title="重复集",
        files=[
            {"name": "Show.S01E01.720p.mkv", "size": 100, "fid": "small"},
            {"name": "Show.S01E01.1080p.mkv", "size": 300, "fid": "large"},
            {"name": "Show.S01E02.mkv", "size": 200, "fid": "e2"},
        ],
        total_size=600,
    )
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5, "expand_in_search": True}
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    plugin._tg_index = None
    plugin._pansou = _Pansou([{"url": url, "title": "重复集", "source": "pansou"}])
    rows = plugin.search_torrents(keyword="重复")
    # 1 条整包 + 2 条单集（同集号只保留体积最大的文件）
    assert len(rows) == 3
    assert "fid=" not in rows[0].enclosure
    assert rows[1].size == 300.0 and "fid=large" in rows[1].enclosure
    assert rows[2].size == 200.0 and "fid=e2" in rows[2].enclosure


def test_search_falls_back_to_legacy_when_channels_empty(tmp_path: Path):
    """channels.json 为空时仍走旧 pansou，避免升级即断。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 3}
    plugin._secret = "secret"
    plugin._cache = None
    plugin._channels = ChannelStore(tmp_path)
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": "https://pan.quark.cn/s/legacy", "title": "旧资源", "source": "pansou"},
    ])
    rows = plugin.search_torrents(keyword="资源")
    assert [item.title for item in rows] == ["旧资源"]
    assert rows[0].pri_order == 0


def test_search_uses_channel_priority_and_dedup(tmp_path: Path, monkeypatch):
    """多渠道按 priority 合并，禁用渠道不参与，重复 sid 只保留高优先级。"""
    store = ChannelStore(tmp_path)
    store.create({
        "name": "TG 高优", "type": "tg_channel", "enabled": True, "priority": 80,
        "config": {"channel": "@high"},
    })
    store.create({
        "name": "盘搜", "type": "pansou", "enabled": True, "priority": 50,
        "config": {"base_url": "http://pansou:8888"},
    })
    store.create({
        "name": "禁用高优", "type": "pansou", "enabled": False, "priority": 99,
        "config": {"base_url": "http://disabled:8888"},
    })
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._cache = None
    plugin._channels = store
    plugin._tg_index = TgIndex(tmp_path)
    plugin._tg_index.add_entries([
        {"sid": "same", "title": "合集资源", "channel": "@high",
         "share_url": "https://pan.quark.cn/s/same", "msg_id": 2},
        {"sid": "low", "title": "合集资源", "channel": "@low",
         "share_url": "https://pan.quark.cn/s/low", "msg_id": 1},
    ])
    monkeypatch.setattr(
        PansouSource, "search",
        lambda self, keyword, limit=20, bypass_cache=False: [
            {"url": "https://pan.quark.cn/s/same", "title": "重复", "source": "pansou"},
            {"url": "https://pan.quark.cn/s/pan", "title": "盘搜资源", "source": "pansou"},
        ],
    )
    rows = plugin.search_torrents(keyword="合集")
    assert [item.title for item in rows] == ["合集资源", "盘搜资源"]
    assert [item.pri_order for item in rows] == [80, 50]
    assert rows[0].site_name == "@high"


def test_search_tg_channel_only_returns_own_entries(tmp_path: Path):
    """单个 tg_channel 渠道只搜自己频道，索引里其它频道不串入。"""
    store = ChannelStore(tmp_path)
    store.create({
        "name": "@own", "type": "tg_channel", "enabled": True, "priority": 10,
        "config": {"channel": "@own"},
    })
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._cache = None
    plugin._channels = store
    plugin._tg_index = TgIndex(tmp_path)
    plugin._tg_index.add_entries([
        {"sid": "own", "title": "同标题", "channel": "@own",
         "share_url": "https://pan.quark.cn/s/own", "msg_id": 2},
        {"sid": "other", "title": "同标题", "channel": "@other",
         "share_url": "https://pan.quark.cn/s/other", "msg_id": 1},
    ])
    rows = plugin.search_torrents(keyword="同标题")
    assert [item.page_url for item in rows] == ["https://pan.quark.cn/s/own"]


def test_search_does_not_fall_back_when_channels_only_disabled(tmp_path: Path):
    """存在渠道但全部禁用时返回空，不能回退旧全局 pansou。"""
    store = ChannelStore(tmp_path)
    store.create({
        "name": "停用盘搜", "type": "pansou", "enabled": False, "priority": 99,
        "config": {"base_url": "http://disabled:8888"},
    })
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5}
    plugin._secret = "secret"
    plugin._cache = None
    plugin._channels = store
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": "https://pan.quark.cn/s/legacy", "title": "不该返回", "source": "pansou"},
    ])
    assert plugin.search_torrents(keyword="资源") == []


def test_sidebar_and_render_mode_contract():
    """侧栏入口与 Vue 远端页面契约固定。"""
    plugin = QuarkChannel()
    assert plugin.get_render_mode() == ("vue", "dist/assets")
    assert plugin.get_sidebar_nav() == [{
        "nav_key": "channels",
        "title": "渠道管理",
        "icon": "mdi-rss",
        "section": "system",
        "permission": "manage",
        "order": 6,
    }]


def test_channel_api_crud_and_summary(tmp_path: Path, monkeypatch):
    """渠道 API 完成新增、列表、更新、探活、汇总与删除。"""
    plugin = QuarkChannel()
    plugin._channels = ChannelStore(tmp_path)
    plugin._tg_index = TgIndex(tmp_path)
    created = plugin.api_channel_create({
        "name": "盘搜", "type": "pansou", "priority": 20,
        "config": {"base_url": "http://pansou:8888"},
    })
    assert created["success"] is True
    channel_id = created["data"]["id"]
    assert plugin.api_channels()["data"]["items"][0]["id"] == channel_id

    monkeypatch.setattr(PansouSource, "check_auth", lambda self: (True, ""))
    probe = plugin.api_channel_test(channel_id)
    assert probe["success"] is True

    updated = plugin.api_channel_update(channel_id, {"priority": 60})
    assert updated["success"] is True
    assert updated["data"]["priority"] == 60
    summary = plugin.api_channels_summary()["data"]
    assert summary["total"] == 1 and summary["enabled"] == 1
    assert plugin.api_channel_delete(channel_id)["success"] is True
    assert plugin.api_channel_delete(channel_id)["success"] is False


def test_channel_monitors_dedupe_and_share_index(tmp_path: Path, monkeypatch):
    """每个启用频道一个监控，同频道重复渠道只保留高优先级。"""
    store = ChannelStore(tmp_path)
    store.create({"name": "高", "type": "tg_channel", "priority": 90,
                  "config": {"channel": "@same"}})
    store.create({"name": "低", "type": "tg_channel", "priority": 70,
                  "config": {"channel": "@same"}})
    store.create({"name": "其它", "type": "tg_channel", "priority": 60,
                  "config": {"channel": "@other"}})
    plugin = QuarkChannel()
    plugin._channels = store
    plugin._tg_index = TgIndex(tmp_path)
    plugin._config = {
        "tg_api_id": 1, "tg_api_hash": "hash", "tg_session": "session",
    }
    created = []

    class _Monitor:
        def __init__(self, data_dir, api_id, api_hash, session, channels, **kwargs):
            self.channels = channels
            self.index = kwargs.get("index")
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            return None

    monkeypatch.setattr("quarkchannel.TgMonitor", _Monitor)
    plugin._start_tg_runtime()
    assert [item.channels for item in created] == [["@same"], ["@other"]]
    assert all(item.started for item in created)
    assert all(item.index is plugin._tg_index for item in created)


def test_init_configures_auto_picker_and_api(tmp_path: Path):
    """插件初始化读取自动追更配置，状态与决策接口可正常返回。"""
    plugin = QuarkChannel()
    plugin.get_data_path = lambda: tmp_path
    try:
        plugin.init_plugin({
            "enabled": False,
            "auto_pick_enabled": True,
            "auto_pick_dry_run": False,
            "auto_pick_min_similarity": 0.5,
            "auto_pick_downloader": "测试下载器",
        })
        picker = plugin._picker
        assert picker is not None
        assert picker.enabled is True and picker.dry_run is False
        assert picker.matcher.min_similarity == 0.5
        assert picker.downloader == "测试下载器"
        assert plugin.api_auto_pick()["data"]["enabled"] is True
        assert plugin.api_status()["data"]["auto_pick_enabled"] is True
    finally:
        plugin.stop_service()
