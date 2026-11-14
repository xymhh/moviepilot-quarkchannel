"""Telegram 频道监控与本地增量索引。

模块导入时不加载 telethon、不启动线程，只有调用监控器时才延迟导入。
索引保存在插件数据目录的 ``tg_index.json``，供搜索链优先命中。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Optional

from app.sdk.logging import logger

INDEX_FILE = "tg_index.json"
DEFAULT_MAX_AGE_DAYS = 14
LINK_RE = re.compile(r"(?:https?://)?pan\.quark\.cn/s/([A-Za-z0-9_-]+)", re.I)
URL_RE = re.compile(r"(?:https?://)?pan\.quark\.cn/s/[^\s]+", re.I)
PASSCODE_RE = re.compile(r"(?:提取码|密码|pwd)\s*[:：=]?\s*([A-Za-z0-9]{4})", re.I)
PASSCODE_STRIP_RE = re.compile(
    r"(?:提取码|密码|pwd)\s*[:：=]?\s*[A-Za-z0-9]{4,8}", re.I,
)


def _normalize(value: Any) -> str:
    """去掉全部空白并转小写，用于标题模糊匹配。"""
    return "".join(str(value or "").split()).lower()


def _timestamp(value: Any, default: float = 0.0) -> float:
    """把 datetime、数字或数字字符串转成时间戳。"""
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OSError):
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int = 0) -> int:
    """把任意值安全转成整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class TgIndex:
    """Telegram 频道消息产出的本地分享索引。"""

    def __init__(self, data_dir: Path, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
        """读取索引文件并清理过期条目。

        :param data_dir: 插件数据目录
        :param max_age_days: 条目保留天数
        """
        self._data_dir = Path(data_dir)
        self._file = self._data_dir / INDEX_FILE
        self._max_age_days = max(1, int(max_age_days))
        self._cursor: dict[str, int] = {}
        self._entries: list[dict] = []
        self.last_poll = 0.0
        self._load()

    @property
    def cursor(self) -> dict[str, int]:
        """返回频道游标字典。"""
        return self._cursor

    @property
    def entries(self) -> list[dict]:
        """返回当前索引条目。"""
        return self._entries

    def __len__(self) -> int:
        """返回索引条目数量。"""
        return len(self._entries)

    def get_cursor(self, channel: str) -> int:
        """读取频道已处理的最大消息 id。"""
        try:
            return _int_value(self._cursor.get(channel))
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, channel: str, msg_id: int) -> None:
        """推进频道游标并持久化。"""
        if not channel:
            return
        current = self.get_cursor(channel)
        if _int_value(msg_id) < current:
            return
        self._cursor[channel] = _int_value(msg_id)
        self.save()

    def add_entries(self, entries: Iterable[dict]) -> int:
        """合并条目；同一 sid 保留最新一条，并清理过期数据。"""
        self._purge(self._max_age_days)
        by_sid = {
            str(item.get("sid") or ""): dict(item)
            for item in self._entries if item.get("sid")
        }
        for raw in entries or []:
            item = dict(raw or {})
            sid = str(item.get("sid") or "").strip()
            if not sid:
                continue
            item["sid"] = sid
            item["created_at"] = _timestamp(item.get("created_at"), time.time())
            existing = by_sid.get(sid)
            if existing is None:
                by_sid[sid] = item
                continue
            old_key = (_timestamp(existing.get("created_at")), _int_value(existing.get("msg_id")))
            new_key = (_timestamp(item.get("created_at")), _int_value(item.get("msg_id")))
            if new_key >= old_key:
                by_sid[sid] = item
        self._entries = list(by_sid.values())
        self._purge(self._max_age_days)
        self.save()
        return len(self._entries)

    def search(self, keyword: str, limit: int = 5,
               max_age_days: int = DEFAULT_MAX_AGE_DAYS,
               channel: Optional[str] = None) -> list[dict]:
        """按标题模糊搜索，返回按时间倒序的索引副本。

        :param keyword: 关键词
        :param limit: 返回上限
        :param max_age_days: 条目最大保留天数
        :param channel: 可选的频道过滤条件
        """
        if self._purge(self._max_age_days):
            self.save()
        needle = _normalize(keyword)
        if not needle:
            return []
        cutoff = time.time() - max(1, int(max_age_days)) * 86400
        wanted = str(channel or "").strip()
        rows = [
            dict(item) for item in self._entries
            if _timestamp(item.get("created_at")) >= cutoff
            and needle in _normalize(item.get("title"))
            and (not wanted or str(item.get("channel") or "") == wanted)
        ]
        rows.sort(
            key=lambda item: (
                _timestamp(item.get("created_at")),
                _int_value(item.get("msg_id")),
            ),
            reverse=True,
        )
        return rows[:max(1, int(limit))]

    def save(self) -> None:
        """原子写回索引文件。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            payload = {"cursor": self._cursor, "entries": self._entries}
            temp = self._file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError as error:
            logger.warning(f"写入 Telegram 索引失败：{type(error).__name__}")

    def _load(self) -> None:
        """读取索引文件，兼容损坏或旧格式。"""
        if not self._file.exists():
            return
        try:
            payload = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        cursor = payload.get("cursor")
        if isinstance(cursor, dict):
            self._cursor = {
                str(channel): _int_value(msg_id)
                for channel, msg_id in cursor.items()
                if str(channel)
            }
        entries = payload.get("entries")
        if isinstance(entries, list):
            self._entries = [dict(item) for item in entries if isinstance(item, dict)]
        if self._purge(self._max_age_days):
            self.save()

    def _purge(self, max_age_days: int) -> int:
        """删除超过保留期的条目并返回删除数量。"""
        cutoff = time.time() - max(1, int(max_age_days)) * 86400
        before = len(self._entries)
        self._entries = [
            item for item in self._entries
            if _timestamp(item.get("created_at")) >= cutoff
        ]
        return before - len(self._entries)


class TgMonitor:
    """在独立线程与独立事件循环中轮询 Telegram 频道。"""

    def __init__(self, data_dir: Path, api_id: int, api_hash: str, session: str,
                 channels: list[str], scan_limit: int = 100,
                 interval_minutes: int = 10, backfill: bool = False,
                 index: Optional[TgIndex] = None,
                 on_new_entries: Optional[Callable[[list[dict]], Any]] = None) -> None:
        """保存凭据与轮询参数；不会在此连接 Telegram。

        :param data_dir: 插件数据目录
        :param api_id: Telegram api_id
        :param api_hash: Telegram api_hash
        :param session: StringSession 字符串
        :param channels: 频道用户名或链接列表
        :param scan_limit: 单频道单次最多拉取消息数
        :param interval_minutes: 轮询间隔分钟数
        :param backfill: 首次接入时是否回补最近 scan_limit 条消息
        :param index: 可选的共享索引；多渠道监控共用一份 tg_index.json
        :param on_new_entries: 可选的新增条目回调，支持同步或异步函数
        """
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else TgIndex(self.data_dir)
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")
        self.session = str(session or "")
        self.channels = [str(item).strip() for item in channels or [] if str(item).strip()]
        self.scan_limit = max(1, int(scan_limit or 100))
        self.interval_minutes = max(1, int(interval_minutes or 10))
        self.backfill = bool(backfill)
        self.on_new_entries = on_new_entries
        self.last_poll = self.index.last_poll
        self._client: Any = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """启动独立监控线程；重复调用安全。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="quarkchannel-tg",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止监控线程，并在超时后收敛返回。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("Telegram 监控停止超时，线程仍在退出中")
        self._thread = None

    async def poll_once(self, client: Any = None,
                        channels: Optional[Iterable[str]] = None) -> list[dict]:
        """扫描频道一次，返回本次新增的索引条目。

        :param client: 可选的 Telethon 客户端；为空时使用监控器自己的客户端
        :param channels: 可选的本次扫描频道列表；为空时扫描全部已配置频道
        :return: 新增条目列表
        """
        client = client or self._client or getattr(self, "client", None)
        if client is None:
            client = await self._ensure_client()
        if client is None:
            return []
        targets = list(channels) if channels is not None else self.channels
        added: list[dict] = []
        for channel in targets:
            if self._stop_event.is_set():
                break
            while not self._stop_event.is_set():
                cursor = self.index.get_cursor(channel)
                if cursor <= 0:
                    # 首次接入默认只定位水位；开启 backfill 时回补最近 scan_limit 条，
                    # 便于新装用户立刻有可搜索的索引。
                    try:
                        recent = await self._recent_messages(client, channel, self.scan_limit)
                    except Exception as error:  # noqa: BLE001 - 取水位失败留到下一轮
                        delay = self._flood_wait_delay(error)
                        if delay is not None:
                            logger.warning(f"Telegram 频道 {channel} 限流，等待 {delay} 秒")
                            await self._sleep(delay)
                            continue
                        logger.warning(
                            f"Telegram 频道 {channel} 取最新消息失败：{type(error).__name__}"
                        )
                        recent = []
                    latest = max((self._message_id(item) for item in recent), default=0)
                    if self.backfill and recent:
                        backfilled: list[dict] = []
                        for message in recent:
                            msg_id = self._message_id(message)
                            if msg_id <= 0:
                                continue
                            # 回补按“入索引时间”记账：历史消息可能早于保留期，
                            # 用原始时间会被立即清理掉。
                            backfilled.extend(self.parse_message(
                                self._message_text(message), channel, msg_id,
                                time.time(),
                            ))
                        if backfilled:
                            self.index.add_entries(backfilled)
                            await self._notify_new_entries(backfilled)
                        logger.info(
                            f"Telegram 频道 {channel} 回补 {len(recent)} 条消息，"
                            f"入索引 {len(backfilled)} 条"
                        )
                    if latest > 0:
                        self.index.set_cursor(channel, latest)
                        logger.info(f"Telegram 频道 {channel} 初始化水位：{latest}")
                    break
                max_id = cursor
                channel_entries: list[dict] = []
                try:
                    result = client.iter_messages(
                        channel, min_id=cursor, limit=self.scan_limit, reverse=True,
                    )
                    if hasattr(result, "__await__"):
                        result = await result
                    async for message in self._iterate(result):
                        msg_id = self._message_id(message)
                        if msg_id <= 0 or msg_id <= cursor:
                            continue
                        entries = self.parse_message(
                            self._message_text(message), channel, msg_id,
                            self._message_time(message),
                        )
                        channel_entries.extend(entries)
                        if msg_id > max_id:
                            max_id = msg_id
                    logger.info(
                        f"Telegram 频道 {channel} 扫描完成：游标 {cursor}→{max_id}，"
                        f"新增条目 {len(channel_entries)}"
                    )
                    if channel_entries:
                        self.index.add_entries(channel_entries)
                    if max_id != cursor:
                        self.index.set_cursor(channel, max_id)
                    added.extend(channel_entries)
                    break
                except Exception as error:  # noqa: BLE001 - 单频道失败不影响其它频道
                    delay = self._flood_wait_delay(error)
                    if delay is not None:
                        logger.warning(f"Telegram 频道 {channel} 限流，等待 {delay} 秒")
                        await self._sleep(delay)
                        continue
                    # 其它异常不推进游标，下一轮会重新扫描并按 sid 去重
                    logger.warning(
                        f"Telegram 频道 {channel} 拉取失败：{type(error).__name__}"
                    )
                    break
        if added:
            await self._notify_new_entries(added)
        self.last_poll = time.time()
        self.index.last_poll = self.last_poll
        return added

    async def _notify_new_entries(self, entries: list[dict]) -> None:
        """调用新增条目回调；回调异常只记录 warning。

        :param entries: 本轮新入索引的条目
        """
        callback = self.on_new_entries
        if callback is None or not entries:
            return
        try:
            result = callback([dict(item) for item in entries])
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001 - 回调失败不能影响监控
            logger.warning(f"Telegram 新条目回调失败：{type(error).__name__}")

    @staticmethod
    def extract_sids(text: str) -> list[str]:
        """按出现顺序提取并去重夸克分享 id。"""
        return list(dict.fromkeys(LINK_RE.findall(text or "")))

    @staticmethod
    def extract_passcode(text: str) -> str:
        """提取消息里的提取码、密码或 pwd 参数。"""
        match = PASSCODE_RE.search(text or "")
        return match.group(1) if match else ""

    @staticmethod
    def parse_message(text: str, channel: str, msg_id: int,
                      created_at: Any = None) -> list[dict]:
        """把一条频道消息解析成索引条目；无夸克链接返回空列表。"""
        text = str(text or "")
        sids = TgMonitor.extract_sids(text)
        if not sids:
            return []
        passcode = TgMonitor.extract_passcode(text)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        title = URL_RE.sub(" ", first_line)
        title = PASSCODE_STRIP_RE.sub(" ", title)
        title = " ".join(title.split()).strip(" -|:：")
        title = title[:120]
        stamp = _timestamp(created_at, time.time()) if created_at is not None else time.time()
        channel_name = str(channel or "")
        return [
            {
                "sid": sid,
                "share_url": f"https://pan.quark.cn/s/{sid}",
                "passcode": passcode,
                "title": title,
                "channel": channel_name,
                "msg_id": _int_value(msg_id),
                "created_at": stamp,
            }
            for sid in sids
        ]

    @staticmethod
    def _parse_message(text: str, channel: str, msg_id: int,
                       created_at: Any = None) -> list[dict]:
        """兼容私有命名的消息解析入口。"""
        return TgMonitor.parse_message(text, channel, msg_id, created_at)

    async def _ensure_client(self) -> Any:
        """延迟导入 telethon 并建立客户端连接；失败返回 None。"""
        if self._client is not None:
            return self._client
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            logger.warning("未安装 telethon，Telegram 频道监控不可用")
            return None
        try:
            self._client = TelegramClient(
                StringSession(self.session), self.api_id, self.api_hash,
            )
            await self._client.connect()
        except Exception as error:  # noqa: BLE001 - 连接失败降级为不监控
            logger.warning(f"Telegram 客户端连接失败：{type(error).__name__}")
            self._client = None
            return None
        return self._client

    async def _disconnect(self) -> None:
        """断开客户端；清理失败忽略。"""
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - 退出阶段不再抛错
            pass

    def _thread_main(self) -> None:
        """独立线程入口：创建事件循环并运行轮询。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._run_loop())
        except Exception as error:  # noqa: BLE001 - 监控异常不能拖垮宿主
            logger.warning(f"Telegram 监控线程退出：{type(error).__name__}")
        finally:
            try:
                loop.run_until_complete(self._disconnect())
            except Exception:  # noqa: BLE001 - 退出阶段不再抛错
                pass
            loop.close()
            self._loop = None

    async def _run_loop(self) -> None:
        """首次立即轮询，之后按配置间隔循环。"""
        client = await self._ensure_client()
        if client is None:
            return
        interval = self.interval_minutes * 60
        while not self._stop_event.is_set():
            try:
                await self.poll_once(client)
            except Exception as error:  # noqa: BLE001 - 单轮失败继续下一轮
                logger.warning(f"Telegram 轮询失败：{type(error).__name__}")
            await self._sleep(interval)

    @staticmethod
    async def _recent_messages(client: Any, channel: str, limit: int) -> list[Any]:
        """读取频道最近 N 条消息（新→旧）。

        :param client: Telethon 客户端或测试桩
        :param channel: 频道标识
        :param limit: 条数上限
        :return: 消息列表
        """
        result = client.iter_messages(channel, limit=limit)
        if hasattr(result, "__await__"):
            result = await result
        items: list[Any] = []
        if hasattr(result, "__aiter__"):
            async for item in result:
                items.append(item)
                if len(items) >= limit:
                    break
        else:
            items = list(result or [])[:limit]
        return items

    @staticmethod
    async def _latest_message(client: Any, channel: str) -> Any:
        """读取频道最新一条消息，用于首次接入时定位水位。

        :param client: Telethon 客户端或测试桩
        :param channel: 频道标识
        :return: 最新消息对象；没有消息时返回 None
        """
        result = client.iter_messages(channel, limit=1)
        if hasattr(result, "__await__"):
            result = await result
        items = []
        if hasattr(result, "__aiter__"):
            async for item in result:
                items.append(item)
                break
        else:
            items = list(result or [])[:1]
        return items[0] if items else None

    async def _sleep(self, seconds: float) -> None:
        """可被 stop 打断的分段睡眠。"""
        remaining = max(0.0, float(seconds or 0))
        while remaining > 0 and not self._stop_event.is_set():
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    @staticmethod
    async def _iterate(result: Any) -> AsyncIterator[Any]:
        """同时兼容异步迭代器、协程结果与普通列表。"""
        if hasattr(result, "__aiter__"):
            async for item in result:
                yield item
            return
        for item in result or []:
            yield item

    @staticmethod
    def _message_id(message: Any) -> int:
        """读取消息 id。"""
        try:
            return _int_value(getattr(message, "id", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _message_text(message: Any) -> str:
        """读取消息正文，兼容不同 Telethon 字段。"""
        for name in ("message", "text", "raw_text"):
            value = getattr(message, name, None)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _message_time(message: Any) -> float:
        """读取消息时间并转为时间戳。"""
        value = getattr(message, "date", None)
        if isinstance(value, datetime):
            return _timestamp(value, time.time())
        if value is None:
            return time.time()
        return _timestamp(value, time.time())

    @staticmethod
    def _flood_wait_delay(error: Exception) -> Optional[int]:
        """识别 FloodWaitError；非限流异常返回 None。"""
        try:
            from telethon.errors import FloodWaitError
            if isinstance(error, FloodWaitError):
                return max(1, int(getattr(error, "seconds", 60) or 60))
        except Exception:  # noqa: BLE001 - telethon 缺失时按类名判断
            pass
        if error.__class__.__name__ == "FloodWaitError":
            return max(1, int(getattr(error, "seconds", 60) or 60))
        return None
