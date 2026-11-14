"""渠道数据模型与 JSON 持久化。

渠道是插件的一等对象：pansou 与 Telegram 频道都通过统一的
``channels.json`` 增删改启停。模块只依赖标准库和宿主日志。
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.sdk.logging import logger

CHANNELS_FILE = "channels.json"
CHANNEL_TYPES = ("pansou", "tg_channel")
PRIORITY_MIN = -9999
PRIORITY_MAX = 9999


def _as_text(value: Any) -> str:
    """把任意值转成去空白字符串。"""
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    """把列表、逗号串或多行文本统一成去重字符串列表。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = re.split(r"[,，\n|]+", value)
    else:
        items = []
    return [item.strip() for item in items if item.strip()]


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把值转成受限整数，异常时返回默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _normalize_config(channel_type: str, raw: Any) -> dict:
    """校验并规范化类型专属配置。

    :param channel_type: pansou / tg_channel
    :param raw: 原始配置
    :return: 规范化后的配置副本
    :raises ValueError: 必填配置缺失
    """
    config = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if channel_type == "pansou":
        base_url = _as_text(config.get("base_url")).rstrip("/")
        if not base_url:
            raise ValueError("pansou 渠道缺少 base_url")
        src = _as_text(config.get("src")).lower() or "all"
        config.update({
            "base_url": base_url,
            "token": _as_text(config.get("token")),
            "username": _as_text(config.get("username")),
            "password": str(config.get("password") or ""),
            "src": src if src in {"all", "tg", "plugin"} else "all",
            "plugins": _as_list(config.get("plugins")),
            "cloud_types": _as_list(config.get("cloud_types")),
        })
        return config
    channel = _as_text(config.get("channel"))
    if not channel:
        raise ValueError("tg_channel 渠道缺少 channel")
    config.update({
        "channel": channel,
        "interval_minutes": _as_int(config.get("interval_minutes"), 10, 1, 1440),
        "scan_limit": _as_int(config.get("scan_limit"), 100, 1, 1000),
        "backfill": bool(config.get("backfill")),
    })
    return config


def _new_stats() -> dict:
    """返回新渠道的统计结构。"""
    return {"last_scan": 0.0, "last_error": "", "hits": 0}


def _new_id() -> str:
    """生成 12 位渠道 id。"""
    return uuid.uuid4().hex[:12]


