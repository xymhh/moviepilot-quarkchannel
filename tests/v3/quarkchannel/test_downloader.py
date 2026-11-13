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
    """记录转存、取链与清理调用的夸克客户端桩。"""

    def __init__(self, url: str = "https://dl.example/file", keep_saved: bool = False):
        self._url = url
        self.keep_saved = keep_saved
        self.stoken_calls = []
        self.save_calls = []
        self.link_fids = []
        self.delete_calls = []
        self.download_calls = []

    def stoken(self, pwd_id, passcode):
        """返回固定 stoken。"""
        self.stoken_calls.append((pwd_id, passcode))
        return "tk"

    def save_files(self, pwd_id, stoken, files):
        """记录整批转存并返回带前缀的转存后 fid。"""
        self.save_calls.append((pwd_id, stoken, list(files)))
        return [f"saved-{item.fid}" for item in files]

    def download_url_by_fid(self, fid, name="", retries=3):
        """记录转存 fid 并返回固定直链。"""
        self.link_fids.append(fid)
        return self._url, {"User-Agent": "test", "Referer": "https://pan.quark.cn/"}

    def download_url(self, share_url, passcode, file_item):
        """兼容单文件旧入口，返回固定直链。"""
        self.download_calls.append((share_url, passcode, file_item.fid))
        return self._url, {"User-Agent": "test", "Referer": "https://pan.quark.cn/"}

    def delete_files(self, fids):
        """记录待删除的网盘副本。"""
        self.delete_calls.append(list(fids))
        return True


