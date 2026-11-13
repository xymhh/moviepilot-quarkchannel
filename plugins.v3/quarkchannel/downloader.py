"""本地分片下载与任务状态管理。

MoviePilot 通过 `list_torrents` 读取这里的任务状态，
因此任务字段要能直接映射成 `DownloaderTorrent`。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

from .aria2 import Aria2Launcher
from .shares.quark import QuarkShareClient, ShareFile, extract_pwd_id

CHUNK_SIZE = 16 * 1024 * 1024
ARIA2_POLL_INTERVAL = 2.0
STATE_DOWNLOADING = "downloading"
STATE_COMPLETED = "completed"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"


@dataclass
class TaskFile:
    """任务内的单个文件进度。"""

    name: str
    size: int
    done: int = 0
    fid: str = ""
    share_token: str = ""

    @property
    def rel_path(self) -> str:
        """相对路径，去掉可能存在的目录前缀穿越。"""
        return Path(self.name).name


@dataclass
class DownloadTask:
    """一条下载任务。"""

    hash: str
    sid: str
    title: str
    folder: str
    save_dir: str
    total: int = 0
    done: int = 0
    state: str = STATE_DOWNLOADING
    error: str = ""
    files: list[TaskFile] = field(default_factory=list)
    share_url: str = ""
    passcode: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    speed: float = 0.0

    @property
    def progress(self) -> float:
        """完成百分比。"""
        if not self.total:
            return 100.0 if self.state == STATE_COMPLETED else 0.0
        return min(100.0, round(self.done * 100.0 / self.total, 1))

    @property
    def content_path(self) -> str:
        """内容路径（目录形式）。"""
        return str(Path(self.save_dir) / self.folder)


class DownloadManager:
    """任务与下载线程的管理者。"""

    def __init__(self, data_dir: Path, client_factory: Callable[[], QuarkShareClient],
                 max_tasks: int = 3, aria2_enabled: bool = True,
                 aria2_connections: int = 64, aria2_split: int = 64,
                 aria2_launcher: Optional[Aria2Launcher] = None) -> None:
        """初始化管理器并恢复历史任务。

        :param data_dir: 插件数据目录
        :param client_factory: 取链客户端工厂（每个 worker 独立实例）
        :param max_tasks: 同时下载的任务数
        :param aria2_enabled: 是否优先使用 aria2 RPC 下载
        :param aria2_connections: aria2 单服务器连接数
        :param aria2_split: aria2 单文件分片数
        :param aria2_launcher: aria2 启动器；为空时强制使用自研分片
        """
        self._data_dir = data_dir
        self._file = data_dir / "tasks.json"
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._tasks: dict[str, DownloadTask] = {}
        self._stops: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_tasks, thread_name_prefix="quarkchannel")
        self._share_urls: dict[str, str] = {}
        self._passcodes: dict[str, str] = {}
        self._aria2_enabled = bool(aria2_enabled)
        self._aria2_connections = max(1, int(aria2_connections))
        self._aria2_split = max(1, int(aria2_split))
        self._aria2_launcher = aria2_launcher
        self._aria2_gids: dict[str, list[str]] = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        """恢复磁盘上的任务，未完成的标记为 stopped 等待手动恢复。"""
        if not self._file.exists():
            return
        try:
            rows = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                files = [TaskFile(**item) for item in row.pop("files", [])]
                task = DownloadTask(**row)
            except TypeError:
                continue
            task.files = files
            if task.state == STATE_DOWNLOADING:
                task.state = STATE_STOPPED
            self._tasks[task.hash] = task

    def _flush(self) -> None:
        """写回任务快照。"""
        try:
            rows = [asdict(task) for task in self._tasks.values()]
            temp = self._file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError:
            return

    # ---------- 查询 ----------

    def tasks(self) -> list[DownloadTask]:
        """返回全部任务副本。"""
        with self._lock:
            return list(self._tasks.values())

    def get(self, task_hash: str) -> Optional[DownloadTask]:
        """按 hash 取任务。"""
        with self._lock:
            return self._tasks.get(task_hash)

    # ---------- 控制 ----------

    def submit(self, task: DownloadTask, share_url: str, passcode: str) -> DownloadTask:
        """登记任务并启动下载线程。

        :param task: 已填充文件清单的任务
        :param share_url: 分享地址，取链时需要
        :param passcode: 提取码
        :return: 任务对象
        """
        with self._lock:
            existing = self._tasks.get(task.hash)
            if existing and existing.state in {STATE_DOWNLOADING, STATE_COMPLETED}:
                return existing
            task.created_at = task.created_at or time.time()
            task.updated_at = time.time()
            task.state = STATE_DOWNLOADING
            # 分享地址与提取码随任务落盘，插件重载后仍能续传
            task.share_url = share_url
            task.passcode = passcode
            task.total = sum(item.size for item in task.files)
            task.done = sum(item.done for item in task.files)
            self._tasks[task.hash] = task
            self._share_urls[task.hash] = share_url
            self._passcodes[task.hash] = passcode
            self._stops[task.hash] = threading.Event()
            self._flush()
        self._pool.submit(self._run, task.hash)
        return task

    def stop(self, task_hash: str) -> bool:
        """停止任务（保留已下载内容），并尽量移除 aria2 任务。"""
        gids: list[str] = []
        with self._lock:
            task = self._tasks.get(task_hash)
            event = self._stops.get(task_hash)
            if not task or not event:
                return False
            event.set()
            task.state = STATE_STOPPED
            task.updated_at = time.time()
            gids = self._aria2_gids.pop(task_hash, [])
            self._flush()
        self._remove_aria2_gids(gids)
        return True

    def resume(self, task_hash: str) -> bool:
        """继续已停止的任务。"""
        with self._lock:
            task = self._tasks.get(task_hash)
            if not task:
                return False
            self._stops[task_hash] = threading.Event()
            task.state = STATE_DOWNLOADING
            task.error = ""
            task.updated_at = time.time()
            self._flush()
        self._pool.submit(self._run, task_hash)
        return True

    def remove(self, hashes: list[str], delete_file: bool = False) -> bool:
        """删除任务记录，可选删除已下载文件，并移除对应 aria2 任务。"""
        changed = False
        gids: list[str] = []
        with self._lock:
            for task_hash in hashes:
                task = self._tasks.pop(task_hash, None)
                event = self._stops.pop(task_hash, None)
                gids.extend(self._aria2_gids.pop(task_hash, []))
                if event:
                    event.set()
                if not task:
                    continue
                changed = True
                if delete_file:
                    self._delete_content(task)
            if changed:
                self._flush()
        self._remove_aria2_gids(gids)
        return changed

    def shutdown(self) -> None:
        """停止全部任务、移除 aria2 任务并关闭线程池。"""
        gids: list[str] = []
        with self._lock:
            for event in self._stops.values():
                event.set()
            for task in self._tasks.values():
                if task.state == STATE_DOWNLOADING:
                    task.state = STATE_STOPPED
            for task_gids in self._aria2_gids.values():
                gids.extend(task_gids)
            self._aria2_gids.clear()
            self._flush()
        self._remove_aria2_gids(gids)
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._aria2_launcher:
            self._aria2_launcher.stop()

    # ---------- 下载执行 ----------

    def _delete_content(self, task: DownloadTask) -> None:
        """删除任务目录，仅限任务自身目录内。"""
        try:
            root = Path(task.content_path)
            if not root.exists():
                return
            for item in sorted(root.rglob("*"), reverse=True):
                if item.is_file():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    item.rmdir()
            root.rmdir()
        except OSError as error:
            logger.warning(f"删除任务内容失败：{error}")

    def _run(self, task_hash: str) -> None:
        """在 worker 线程中顺序下载各文件，整任务只转存一次。"""
        task = self.get(task_hash)
        event = self._stops.get(task_hash)
        share_url = (task.share_url if task else "") or self._share_urls.get(task_hash, "")
        passcode = (task.passcode if task else "") or self._passcodes.get(task_hash, "")
        if not task or not event:
            return
        client = self._client_factory()
        root = Path(task.content_path)
        saved_fids: list[str] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
            pending: list[TaskFile] = []
            for item in task.files:
                if item.done >= item.size > 0:
                    continue
                destination = root / item.rel_path
                if destination.exists() and destination.stat().st_size == item.size:
                    item.done = item.size
                    continue
                pending.append(item)
            saved_map: dict[int, str] = {}
            if pending and not event.is_set():
                if not share_url:
                    raise ValueError("缺少分享地址，无法取链")
                pwd_id = extract_pwd_id(share_url)
                if not pwd_id:
                    raise ValueError("无法从分享地址解析分享 id")
                stoken = client.stoken(pwd_id, passcode)
                if not stoken:
                    raise ValueError("分享不可用，无法转存取链")
                # 分享文件的 share_fid_token 有时效，转存前必须重新拉一次 detail，
                # 否则会命中 41020「转存文件token校验异常」。
                fresh_tokens: dict[str, str] = {}
                try:
                    # 必须复用同一个 stoken：detail 返回的 share_fid_token 与 stoken 绑定，
                    # 换一次 stoken 后再转存会报 41020 token 校验异常。
                    snapshot = client.snapshot(share_url, passcode, token=stoken)
                    fresh_tokens = {entry.fid: entry.share_token for entry in snapshot.files}
                except Exception as error:  # noqa: BLE001 - 拉取失败时退回缓存 token
                    logger.warning(f"刷新分享文件 token 失败，改用缓存值：{error}")
                transfer_files = [
                    ShareFile(
                        fid=item.fid,
                        name=item.name,
                        size=item.size,
                        share_token=fresh_tokens.get(item.fid) or item.share_token,
                    )
                    for item in pending
                ]
                saved = client.save_files(pwd_id, stoken, transfer_files)
                if len(saved) != len(pending):
                    raise ValueError(
                        f"转存文件数量不一致：{len(saved)}/{len(pending)}"
                    )
                saved_fids = list(saved)
                saved_map = {id(item): fid for item, fid in zip(pending, saved)}
            aria2_mode = "none"
            if pending and not event.is_set():
                aria2_mode = self._run_aria2(task, event, client, pending, saved_map)
                if aria2_mode == "stopped":
                    return
            if aria2_mode != "completed":
                for item in task.files:
                    if event.is_set():
                        return
                    if item.done >= item.size > 0:
                        continue
                    self._download_one(
                        task, event, client, share_url, passcode, item, saved_map.get(id(item)),
                    )
            with self._lock:
                task.done = sum(item.done for item in task.files)
                task.state = STATE_COMPLETED
                task.speed = 0.0
                task.updated_at = time.time()
                self._flush()
            if saved_fids and not bool(getattr(client, "keep_saved", False)):
                try:
                    client.delete_files(saved_fids)
                except Exception as error:  # noqa: BLE001 - 清理失败不改变下载结果
                    logger.warning(f"删除网盘副本失败：{error}")
            logger.info(f"网盘直链下载完成：{task.title} → {task.content_path}")
        except Exception as error:  # noqa: BLE001 - 单任务失败不影响其它任务
            logger.error(f"网盘直链下载失败：{task.title}，{error}")
            with self._lock:
                task.state = STATE_ERROR
                task.error = str(error)[:500]
                task.speed = 0.0
                task.updated_at = time.time()
                self._flush()

    def _aria2_options(self, task: DownloadTask, item: TaskFile,
                       headers: dict) -> dict:
        """构造 aria2.addUri 需要的下载选项。"""
        headers = headers or {}
        # 与生产可用实现一致：全部请求头放进 header 列表整体下发。
        header_list = [
            f"{key}: {value}" for key, value in headers.items() if value
        ]
        options = {
            "dir": task.content_path,
            "out": item.rel_path,
            "header": header_list,
            "split": str(self._aria2_split),
            "max-connection-per-server": str(self._aria2_connections),
            "min-split-size": "1M",
            "continue": "true",
            "auto-file-renaming": "false",
        }
        return options

    def _remove_aria2_gids(self, gids: list[str]) -> None:
        """尽力从 aria2 移除 gid，不向调用方抛异常。"""
        launcher = self._aria2_launcher
        rpc = getattr(launcher, "rpc", None) if launcher else None
        if not gids or rpc is None:
            return
        for gid in gids:
            try:
                rpc.remove(gid)
            except Exception:  # noqa: BLE001 - 清理失败不影响任务状态
                pass

    def _discard_aria2_partials(self, task: DownloadTask,
                                records: list[tuple[TaskFile, str]]) -> None:
        """删除 aria2 已产生的不完整文件，避免回落后误判为已完成。"""
        for item, _gid in records:
            with self._lock:
                item.done = 0
            try:
                (Path(task.content_path) / item.rel_path).unlink(missing_ok=True)
            except OSError:
                pass

    def _run_aria2(self, task: DownloadTask, event: threading.Event,
                   client: QuarkShareClient, pending: list[TaskFile],
                   saved_map: dict[int, str]) -> str:
        """用 aria2 下载待处理文件；返回 completed/fallback/stopped。"""
        launcher = self._aria2_launcher
        if not self._aria2_enabled or launcher is None:
            return "fallback"
        try:
            if not launcher.ensure():
                logger.warning("aria2 不可用（RPC 探测失败），回落到自研分片下载")
                return "fallback"
        except Exception as error:  # noqa: BLE001 - aria2 异常必须回落
            logger.warning(f"aria2 启动探测异常，回落到自研分片下载：{error}")
            return "fallback"
        rpc = getattr(launcher, "rpc", None)
        if rpc is None:
            logger.warning("aria2 RPC 客户端缺失，回落到自研分片下载")
            return "fallback"
        records: list[tuple[TaskFile, str]] = []
        try:
            for item in pending:
                if event.is_set():
                    self._remove_aria2_gids([gid for _item, gid in records])
                    with self._lock:
                        self._aria2_gids.pop(task.hash, None)
                        task.state = STATE_STOPPED
                        task.speed = 0.0
                        self._flush()
                    return "stopped"
                url, headers = client.download_url_by_fid(
                    saved_map.get(id(item), ""), name=item.name,
                )
                gid = rpc.add_uri(url, self._aria2_options(task, item, headers))
                if not gid:
                    logger.warning(f"aria2 添加任务失败：{item.name}，回落到自研分片下载")
                    self._remove_aria2_gids([gid for _item, gid in records])
                    self._discard_aria2_partials(task, records)
                    with self._lock:
                        self._aria2_gids.pop(task.hash, None)
                    return "fallback"
                records.append((item, gid))
                with self._lock:
                    self._aria2_gids.setdefault(task.hash, []).append(gid)
        except Exception as error:  # noqa: BLE001 - 取链或添加异常回落
            logger.warning(f"aria2 取链/添加异常，回落到自研分片下载：{error}")
            self._remove_aria2_gids([gid for _item, gid in records])
            self._discard_aria2_partials(task, records)
            with self._lock:
                self._aria2_gids.pop(task.hash, None)
            return "fallback"
        while True:
            if event.is_set():
                self._remove_aria2_gids([gid for _item, gid in records])
                with self._lock:
                    self._aria2_gids.pop(task.hash, None)
                    task.state = STATE_STOPPED
                    task.speed = 0.0
                    task.updated_at = time.time()
                    self._flush()
                return "stopped"
            total_done = 0
            total_speed = 0
            all_complete = True
            aria2_error = ""
            for item, gid in records:
                status = rpc.tell_status(gid)
                if not isinstance(status, dict):
                    logger.warning(f"aria2 状态查询失败：{item.name}，回落到自研分片下载")
                    self._remove_aria2_gids([gid for _item, gid in records])
                    self._discard_aria2_partials(task, records)
                    with self._lock:
                        self._aria2_gids.pop(task.hash, None)
                    return "fallback"
                state = str(status.get("status") or "")
                if state == "error":
                    aria2_error = str(status.get("errorMessage") or "未知错误")[:200]
                    break
                try:
                    completed = int(status.get("completedLength") or 0)
                except (TypeError, ValueError):
                    completed = 0
                try:
                    speed = int(status.get("downloadSpeed") or 0)
                except (TypeError, ValueError):
                    speed = 0
                item.done = min(item.size, completed) if item.size > 0 else completed
                total_done += item.done
                total_speed += speed
                if state != "complete":
                    all_complete = False
            if aria2_error:
                self._remove_aria2_gids([gid for _item, gid in records])
                self._discard_aria2_partials(task, records)
                with self._lock:
                    self._aria2_gids.pop(task.hash, None)
                # 夸克 CDN 对 aria2 的请求特征较严（403/412），这类拒绝改用自研分片，
                # 避免把本可下载的资源判失败。
                if any(code in aria2_error for code in ("status=401", "status=403",
                                                        "status=404", "status=412")):
                    logger.warning(f"aria2 请求被夸克拒绝（{aria2_error}），回落到自研分片下载")
                    return "fallback"
                raise RuntimeError(f"aria2 下载失败：{aria2_error}"[:200])
            with self._lock:
                task.done = min(task.total, total_done) if task.total else total_done
                task.speed = float(total_speed)
                task.updated_at = time.time()
                self._flush()
            if all_complete:
                with self._lock:
                    self._aria2_gids.pop(task.hash, None)
                    for item, _gid in records:
                        item.done = item.size
                    task.done = sum(entry.done for entry in task.files)
                    task.speed = 0.0
                    task.updated_at = time.time()
                    self._flush()
                return "completed"
            time.sleep(ARIA2_POLL_INTERVAL)

    def _download_one(self, task: DownloadTask, event: threading.Event, client: QuarkShareClient,
                      share_url: str, passcode: str, item: TaskFile,
                      saved_fid: Optional[str] = None) -> None:
        """下载单个文件（分片并行 + 断点续传），优先使用任务级转存 fid。"""
        if saved_fid:
            url, headers = client.download_url_by_fid(saved_fid, name=item.name)
        else:
            if not share_url:
                raise ValueError("缺少分享地址，无法取链")
            share_file = ShareFile(
                fid=item.fid, name=item.name, size=item.size, share_token=item.share_token,
            )
            url, headers = client.download_url(share_url, passcode, share_file)
        destination = Path(task.content_path) / item.rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size == item.size:
            with self._lock:
                item.done = item.size
                task.done = sum(entry.done for entry in task.files)
            return
        workers = 4
        chunk = CHUNK_SIZE
        parts: list[tuple[int, int]] = []
        position = item.done
        while position < item.size:
            parts.append((position, min(position + chunk - 1, item.size - 1)))
            position += chunk
        if not parts:
            parts = [(0, max(item.size - 1, 0))]
        previous = item.done
        with open(destination, "r+b" if destination.exists() else "w+b") as handle:
            handle.truncate(item.size)

        def run_part(bounds: tuple[int, int]) -> None:
            """下载一个字节区间并写入对应偏移。"""
            start, end = bounds
            if event.is_set():
                return
            response = RequestUtils().get_res(
                url,
                headers={**headers, "Range": f"bytes={start}-{end}"},
                timeout=120,
            )
            if response is None or response.status_code not in (200, 206):
                status = getattr(response, "status_code", "无响应")
                raise RuntimeError(f"分片下载失败：{start}-{end}，HTTP {status}")
            payload = response.content or b""
            if response.status_code == 200 and start > 0:
                raise RuntimeError("服务器不支持分段下载")
            expected = end - start + 1
            if len(payload) != expected:
                raise RuntimeError(
                    f"文件大小不一致：{item.name} 分片 {start}-{end} 收到 {len(payload)}/{expected}"
                )
            with open(destination, "r+b") as writer:
                writer.seek(start)
                writer.write(payload)
                writer.flush()
            with self._lock:
                item.done += len(payload)
                task.done = sum(entry.done for entry in task.files)
                task.updated_at = time.time()
                elapsed = max(task.updated_at - (task.created_at or task.updated_at), 1.0)
                task.speed = (task.done - previous) / elapsed

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quarkpart") as pool:
            futures = [pool.submit(run_part, bounds) for bounds in parts]
            for future in futures:
                future.result()
        if event.is_set():
            task.state = STATE_STOPPED
            self._flush()
            return
        actual = destination.stat().st_size if destination.exists() else 0
        if actual != item.size:
            raise RuntimeError(f"文件大小不一致：{item.name} {actual}/{item.size}")
        with self._lock:
            item.done = item.size
            task.done = sum(entry.done for entry in task.files)
            self._flush()
