"""自动追更决策单元测试：全 mock，不访问网络、数据库与下载器。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarkchannel.picker import (
    AutoPickStore,
    AutoPicker,
    EpisodePlan,
    MissingEpisodeResolver,
    SubscriptionMatcher,
)
from quarkchannel.sources.tgchannel import TgMonitor
from quarkchannel.torrents import ShareEntry


class _Subscription:
    """最小订阅对象。"""

    def __init__(self, name: str, season: int = 1, enabled: bool = True,
                 score_id: str = "100", mtype: str = "电视剧"):
        self.name = name
        self.season = season
        self.enabled = enabled
        self.state = "R"
        self.type = mtype
        self.media_source = "themoviedb"
        self.media_id = score_id
        self.tmdb_id = score_id
        self.year = "2026"
        self.total_episode = None
        self.start_episode = 1


class _FakeOper:
    """固定订阅列表的数据入口替身。"""

    def __init__(self, rows):
        self._rows = rows

    def list(self, state=None):
        """返回预设订阅。"""
        return list(self._rows)


class _SubscribeOper:
    """返回固定订阅的入口桩。"""

    def __init__(self, rows: list[_Subscription] | Exception):
        self.rows = rows
        self.calls = 0

    def list(self):
        """记录调用并返回订阅。"""
        self.calls += 1
        if isinstance(self.rows, Exception):
            raise self.rows
        return list(self.rows)


class _MediaServerOper:
    """按 seasoninfo 返回媒体库条目的入口桩。"""

    def __init__(self, episodes: list[int]):
        self.episodes = episodes
        self.calls: list[dict] = []

    def exists(self, **kwargs):
        """记录查询参数并返回指定季已有集。"""
        self.calls.append(dict(kwargs))
        season = kwargs.get("season", 1)
        return SimpleNamespace(seasoninfo={str(season): list(self.episodes)})


class _Cache:
    """分享快照缓存桩。"""

    def __init__(self, entry: ShareEntry):
        self.entry = entry
        self.calls: list[str] = []

    def get(self, sid: str):
        """返回预设快照。"""
        self.calls.append(sid)
        return self.entry if sid == self.entry.sid else None


class _Chain:
    """下载链桩，记录 download_single 参数。"""

    def __init__(self, result: str | None = "hash-1"):
        self.result = result
        self.calls: list[dict] = []

    def download_single(self, **kwargs):
        """记录调用并返回预设结果。"""
        self.calls.append(dict(kwargs))
        return self.result


def _share(sid: str = "share-1", name: str = "折月亮.S01E22.2160p.mkv",
           fid: str = "f22", size: int = 1024) -> ShareEntry:
    """构造只含一集的分享快照。"""
    return ShareEntry(
        sid=sid,
        provider="quark",
        share_url=f"https://pan.quark.cn/s/{sid}",
        title="折月亮",
        files=[{"name": name, "fid": fid, "size": size}],
        total_size=size,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Show.S01E22.1080p.mkv", [22]),
        ("Show.E22.mkv", [22]),
        ("22.mkv", [22]),
        ("剧集 第22集.mp4", [22]),
        ("Show.21-25.mkv", [21, 22, 23, 24, 25]),
        ("Show.S01E21-E25.mkv", [21, 22, 23, 24, 25]),
        ("Show.1080p.mkv", []),
        ("Show.4K.mkv", []),
        ("Show.2024.mkv", []),
    ],
)
def test_episode_plan_parses_episode_and_range(name, expected):
    """集号解析覆盖显式标记、裸数字与区间。"""
    assert EpisodePlan.episodes_of(name) == expected


def test_episode_plan_outputs_download_items():
    """文件清单输出 episode/fid/file_name/size，并按集号排序。"""
    files = [
        {"name": "Show.S01E02.mkv", "fid": "f2", "size": 20},
        {"name": "Show.S01E01.mkv", "fid": "f1", "size": 10},
        {"name": "花絮.mkv", "fid": "fx", "size": 5},
    ]
    assert EpisodePlan.from_files(files) == [
        {"episode": 1, "fid": "f1", "file_name": "Show.S01E01.mkv", "size": 10},
        {"episode": 2, "fid": "f2", "file_name": "Show.S01E02.mkv", "size": 20},
    ]


def test_matcher_threshold_and_best_of_multiple():
    """标题匹配按阈值过滤，并在多命中时取最高分。"""
    rows = [
        _Subscription("折月亮", score_id="1"),
        _Subscription("折月亮 外传", score_id="2"),
        _Subscription("完全无关的剧", score_id="3"),
    ]
    matcher = SubscriptionMatcher(0.75, _SubscribeOper(rows))
    best, score = matcher.find("折月亮 S01E22 4K")
    assert best is not None and best.media_id == "1"
    assert score >= 0.75
    assert matcher.match("另一部完全不同的剧名") is None


def test_matcher_checks_season_and_skips_disabled():
    """季号不符与停用订阅都不参与匹配。"""
    rows = [
        _Subscription("折月亮", season=1),
        _Subscription("折月亮", season=2, enabled=False),
    ]
    matcher = SubscriptionMatcher(0.75, _SubscribeOper(rows))
    assert matcher.match("折月亮", season=1) is rows[0]
    assert matcher.match("折月亮", season=2) is None
    assert matcher.match("折月亮", season=3) is None


def test_missing_resolver_uses_media_library_not_total_episode(tmp_path):
    """已有 1..21 时候选 22 缺 22，候选 21 不缺。"""
    store = AutoPickStore(tmp_path)
    oper = _MediaServerOper(list(range(1, 22)))
    resolver = MissingEpisodeResolver(store, oper)
    assert resolver.missing_episodes([22], "123", 1, "share-1") == [22]
    assert resolver.missing_episodes([21], "123", 1, "share-1") == []
    assert oper.calls[0]["season"] == 1


def test_missing_resolver_skips_processed_key(tmp_path):
    """去重键包含 tmdb_id/season/episode/share_id。"""
    store = AutoPickStore(tmp_path)
    resolver = MissingEpisodeResolver(store, _MediaServerOper([1, 2]))
    store.mark_processed("123", 1, 3, "share-a")
    assert resolver.missing_episodes([3], "123", 1, "share-a") == []
    assert resolver.missing_episodes([3], "123", 1, "share-b") == [3]


def _picker(tmp_path: Path, *, dry_run: bool, chain: _Chain,
            cache: _Cache, episodes: list[int] | None = None):
    """构造使用替身的 AutoPicker。"""
    return AutoPicker(
        data_dir=tmp_path,
        cache=cache,
        torrent_url_builder=lambda sid, fid: f"http://mp/torrent?sid={sid}&fid={fid}",
        enabled=True,
        dry_run=dry_run,
        min_similarity=0.75,
        downloader="网盘直链",
        subscribe_oper=_SubscribeOper([_Subscription("折月亮")]),
        mediaserver_oper=_MediaServerOper(episodes or list(range(1, 22))),
        download_chain=chain,
    )


def _entry(share: ShareEntry, channel: str = "测试频道") -> dict:
    """构造 TG 索引条目。"""
    return {
        "sid": share.sid,
        "share_url": share.share_url,
        "title": "折月亮 S01E22 4K",
        "channel": channel,
        "msg_id": 9,
        "created_at": time.time(),
    }


def test_dry_run_records_but_does_not_download(tmp_path):
    """dry_run 只写最近决策，不调用 download_single。"""
    share = _share()
    chain = _Chain()
    picker = _picker(tmp_path, dry_run=True, chain=chain, cache=_Cache(share))
    decisions = picker.process([_entry(share)])
    assert chain.calls == []
    assert decisions and decisions[0]["action"] == "dry_run"
    assert picker.recent(1)[0]["dry_run"] is True
    assert picker.store.processed_count() == 0


def test_real_branch_builds_context_and_dedupes(tmp_path):
    """真实分支参数正确，成功下载后同分享同集不再重复提交。"""
    share = _share()
    chain = _Chain()
    picker = _picker(tmp_path, dry_run=False, chain=chain, cache=_Cache(share))
    entries = [_entry(share)]
    decisions = picker.process(entries)
    assert decisions[0]["action"] == "downloaded"
    assert len(chain.calls) == 1
    call = chain.calls[0]
    assert call["torrent_content"] is None
    assert call["episodes"] == {22}
    assert call["downloader"] == "网盘直链"
    context = call["context"]
    assert context.torrent_info.site_name == "测试频道"
    assert "f22" in context.torrent_info.enclosure
    assert context.media_info.media_id == "100"
    assert context.media_info.title == "折月亮"
    assert context.meta_info.episode_list == [22]
    assert picker.store.processed_count() == 1
    picker.process(entries)
    assert len(chain.calls) == 1


def test_cache_miss_fetches_snapshot_once_and_handles_errors(tmp_path):
    """缓存未命中时现场取一次快照；取快照异常只记录不抛出。"""
    share = _share()
    fetched: list[tuple] = []

    def fetch(url, passcode, title):
        fetched.append((url, passcode, title))
        return share

    picker = AutoPicker(
        data_dir=tmp_path,
        cache=None,
        snapshot_fetcher=fetch,
        torrent_url_builder=lambda sid, fid: "url",
        enabled=True,
        dry_run=True,
        subscribe_oper=_SubscribeOper([_Subscription("折月亮")]),
        mediaserver_oper=_MediaServerOper(list(range(1, 22))),
    )
    picker.process([_entry(share)])
    assert len(fetched) == 1

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    picker.snapshot_fetcher = broken
    decisions = picker.process([_entry(share)])
    assert decisions[0]["action"] == "skipped"


def test_process_survives_subscription_failure(tmp_path):
    """读取订阅异常不会打断监控，只返回跳过记录。"""
    share = _share()
    picker = AutoPicker(
        data_dir=tmp_path,
        cache=_Cache(share),
        torrent_url_builder=lambda sid, fid: "url",
        enabled=True,
        dry_run=True,
        subscribe_oper=_SubscribeOper(RuntimeError("db down")),
        mediaserver_oper=_MediaServerOper([]),
    )
    decisions = picker.process([_entry(share)])
    assert decisions[0]["action"] == "skipped"
    assert "订阅" in decisions[0]["reason"]


def test_tg_monitor_invokes_new_entries_callback(tmp_path):
    """poll_once 拿到新增条目后调用回调。"""
    seen: list[list[dict]] = []

    class _Message:
        id = 2
        message = "折月亮\nhttps://pan.quark.cn/s/abc123"
        date = time.time()

    class _Client:
        def iter_messages(self, entity, **kwargs):
            return [_Message()]

    monitor = TgMonitor(
        tmp_path, 1, "hash", "session", ["chan"], index=None,
        on_new_entries=lambda rows: seen.append(rows),
    )
    monitor.index.set_cursor("chan", 1)
    added = asyncio.run(monitor.poll_once(client=_Client()))
    assert added and seen and seen[0][0]["sid"] == "abc123"


def test_store_persists_and_reloads(tmp_path):
    """去重表与最近决策可跨实例恢复。"""
    first = AutoPickStore(tmp_path)
    first.mark_processed("123", 1, 22, "share-1")
    first.add_recent({"action": "downloaded", "title": "折月亮"})
    second = AutoPickStore(tmp_path)
    assert second.is_processed("123", 1, 22, "share-1")
    assert second.recent(1)[0]["title"] == "折月亮"


def test_episode_plan_ignores_non_video_files():
    """电子书/图片/字幕不参与集号解析。"""
    plan = EpisodePlan.parse_files([
        {"name": "《审美心理描述》（azw3+epub+mobi+pdf)", "size": 1, "fid": "a"},
        {"name": "Show.S01E05.1080p.mkv", "size": 10, "fid": "b"},
        {"name": "Show.S01E05.chs.ass", "size": 1, "fid": "c"},
    ])
    assert [item["episode"] for item in plan] == [5]


def test_matcher_substring_beats_similarity():
    """订阅名是标题子串时直接命中（标题常带「名称：」「更至N集」等后缀）。"""
    class _Sub:
        name = "折月亮"
        type = "电视剧"
        season = 1
        enabled = True
        state = "R"
        start_episode = 1

    matcher = SubscriptionMatcher(subscribe_oper=_FakeOper([_Sub()]), min_similarity=0.9)
    hit, score = matcher.find("名称：折月亮 (2026) 4K 更至20集 [都市爱情]", season=1, episode=21)
    assert hit is not None and score == 1.0