class ChannelStore:
    """渠道存储：线程安全 CRUD 与原子 JSON 落盘。"""

    def __init__(self, data_dir: Path) -> None:
        """绑定数据目录并加载渠道文件。

        :param data_dir: 插件数据目录
        """
        self._dir = Path(data_dir)
        self._file = self._dir / CHANNELS_FILE
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        """从磁盘恢复渠道；损坏文件按空列表处理。"""
        if not self._file.exists():
            return
        try:
            payload = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rows = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = self._build_item(row, existing=None)
            except (TypeError, ValueError):
                continue
            self._items.append(item)
        self._sort_locked()

    def _save_locked(self) -> None:
        """原子写回渠道文件；调用方必须持锁。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            temp = self._file.with_suffix(".json.tmp")
            payload = {"items": self._items}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError as error:
            logger.warning(f"写入渠道文件失败：{type(error).__name__}")

    def _sort_locked(self) -> None:
        """按 priority 倒序稳定排序。"""
        self._items.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)

    def _build_item(self, payload: dict, existing: Optional[dict] = None) -> dict:
        """合并已有渠道与局部字段，生成规范化的渠道对象。"""
        source = dict(existing or {})
        for key in ("id", "name", "type", "enabled", "priority", "config", "stats"):
            if key in payload:
                source[key] = payload[key]
        channel_type = _as_text(source.get("type"))
        if channel_type not in CHANNEL_TYPES:
            raise ValueError("渠道类型必须是 pansou 或 tg_channel")
        name = _as_text(source.get("name"))
        if not name:
            raise ValueError("渠道名称不能为空")
        config = _normalize_config(channel_type, source.get("config"))
        priority = _as_int(source.get("priority"), 0, PRIORITY_MIN, PRIORITY_MAX)
        stats = copy.deepcopy(source.get("stats")) if isinstance(source.get("stats"), dict) else {}
        normalized_stats = _new_stats()
        normalized_stats.update({
            "last_scan": float(stats.get("last_scan") or 0.0),
            "last_error": str(stats.get("last_error") or ""),
            "hits": _as_int(stats.get("hits"), 0, 0, 2**31 - 1),
        })
        return {
            "id": _as_text(source.get("id")) or _new_id(),
            "name": name,
            "type": channel_type,
            "enabled": bool(source.get("enabled", True)),
            "priority": priority,
            "config": config,
            "stats": normalized_stats,
        }

    def list(self) -> list[dict]:
        """返回按 priority 倒序排列的渠道副本。"""
        with self._lock:
            self._sort_locked()
            return [copy.deepcopy(item) for item in self._items]

    def get(self, channel_id: str) -> Optional[dict]:
        """按 id 读取渠道副本。"""
        target = _as_text(channel_id)
        with self._lock:
            for item in self._items:
                if item.get("id") == target:
                    return copy.deepcopy(item)
        return None

    def create(self, payload: dict) -> dict:
        """新增渠道并返回完整对象。

        :param payload: 渠道字段
        :return: 新渠道副本
        :raises ValueError: 名称、类型或配置不合法
        """
        item = self._build_item(payload or {}, existing=None)
        with self._lock:
            while any(row.get("id") == item["id"] for row in self._items):
                item["id"] = _new_id()
            item["stats"] = _new_stats()
            self._items.append(item)
            self._sort_locked()
            self._save_locked()
        return copy.deepcopy(item)

    def update(self, channel_id: str, patch: dict) -> Optional[dict]:
        """局部更新渠道并返回新副本；不存在时返回 None。

        :param channel_id: 渠道 id
        :param patch: 需要更新的字段
        :raises ValueError: 更新后的渠道不合法
        """
        target = _as_text(channel_id)
        with self._lock:
            for index, current in enumerate(self._items):
                if current.get("id") != target:
                    continue
                updated = self._build_item(patch or {}, existing=current)
                self._items[index] = updated
                self._sort_locked()
                self._save_locked()
                return copy.deepcopy(updated)
        return None

    def delete(self, channel_id: str) -> bool:
        """删除渠道；成功返回 True。"""
        target = _as_text(channel_id)
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.get("id") != target]
            if len(self._items) == before:
                return False
            self._save_locked()
            return True

    def record_stat(self, channel_id: str, hits: int = 0,
                    error: Optional[str] = None) -> Optional[dict]:
        """累加命中数并记录扫描时间/错误；渠道不存在时返回 None。"""
        target = _as_text(channel_id)
        with self._lock:
            for item in self._items:
                if item.get("id") != target:
                    continue
                stats = item.setdefault("stats", _new_stats())
                stats["hits"] = _as_int(stats.get("hits"), 0, 0, 2**31 - 1) + max(0, _as_int(hits, 0, 0, 2**31 - 1))
                stats["last_scan"] = time.time()
                if error is not None:
                    stats["last_error"] = str(error)
                self._save_locked()
                return copy.deepcopy(item)
        return None

    def migrate_from_config(self, config: dict) -> int:
        """channels.json 不存在时从旧配置生成渠道，返回新增数量。"""
        with self._lock:
            if self._file.exists():
                return 0
            old = dict(config or {})
            migrated: list[dict] = []
            pansou_url = _as_text(old.get("pansou_url"))
            if pansou_url:
                migrated.append(self._build_item({
                    "name": "盘搜",
                    "type": "pansou",
                    "enabled": True,
                    "priority": 50,
                    "config": {
                        "base_url": pansou_url,
                        "token": old.get("pansou_token", ""),
                        "username": old.get("pansou_user", ""),
                        "password": old.get("pansou_password", ""),
                        "src": old.get("pansou_src", "all"),
                        "plugins": old.get("pansou_plugins", ""),
                        "cloud_types": old.get("pansou_cloud_types", "quark"),
                    },
                }))
            for channel in _as_list(old.get("tg_channels")):
                migrated.append(self._build_item({
                    "name": channel,
                    "type": "tg_channel",
                    "enabled": True,
                    "priority": 30,
                    "config": {
                        "channel": channel,
                        "interval_minutes": old.get("tg_interval_minutes", 10),
                        "scan_limit": old.get("tg_scan_limit", 100),
                        "backfill": old.get("tg_backfill", False),
                    },
                }))
            if not migrated:
                return 0
            self._items.extend(migrated)
            self._sort_locked()
            self._save_locked()
            return len(migrated)
