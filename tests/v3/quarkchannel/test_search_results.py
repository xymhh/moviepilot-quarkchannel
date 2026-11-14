"""搜索结果（整包 + 单集）回归测试：标题规范化、连集展开、多画质去重、缓存门控。"""

from __future__ import annotations

from quarkchannel import QuarkChannel
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


def _plugin(entry: ShareEntry, config: dict | None = None) -> QuarkChannel:
    """构造可直接调用搜索的插件实例。"""
    plugin = QuarkChannel()
    plugin._enabled = True
    plugin._config = {"max_results": 5, "expand_in_search": True, **(config or {})}
    plugin._secret = "secret"
    plugin._cache = _Cache([entry])
    plugin._tg_index = None
    plugin._pansou = _Pansou([
        {"url": entry.share_url, "title": entry.title, "source": "pansou"},
    ])
    return plugin


def _entry(url: str, title: str, files: list[dict], total_size: int) -> ShareEntry:
    """构造分享快照。"""
    return ShareEntry(
        sid=build_sid(url),
        provider="quark",
        share_url=url,
        title=title,
        files=files,
        total_size=total_size,
    )


# ---------- _clean_title ----------

def test_clean_title_strips_update_and_ads():
    """水印、广告词与「更新至第 N 集」都会被去掉，剧名与年份保留。"""
    assert QuarkChannel._clean_title("【公众号：追剧君】仙逆 更新至第12集 2024 1080p") == "仙逆 2024 1080p"
    assert QuarkChannel._clean_title("仙逆 S01E05 高清中字") == "仙逆"
    assert QuarkChannel._clean_title("【独家首发】凡人修仙传 第3集") == "凡人修仙传"
    assert QuarkChannel._clean_title("仙逆") == "仙逆"


def test_clean_title_keeps_year_and_resolution():
    """年份与分辨率是识别信息，不应被清洗掉。"""
    assert QuarkChannel._clean_title("庆余年2 (2024) 2160p") == "庆余年2 2024 2160p"


# ---------- _episode_range ----------

def test_episode_range_parses_ranges_and_singles():
    """区间写法展开成集号列表，单集返回单元素列表，解析不出返回空。"""
    assert QuarkChannel._episode_range("Show.S01E01-E02.mkv") == [1, 2]
    assert QuarkChannel._episode_range("Show.S01E03-05.mkv") == [3, 4, 5]
    assert QuarkChannel._episode_range("仙逆 第1-2集.mp4") == [1, 2]
    assert QuarkChannel._episode_range("Show.S01E07.mkv") == [7]
    assert QuarkChannel._episode_range("花絮.mkv") == []
    assert QuarkChannel._episode_of("Show.S01E01-E02.mkv") == 1


def test_episode_range_caps_at_ten():
    """单文件跨集超过 10 集时只取前 10 集，防误解析。"""
    assert QuarkChannel._episode_range("Show.S01E01-E20.mkv") == list(range(1, 11))


# ---------- _pack_title ----------

def test_pack_title_normalizes_messy_share_title():
    """含「更新至第12集」的乱标题规范化为「剧名 S01E01-E12」。"""
    entry = _entry(
        "https://pan.quark.cn/s/pack",
        "【公众号：追剧君】仙逆 更新至第12集",
        [
            {"name": f"仙逆 S01E{i:02d}.mkv", "size": 100, "fid": f"f{i}"}
            for i in range(1, 13)
        ],
        total_size=1200,
    )
    plugin = _plugin(entry)
    title = plugin._pack_title(
        {"title": "【公众号：追剧君】仙逆 更新至第12集"}, entry, None,
    )
    assert title == "仙逆 S01E01-E12"


def test_pack_title_falls_back_to_season_when_no_episodes():
    """解析不出集号但有季号标记时退回「剧名 Sxx」。"""
    entry = _entry(
        "https://pan.quark.cn/s/season-only",
        "仙逆 S02",
        [{"name": "Show.S02.1080p.mkv", "size": 100, "fid": "f1"}],
        total_size=100,
    )
    assert QuarkChannel._pack_title({"title": "仙逆 S02"}, entry, None) == "仙逆 S02"


def test_pack_title_movie_like_share_keeps_clean_name():
    """无集号、无季号标记的分享（如电影）只给干净剧名，不硬补 S01。"""
    entry = _entry(
        "https://pan.quark.cn/s/movie",
        "演示电影",
        [
            {"name": "Movie.2160p.mkv", "size": 1000, "fid": "m1"},
            {"name": "Movie.1080p.mkv", "size": 800, "fid": "m2"},
        ],
        total_size=1800,
    )
    assert QuarkChannel._pack_title({"title": "演示电影"}, entry, None) == "演示电影"


def test_pack_title_movie_mtype_returns_clean_name():
    """mtype 为电影时直接返回干净剧名，不加季集信息。"""
    entry = _entry(
        "https://pan.quark.cn/s/movie2",
        "演示电影 S01E01-E02",
        [
            {"name": "Show.S01E01.mkv", "size": 100, "fid": "f1"},
            {"name": "Show.S01E02.mkv", "size": 100, "fid": "f2"},
        ],
        total_size=200,
    )
    assert QuarkChannel._pack_title({"title": "演示电影"}, entry, "电影") == "演示电影"


