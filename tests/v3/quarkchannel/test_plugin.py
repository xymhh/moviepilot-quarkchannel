"""插件判断题单元测试：选集、集号解析、页面兜底与下载幂等。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import DOWNLOAD_TASK_ALREADY_EXISTS
from quarkchannel import QuarkChannel
from quarkchannel.downloader import STATE_COMPLETED, STATE_DOWNLOADING, STATE_ERROR
from quarkchannel.torrents import ShareEntry, render_torrent


class _Cache:
    """只读分享缓存桩。"""

    def __init__(self, entry: ShareEntry):
        self._entry = entry

    def get(self, sid: str):
        """无论 sid 都返回预设快照。"""
        return self._entry if sid == self._entry.sid else None


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