class _FakeAria2Rpc:
    """模拟 aria2 RPC 的添加、状态查询与移除。"""

    def __init__(self, size: int = 10, mode: str = "success",
                 error_message: str = "aria2 error", add_result: bool = True):
        self.size = size
        self.mode = mode
        self.error_message = error_message
        self.add_result = add_result
        self.add_calls = []
        self.counts = {}
        self.removed = []

    def add_uri(self, url, options):
        """记录 URL 与选项并返回递增 gid。"""
        self.add_calls.append((url, dict(options)))
        if not self.add_result:
            return None
        return f"gid-{len(self.add_calls)}"

    def tell_status(self, gid):
        """按调用次数返回等待、下载中、完成，或直接返回错误。"""
        count = self.counts.get(gid, 0)
        self.counts[gid] = count + 1
        if self.mode == "error":
            return {"status": "error", "errorMessage": self.error_message}
        if count == 0:
            return {"status": "waiting", "completedLength": "0", "downloadSpeed": "0"}
        if count == 1:
            return {
                "status": "active",
                "completedLength": str(self.size // 2),
                "downloadSpeed": "1024",
            }
        return {
            "status": "complete",
            "completedLength": str(self.size),
            "downloadSpeed": "0",
        }

    def remove(self, gid):
        """记录被移除的 gid。"""
        self.removed.append(gid)
        return gid


class _FakeLauncher:
    """模拟 aria2 启动器。"""

    def __init__(self, rpc: _FakeAria2Rpc, available: bool = True):
        self.rpc = rpc
        self.available = available
        self.stop_calls = 0

    def ensure(self):
        """返回预设可用状态。"""
        return self.available

    def stop(self):
        """记录停止次数。"""
        self.stop_calls += 1


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


def _failing_utils():
    """构造所有分片都失败的 RequestUtils 假类。"""

    class _Request:
        def __init__(self, *args, **kwargs):
            """兼容无参构造。"""

        def get_res(self, url, headers=None, **kwargs):
            """固定返回 500。"""
            return _Resp(b"", 500)

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


def _multi_task(tmp_path: Path, count: int = 3, size: int = 10) -> DownloadTask:
    """构造包含多个文件的任务。"""
    return DownloadTask(
        hash="hash-multi",
        sid="sid-1",
        title="演示多文件",
        folder="Show",
        save_dir=str(tmp_path / "downloads"),
        files=[
            TaskFile(name=f"f{i}.bin", size=size, fid=f"fid-{i}", share_token=f"tok-{i}")
            for i in range(count)
        ],
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
    client = _Client()
    manager = DownloadManager(tmp_path, lambda: client)
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
        assert len(client.save_calls) == 1
        assert client.link_fids == ["saved-fid-1"]
    finally:
        manager.shutdown()


def test_resume_partial_file_requests_remaining_ranges(tmp_path, monkeypatch):
    """断点续传只请求未完成区间，并保留已有前缀。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client()
    task = _task(tmp_path, size=len(source), done=4)
    destination = Path(task.content_path) / "a.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source[:4])
    manager = DownloadManager(tmp_path, lambda: client)
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert destination.read_bytes() == source
        assert sorted(calls) == [(4, 7), (8, 9)]
        assert len(client.save_calls) == 1
    finally:
        manager.shutdown()


def test_skip_already_completed_file(tmp_path, monkeypatch):
    """目标文件已完整时直接标记完成，不转存也不发请求。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client()
    task = _task(tmp_path, size=len(source))
    destination = Path(task.content_path) / "a.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source)
    manager = DownloadManager(tmp_path, lambda: client)
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert finished.files[0].done == len(source)
        assert calls == []
        assert client.save_calls == []
        assert client.link_fids == []
        assert destination.read_bytes() == source
    finally:
        manager.shutdown()


def test_transfer_only_once_and_delete_after_success(tmp_path, monkeypatch):
    """同一任务多个文件只转存一次，全部成功且 keep_saved=False 时删除副本。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client(keep_saved=False)
    manager = DownloadManager(tmp_path, lambda: client)
    task = _multi_task(tmp_path, count=3, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "1234")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert len(client.save_calls) == 1
        assert client.save_calls[0][0] == "abc"
        assert client.save_calls[0][1] == "tk"
        assert len(client.save_calls[0][2]) == 3
        assert client.link_fids == ["saved-fid-0", "saved-fid-1", "saved-fid-2"]
        assert client.delete_calls == [["saved-fid-0", "saved-fid-1", "saved-fid-2"]]
        for index in range(3):
            assert (Path(task.content_path) / f"f{index}.bin").read_bytes() == source
    finally:
        manager.shutdown()


def test_keep_saved_skips_delete(tmp_path, monkeypatch):
    """keep_saved=True 时下载成功也不删除网盘副本。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client(keep_saved=True)
    manager = DownloadManager(tmp_path, lambda: client)
    task = _multi_task(tmp_path, count=2, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert len(client.save_calls) == 1
        assert client.delete_calls == []
    finally:
        manager.shutdown()


def test_failure_keeps_saved_copy(tmp_path, monkeypatch):
    """下载失败时保留已转存副本，方便下一次重试。"""
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _failing_utils())
    client = _Client(keep_saved=False)
    manager = DownloadManager(tmp_path, lambda: client)
    task = _multi_task(tmp_path, count=2)
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_ERROR
        assert len(client.save_calls) == 1
        assert client.delete_calls == []
    finally:
        manager.shutdown()


def test_stop_event_aborts_without_requests(tmp_path, monkeypatch):
    """停止事件已置位时不发分片请求，并把任务标记为 stopped。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(b"abcdefghij", calls))
    task = _task(tmp_path)
    event = threading.Event()
    event.set()
    manager = DownloadManager(tmp_path, lambda: _Client())
    try:
        manager._download_one(
            task, event, _Client(), "https://pan.quark.cn/s/abc", "", task.files[0], "saved-fid",
        )
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
                task, threading.Event(), _Client(), "https://pan.quark.cn/s/abc", "",
                task.files[0], "saved-fid",
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


def test_aria2_success_updates_progress_and_cleans_saved(tmp_path, monkeypatch):
    """aria2 成功链路使用 64 线程参数、轮询三种状态并清理转存副本。"""
    monkeypatch.setattr(downloader_module, "ARIA2_POLL_INTERVAL", 0.0)
    source = b"abcdefghij"
    client = _Client(keep_saved=False)
    rpc = _FakeAria2Rpc(size=len(source))
    launcher = _FakeLauncher(rpc)
    manager = DownloadManager(
        tmp_path, lambda: client, aria2_launcher=launcher, aria2_connections=64, aria2_split=64,
    )
    task = _multi_task(tmp_path, count=2, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "1234")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert finished.done == 2 * len(source)
        assert finished.speed == 0.0
        assert [call[0] for call in rpc.add_calls] == [
            "https://dl.example/file", "https://dl.example/file",
        ]
        for _url, options in rpc.add_calls:
            assert options["dir"] == task.content_path
            assert options["out"] in {"f0.bin", "f1.bin"}
            assert options["split"] == "64"
            assert options["max-connection-per-server"] == "64"
            assert options["min-split-size"] == "1M"
            assert options["continue"] == "true"
            assert options["auto-file-renaming"] == "false"
            assert any(line.startswith("User-Agent:") for line in options["header"])
            assert any(line.startswith("Referer:") for line in options["header"])
        assert sorted(rpc.counts.values()) == [3, 3]
        assert client.save_calls and len(client.save_calls[0][2]) == 2
        assert client.delete_calls == [["saved-fid-0", "saved-fid-1"]]
    finally:
        manager.shutdown()
    assert launcher.stop_calls == 1


def test_aria2_unavailable_falls_back_to_chunks(tmp_path, monkeypatch):
    """aria2 RPC 不可用时自动回落到内置分片下载。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client()
    rpc = _FakeAria2Rpc()
    launcher = _FakeLauncher(rpc, available=False)
    manager = DownloadManager(tmp_path, lambda: client, aria2_launcher=launcher)
    task = _task(tmp_path, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert (Path(task.content_path) / "a.bin").read_bytes() == source
        assert rpc.add_calls == []
        assert sorted(calls) == [(0, 3), (4, 7), (8, 9)]
    finally:
        manager.shutdown()


def test_aria2_add_failure_falls_back_to_chunks(tmp_path, monkeypatch):
    """aria2 addUri 失败时回落到内置分片下载。"""
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client()
    rpc = _FakeAria2Rpc(add_result=False)
    launcher = _FakeLauncher(rpc)
    manager = DownloadManager(tmp_path, lambda: client, aria2_launcher=launcher)
    task = _task(tmp_path, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert (Path(task.content_path) / "a.bin").read_bytes() == source
        assert len(rpc.add_calls) == 1
        assert sorted(calls) == [(0, 3), (4, 7), (8, 9)]
    finally:
        manager.shutdown()


def test_aria2_error_marks_task_error(tmp_path, monkeypatch):
    """aria2 返回 error 时任务失败，错误信息截断且移除 gid。"""
    monkeypatch.setattr(downloader_module, "ARIA2_POLL_INTERVAL", 0.0)
    client = _Client()
    rpc = _FakeAria2Rpc(mode="error", error_message="x" * 300)
    launcher = _FakeLauncher(rpc)
    manager = DownloadManager(tmp_path, lambda: client, aria2_launcher=launcher)
    task = _task(tmp_path, size=10)
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_ERROR
        assert "aria2 下载失败" in finished.error
        assert len(finished.error) <= 200
        assert rpc.removed == ["gid-1"]
    finally:
        manager.shutdown()


def test_aria2_http_rejection_falls_back(tmp_path, monkeypatch):
    """aria2 被夸克以 403/412 拒绝时回落到自研分片，而不是判任务失败。"""
    monkeypatch.setattr(downloader_module, "ARIA2_POLL_INTERVAL", 0.0)
    source = b"abcdefghij"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(downloader_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader_module, "RequestUtils", _range_utils(source, calls))
    client = _Client()
    rpc = _FakeAria2Rpc(mode="error", error_message="The response status is not successful. status=403")
    launcher = _FakeLauncher(rpc)
    manager = DownloadManager(tmp_path, lambda: client, aria2_launcher=launcher)
    task = _task(tmp_path, size=len(source))
    try:
        manager.submit(task, "https://pan.quark.cn/s/abc", "")
        finished = _wait(manager, task.hash, {STATE_COMPLETED, STATE_ERROR})
        assert finished.state == STATE_COMPLETED
        assert (Path(task.content_path) / "a.bin").read_bytes() == source
        assert sorted(calls) == [(0, 3), (4, 7), (8, 9)]
    finally:
        manager.shutdown()