def test_pack_title_compresses_non_continuous_episodes():
    """不连续集号压缩成 E01-E03,E05 形式。"""
    entry = _entry(
        "https://pan.quark.cn/s/gaps",
        "缺集剧",
        [
            {"name": f"Show.S01E{i:02d}.mkv", "size": 100, "fid": f"f{i}"}
            for i in (1, 2, 3, 5)
        ],
        total_size=400,
    )
    assert QuarkChannel._pack_title({"title": "缺集剧"}, entry, None) == "缺集剧 S01E01-E03,E05"


# ---------- 搜索结果：连集展开 ----------

def test_search_expands_range_file_into_each_episode():
    """E01-E02 合集文件同时展开出 E01、E02 两个单集结果。"""
    entry = _entry(
        "https://pan.quark.cn/s/range",
        "连集剧",
        [
            {"name": "Show.S01E01-E02.mkv", "size": 300, "fid": "f12"},
            {"name": "Show.S01E03.mkv", "size": 150, "fid": "f3"},
        ],
        total_size=450,
    )
    plugin = _plugin(entry)
    rows = plugin.search_torrents(keyword="连集")
    assert [item.title for item in rows] == [
        "连集剧 S01E01-E03",
        "连集 S01E01", "连集 S01E02", "连集 S01E03",
    ]
    assert [item.size for item in rows] == [450.0, 300.0, 300.0, 150.0]


def test_search_select_files_matches_range_file_for_later_episode():
    """下载筛选时，E01-E02 合集文件能命中第 2 集（不再丢集）。"""
    entry = _entry(
        "https://pan.quark.cn/s/range2",
        "连集剧",
        [{"name": "Show.S01E01-E02.mkv", "size": 300, "fid": "f12"}],
        total_size=300,
    )
    assert [item["fid"] for item in QuarkChannel._select_files(entry, {2})] == ["f12"]
    assert QuarkChannel._select_files(entry, {3}) == []


# ---------- 搜索结果：多画质去重 ----------

def test_search_keeps_each_quality_of_same_episode():
    """同一集的 1080p 与 720p 都保留，不再只留体积最大的一个。"""
    entry = _entry(
        "https://pan.quark.cn/s/quality",
        "多画质",
        [
            {"name": "Show.S01E01.720p.mkv", "size": 100, "fid": "small"},
            {"name": "Show.S01E01.1080p.mkv", "size": 300, "fid": "large"},
        ],
        total_size=400,
    )
    plugin = _plugin(entry)
    rows = plugin.search_torrents(keyword="多画质")
    assert [item.title for item in rows] == [
        "多画质 S01E01",
        "多画质 S01E01 1080p", "多画质 S01E01 720p",
    ]
    assert [item.size for item in rows] == [400.0, 300.0, 100.0]


# ---------- 搜索结果：整包大小只计视频 ----------

def test_pack_size_counts_video_files_only():
    """整包大小不含字幕等非视频文件。"""
    entry = _entry(
        "https://pan.quark.cn/s/mixed",
        "混合包",
        [
            {"name": "Show.S01E01.mkv", "size": 100, "fid": "f1"},
            {"name": "Show.S01E01.srt", "size": 50, "fid": "s1"},
            {"name": "封面.jpg", "size": 25, "fid": "c1"},
        ],
        total_size=175,
    )
    plugin = _plugin(entry)
    rows = plugin.search_torrents(keyword="混合")
    assert rows[0].size == 100.0


# ---------- 搜索结果：缓存门控 ----------

def test_search_cache_skips_unready_candidates(monkeypatch):
    """有候选无快照时不写缓存，下次搜索会重新展开。"""
    entry = _entry(
        "https://pan.quark.cn/s/ready",
        "就绪剧",
        [{"name": "Show.S01E01.mkv", "size": 100, "fid": "f1"}],
        total_size=100,
    )
    plugin = _plugin(entry)
    plugin._cache = _Cache([entry])
    plugin._pansou = _Pansou([
        {"url": entry.share_url, "title": "就绪剧", "source": "pansou"},
        {"url": "https://pan.quark.cn/s/not-ready", "title": "未就绪",
         "source": "pansou"},
    ])
    monkeypatch.setattr(plugin, "_ensure_entry", lambda *args: None)
    rows = plugin.search_torrents(keyword="就绪")
    assert len(rows) == 3  # 就绪的整包+单集，未就绪的只有整包
    assert plugin._search_cache_get("就绪|None") is None


def test_search_cache_writes_when_all_ready():
    """全部候选都有快照时正常写缓存。"""
    entry = _entry(
        "https://pan.quark.cn/s/ready2",
        "就绪剧",
        [{"name": "Show.S01E01.mkv", "size": 100, "fid": "f1"}],
        total_size=100,
    )
    plugin = _plugin(entry)
    rows = plugin.search_torrents(keyword="就绪")
    assert len(rows) == 2
    assert plugin._search_cache_get("就绪|None") is not None
