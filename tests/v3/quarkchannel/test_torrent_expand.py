"""合成种子单文件/整包渲染与 /torrent 懒加载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarkchannel import QuarkChannel
from quarkchannel.torrents import ShareEntry, TorrentCache, build_sid, render_torrent, sign


class _Cache:
    """只读缓存桩。"""

    def __init__(self, entry: ShareEntry | None = None):
        self.entry = entry

    def get(self, sid: str):
        """按 sid 返回预设快照。"""
        return self.entry if self.entry and sid == self.entry.sid else None


def _entry() -> ShareEntry:
    """构造两集分享。"""
    return ShareEntry(
        sid="sid-expand",
        provider="quark",
        share_url="https://pan.quark.cn/s/expand",
        title="演示剧集",
        files=[
            {"name": "Show.S01E01.mkv", "size": 10, "fid": "f1"},
            {"name": "Show.S01E02.mkv", "size": 20, "fid": "f2"},
        ],
        total_size=30,
    )


def test_render_torrent_whole_keeps_all_files():
    """不传 fid 时渲染整包。"""
    payload = render_torrent(_entry())
    assert b"Show.S01E01.mkv" in payload
    assert b"Show.S01E02.mkv" in payload


def test_render_torrent_single_file_uses_fid():
    """传 fid 时只包含目标文件名与大小。"""
    payload = render_torrent(_entry(), fid="f2")
    assert b"Show.S01E02.mkv" in payload
    assert b"Show.S01E01.mkv" not in payload
    assert b"i20e" in payload


def test_render_torrent_missing_fid_raises():
    """找不到指定 fid 时抛 ValueError，供端点返回 404。"""
    with pytest.raises(ValueError):
        render_torrent(_entry(), fid="missing")


def test_api_torrent_serves_cached_whole():
    """有快照且不带 fid 时返回整包种子。"""
    entry = _entry()
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    response = plugin.api_torrent(sid=entry.sid, sig=sign(entry.sid, "secret"))
    assert response.status_code == 200
    assert b"Show.S01E01.mkv" in response.content
    assert b"Show.S01E02.mkv" in response.content


def test_api_torrent_serves_cached_single_file():
    """有快照且带 fid 时只返回单文件种子。"""
    entry = _entry()
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    response = plugin.api_torrent(
        sid=entry.sid, sig=sign(entry.sid, "secret"), fid="f2",
    )
    assert response.status_code == 200
    assert b"Show.S01E02.mkv" in response.content
    assert b"Show.S01E01.mkv" not in response.content


def test_api_torrent_rejects_bad_signature():
    """签名错误直接 403。"""
    entry = _entry()
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = _Cache(entry)
    response = plugin.api_torrent(sid=entry.sid, sig="bad")
    assert response.status_code == 403


def test_api_torrent_missing_snapshot_returns_404(tmp_path: Path):
    """无快照且无候选登记时返回 404。"""
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = TorrentCache(tmp_path)
    sid = build_sid("https://pan.quark.cn/s/missing")
    response = plugin.api_torrent(sid=sid, sig=sign(sid, "secret"))
    assert response.status_code == 404


def test_api_torrent_lazy_snapshot_serves_whole(tmp_path: Path, monkeypatch):
    """无快照时按候选登记现场取快照并返回整包。"""
    entry = _entry()
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = TorrentCache(tmp_path)
    sid = build_sid(entry.share_url)
    plugin._remember_pending({
        "sid": sid, "share_url": entry.share_url, "passcode": "", "title": entry.title,
    })
    monkeypatch.setattr(plugin, "_ensure_entry", lambda *args: entry)
    response = plugin.api_torrent(sid=sid, sig=sign(sid, "secret"))
    assert response.status_code == 200
    assert b"Show.S01E01.mkv" in response.content


def test_api_torrent_lazy_snapshot_missing_fid_returns_404(tmp_path: Path, monkeypatch):
    """现场取到快照但找不到 fid 时返回 404。"""
    entry = _entry()
    plugin = QuarkChannel()
    plugin._secret = "secret"
    plugin._cache = TorrentCache(tmp_path)
    sid = build_sid(entry.share_url)
    plugin._remember_pending({
        "sid": sid, "share_url": entry.share_url, "passcode": "", "title": entry.title,
    })
    monkeypatch.setattr(plugin, "_ensure_entry", lambda *args: entry)
    response = plugin.api_torrent(sid=sid, sig=sign(sid, "secret"), fid="missing")
    assert response.status_code == 404
