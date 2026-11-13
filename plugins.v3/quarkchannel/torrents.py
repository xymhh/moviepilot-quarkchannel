"""合成种子的组装、签名与缓存。

设计要点：MoviePilot 只接受 magnet 或可下载的 .torrent，
因此插件把「分享文件清单」编码成合法种子，并用 comment 标记回自身。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .bencode import build_torrent, info_hash

MARKER = "QUARKCHANNEL"
CACHE_TTL_SECONDS = 3600
MEMORY_TTL_SECONDS = 600


@dataclass
class ShareEntry:
    """一份分享的本地快照。"""

    sid: str
    provider: str
    share_url: str
    passcode: str = ""
    title: str = ""
    files: list[dict] = field(default_factory=list)
    total_size: int = 0
    updated_at: float = 0.0

    @property
    def folder_name(self) -> str:
        """种子名：去掉路径非法字符，供 MoviePilot 当下载目录名。"""
        name = (self.title or self.sid).strip() or self.sid
        for char in '\\/:*?"<>|':
            name = name.replace(char, " ")
        return " ".join(name.split())[:120] or self.sid

    def share_files(self) -> list[dict]:
        """转换为 bencode 需要的文件结构。"""
        return [
            {
                "length": int(item.get("size") or 0),
                "path": [str(item.get("name") or "file")],
            }
            for item in self.files
            if int(item.get("size") or 0) > 0
        ]


def build_sid(share_url: str) -> str:
    """用分享地址生成稳定 id。"""
    return hashlib.md5(share_url.strip().encode("utf-8")).hexdigest()[:16]


def sign(sid: str, secret: str) -> str:
    """对分享 id 生成 HMAC 签名。"""
    return hmac.new(secret.encode("utf-8"), sid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify(sid: str, signature: str, secret: str) -> bool:
    """校验签名，避免匿名端点被外部枚举。"""
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(sid, secret), signature)


def render_torrent(entry: ShareEntry) -> bytes:
    """把分享快照渲染为种子字节串。"""
    files = entry.share_files()
    if not files:
        raise ValueError("分享内没有可下载的文件")
    return build_torrent(
        name=entry.folder_name,
        files=files,
        comment=f"{MARKER}|{entry.sid}|{entry.provider}",
    )


def parse_marker(content: bytes) -> Optional[tuple[str, str]]:
    """从种子内容中取回 (sid, provider)。

    comment 是 `<长度>:QUARKCHANNEL|<sid>|<provider>`，
    因此先定位标记、再回读长度前缀，避免把后续 bencode 内容当成 provider。

    :param content: 种子字节串
    :return: 命中本插件标记时返回 sid 与 provider，否则 None
    """
    if not content:
        return None
    head = content[:8192]
    marker = f"{MARKER}|".encode("utf-8")
    index = head.find(marker)
    if index <= 0:
        return None
    cursor = index - 1
    if cursor < 0 or head[cursor:cursor + 1] != b":":
        return None
    digits_end = cursor
    cursor -= 1
    while cursor >= 0 and head[cursor:cursor + 1].isdigit():
        cursor -= 1
    try:
        length = int(head[cursor + 1:digits_end])
    except ValueError:
        return None
    comment = head[index:index + length]
    parts = comment.split(b"|")
    if len(parts) < 3:
        return None
    sid = parts[1].decode("utf-8", "ignore")
    provider = parts[2].decode("utf-8", "ignore")
    return (sid, provider or "quark")


class TorrentCache:
    """分享快照缓存：内存加速 + 数据目录落盘。"""

    def __init__(self, data_dir: Path) -> None:
        """绑定缓存目录并加载已有快照。

        :param data_dir: 插件数据目录
        """
        self._dir = data_dir
        self._file = data_dir / "shares.json"
        self._lock = threading.RLock()
        self._entries: dict[str, ShareEntry] = {}
        self._loaded_at = 0.0
        self._load()

    def _load(self) -> None:
        """从磁盘恢复快照，坏文件直接忽略。"""
        self._loaded_at = time.time()
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                entry = ShareEntry(**item)
            except TypeError:
                continue
            if time.time() - (entry.updated_at or 0) < CACHE_TTL_SECONDS * 24:
                self._entries[entry.sid] = entry

    def _flush(self) -> None:
        """把快照写回磁盘，写失败不影响主流程。"""
        try:
            payload = [asdict(entry) for entry in self._entries.values()]
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError:
            return

    def put(self, entry: ShareEntry) -> None:
        """写入或更新快照。"""
        with self._lock:
            entry.updated_at = time.time()
            self._entries[entry.sid] = entry
            self._flush()

    def get(self, sid: str) -> Optional[ShareEntry]:
        """按 id 读取快照。"""
        with self._lock:
            return self._entries.get(sid)

    def drop(self, sid: str) -> None:
        """移除快照（分享失效时调用）。"""
        with self._lock:
            if self._entries.pop(sid, None) is not None:
                self._flush()

    def purge_expired(self) -> int:
        """清理过期快照，返回清理数量。"""
        deadline = time.time() - CACHE_TTL_SECONDS * 24
        with self._lock:
            expired = [sid for sid, entry in self._entries.items() if entry.updated_at < deadline]
            for sid in expired:
                self._entries.pop(sid, None)
            if expired:
                self._flush()
        return len(expired)


def sha1_of_torrent(content: bytes) -> str:
    """返回种子 info 段 hash，作为 MoviePilot 侧任务标识。"""
    return info_hash(content)
