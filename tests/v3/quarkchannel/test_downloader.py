"""本地分片下载与任务管理单元测试：用假 RequestUtils 提供 Range 字节。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from quarkchannel import downloader as downloader_module
from quarkchannel.downloader import (
    STATE_COMPLETED,
    STATE_ERROR,
    STATE_STOPPED,
    DownloadManager,
    DownloadTask,
    TaskFile,
)


class _Resp:
    """最小 HTTP 响应桩。"""

    def __init__(self, content: bytes, status_code: int = 206):
        self.content = content
        self.status_code = status_code


class _Client:
    """固定直链的夸克客户端桩。"""

    def __init__(self, url: str = "https://dl.example/file"):
        self._url = url
        self.calls = []

    def download_url(self, share_url, passcode, file_item):
        """记录取链参数并返回固定直链。"""
        self.calls.append((share_url, passcode, file_item.fid, file_item.share_token))
        return self._url, {"User-Agent": "test", "Referer": "https://pan.quark.cn/"}


def _range_utils(source: bytes, calls: list[tuple[int, int]], short_at: int | None = None):
    """构造按 Range 返回字节的 RequestUtils 假类。"""

    class _Request:
        def __init__(self, *args, **kwargs):
            """兼容无参构造。"""

        def get_res(self, url, headers=None, **kwargs):
            """解析 Range 并返回对应片段。"""
            value = (headers or {}).get("Range", "bytes=0-")
            start_text, end_text = value.split("=", 1)[1].split("-", 1)
            start = int(start_text)
            end = int(end_text)
            calls.append((start, end))
            if short_at is not None and start == short_at:
                return _Resp(source[start:start + 1])
            return _Resp(source[start:end + 1])

    return _Request


def _task(tmp_path: Path, size: int = 10, done: int = 0, folder: str = "Show",
          name: str = "a.bin") -> DownloadTask:
    """构造只有单个文件的任务。"""
    return DownloadTask(
        hash="hash-1",
        sid="sid-1",
        title="演示",
        folder=folder,
        save_dir=str(tmp_path / "downloads"),
        files=[TaskFile(name=name, size=size, done=done, fid="fid-1", share_token="tok-1")],
    )


def _wait(manager: DownloadManager, task_hash: str, states: set[str],
          timeout: float = 5.0) -> DownloadTask:
    """等待任务进入目标状态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = manager.get(task_hash)
        if task and task.state in states:
            return task
        time.sleep(0.01)
    task = manager.get(task_hash)
    raise AssertionError(f"任务未进入 {states}，当前：{getattr(task, 'state', None)}")


def test_chunk_write_completed(tmp_path, monkeypatch):
    """分片按 Range 写入正确位置，完成后文件内容与源一致。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    manager = DownloadManager(tmp_path, lambda: _Client())
    task = _task(tmp_path, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "1234")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        destination = Path(task.content_path) / "a.bin"
        assert destination.read_bytes() == source
        assert finished.done == len(source)
        assert finished.progress == 100.0
        assert sorted(calls) == [(0, 3), (4, 7), (8, 9)]
    finally:
        manager.shutdown()


def test_resume_partial_file_requests_remaining_ranges(tmp_path, monkeypatch):
    """断点续传只请求未完成区间，并保留已有前缀。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    task = _task(tmp_path, size=len(source), done=4)
    destination = Path(task.content_path) / "a.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source[:4])
    manager = DownloadManager(tmp_path, lambda: _Client())
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert destination.read_bytes() == source
        assert sorted(calls) == [(4, 7), (8, 9)]
    finally:
        manager.shutdown()


def test_skip_already_completed_file(tmp_path, monkeypatch):
    """目标文件已完整时直接标记完成，不再发请求。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    task = _task(tmp_path, size=len(source))
    destination = Path(task.content_path) / "a.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source)
    manager = DownloadManager(tmp_path, lambda: _Client())
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert finished.files[0].done == len(source)
        assert calls == []
        assert destination.read_bytes() == source
    finally:
        manager.shutdown()


def test_stop_event_aborts_without_requests(tmp_path, monkeypatch):
    """停止事件已置位时不发请求，并把任务标记为 stopped。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(b"abcdefghij", calls))
    task = _task(tmp_path)
    event = threading.Event()
    event.set()
    manager = DownloadManager(tmp_path, lambda: _Client())
    try:
        manager._download_one(task, event, _Client(), "https://pan.quark.cn/s/abc", "", task.files[0])
        assert calls == []
        assert task.state == STATE_STOPPED
    finally:
        manager.shutdown()


def test_size_mismatch_raises(tmp_path, monkeypatch):
    """分片返回字节数不足时报文件大小不一致。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(b"abcdefghij", calls, short_at=4))
    task = _task(tmp_path)
    manager = DownloadManager(tmp_path, lambda: _Client())
    try:
        with pytest.raises(RuntimeError, match="文件大小不一致"):
            manager._download_one(
                task, threading.Event(), _Client(), "https://pan.quark.cn/s/abc", "", task.files[0],
            )
    finally:
        manager.shutdown()


def test_remove_with_delete_only_drops_task_directory(tmp_path):
    """remove(delete_file=True) 只删除任务目录，不动同级文件。"""
    manager = DownloadManager(tmp_path, lambda: _Client())
    task = _task(tmp_path, folder="Show")
    root = Path(task.content_path)
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"payload")
    (root / "sub" / "b.bin").write_bytes(b"child")
    keep = Path(task.save_dir) / "keep.txt"
    keep.write_bytes(b"keep")
    manager._tasks[task.hash] = task
    try:
        assert manager.remove([task.hash], delete_file=True) is True
    finally:
        manager.shutdown()
    assert not root.exists()
    assert keep.read_bytes() == b"keep"
    assert manager.get(task.hash) is None


def test_remove_without_delete_keeps_files(tmp_path):
    """remove(delete_file=False) 仅删除任务记录，保留下载内容。"""
    manager = DownloadManager(tmp_path, lambda: _Client())
    task = _task(tmp_path, folder="Show")
    root = Path(task.content_path)
    root.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"payload")
    manager._tasks[task.hash] = task
    try:
        assert manager.remove([task.hash], delete_file=False) is True
    finally:
        manager.shutdown()
    assert root.exists()
    assert (root / "a.bin").read_bytes() == b"payload"
