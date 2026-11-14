"""网盘直链频道（QuarkChannel）。

把网盘分享链接转换成 MoviePilot 能识别的种子资源，
再由本插件自己完成「取链 → 分片下载 → 回报任务状态」。

MoviePilot 侧只负责：搜索聚合、订阅、过滤、下载管理、整理、STRM、Emby 与通知。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import quote

from fastapi.responses import Response

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas import (
    DOWNLOAD_TASK_ALREADY_EXISTS,
    DownloaderFile,
    DownloaderTorrent,
)

try:  # 与站点索引模块同源：MoviePilot 搜索链会直接调用 to_dict()
    from app.domain.context import TorrentInfo
except ImportError:  # pragma: no cover - 兼容宿主结构调整
    from app.schemas import TorrentInfo
from app.schemas.types import MediaType, SystemConfigKey, TorrentQueryStatus

from .aria2 import Aria2Launcher
from .channels import ChannelStore
from .downloader import (
    DownloadManager,
    DownloadTask,
    STATE_COMPLETED,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_STOPPED,
    TaskFile,
)
from .picker import AutoPicker
from .shares.quark import QuarkShareClient, extract_pwd_id
from .sources.pansou import PansouSource, normalize_share_url
from .sources.tgchannel import TgIndex, TgMonitor
from .torrents import (
    ShareEntry,
    TorrentCache,
    build_sid,
    parse_marker,
    render_torrent,
    sha1_of_torrent,
    sign,
    verify,
)

EPISODE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[._ ]?[Ee](\d{1,4})"),
    re.compile(r"(?:第|EP|Ep|ep)\s*(\d{1,4})[集话話]"),
    re.compile(r"(?:第|EP|Ep|ep)\s*(\d{1,4})\s*[-~]\s*\d{1,4}"),
    re.compile(r"[\[\(【]\s*(\d{1,4})\s*[\]\)】]"),
    re.compile(r"(?<![\dA-Za-z])(\d{1,4})\s*[-~]\s*\d{1,4}(?![\dA-Za-z])"),
    # 纯数字文件名：01.mp4 / 12.mkv（排除 1080p 这类分辨率）
    re.compile(r"^\s*(\d{1,4})\s*(?:\.|$|[-_. ])"),
)
STORAGE_PREFIX = ("u115:", "alipan:", "rclone:", "alist:", "alistgo:", "smb:")
SEASON_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[._ ]?[Ee](\d{1,4})")
MAX_EXPAND_FILES = 0  # 0 表示不限制，按集完整展开
VIDEO_SUFFIXES = (
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".rmvb", ".webm", ".mpg", ".mpeg", ".iso", ".m4v",
)
PENDING_SHARE_LIMIT = 500
PREWARM_COUNT = 2
PREWARM_DELAY_SECONDS = 0.2
SNAPSHOT_TIMEOUT_SECONDS = 8.0


class QuarkChannel(_PluginBase):
    """网盘直链频道插件。"""

    plugin_name = "网盘直链频道"
    plugin_desc = "把夸克分享链接转成 MoviePilot 原生资源，并下载到本地目录。"
    plugin_icon = "QuarkChannel.png"
    plugin_version = "0.13.5"
    plugin_author = "xymhh"
    author_url = "https://github.com/xymhh"
    plugin_config_prefix = "quarkchannel_"
    plugin_order = 60
    auth_level = 1

    _enabled: bool = False
    _config: dict = {}
    _cache: Optional[TorrentCache] = None
    _manager: Optional[DownloadManager] = None
    _pansou: Optional[PansouSource] = None
    _channels: Optional[ChannelStore] = None
    _tg_index: Optional[TgIndex] = None
    _tg_monitors: list[TgMonitor] = []
    _picker: Optional[AutoPicker] = None
    _channel_sources: dict[str, tuple[tuple, PansouSource]] = {}
    _secret: str = ""
    _aria2_secret: str = ""

    def __init__(self) -> None:
        """初始化进程内候选登记表与后台预热队列。"""
        super().__init__()
        self._pending_shares: OrderedDict[str, dict] = OrderedDict()
        self._search_cache: dict[str, tuple[float, list]] = {}
        self._pending_lock = threading.RLock()
        self._prewarm_pool: Optional[ThreadPoolExecutor] = None
        self._prewarm_lock = threading.Lock()
        self._prewarm_inflight: set[str] = set()
        self._tg_monitors = []
        self._channel_sources = {}
        self._picker = None

    # ---------- 生命周期 ----------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并重建运行状态；允许重复调用。"""
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._config = config
        data_dir = self.get_data_path()
        self._stop_prewarm()
        with self._pending_lock:
            self._pending_shares.clear()
        self._cache = TorrentCache(data_dir)
        self._stop_tg_monitor()
        self._tg_index = TgIndex(data_dir)
        self._channels = ChannelStore(data_dir)
        self._channel_sources = {}
        try:
            migrated = self._channels.migrate_from_config(config)
        except Exception as error:  # noqa: BLE001 - 迁移失败不阻断插件启动
            migrated = 0
            logger.warning(f"渠道迁移失败：{type(error).__name__}")
        if migrated:
            logger.info(f"已从旧配置迁移 {migrated} 个渠道")
        self._picker = AutoPicker(
            data_dir=data_dir,
            cache=self._cache,
            snapshot_fetcher=self._ensure_entry,
            torrent_url_builder=self._torrent_url,
            enabled=bool(config.get("auto_pick_enabled")),
            dry_run=bool(config.get("auto_pick_dry_run", True)),
            min_similarity=float(
                config.get("auto_pick_min_similarity")
                if config.get("auto_pick_min_similarity") is not None else 0.75
            ),
            downloader=str(config.get("auto_pick_downloader") or "网盘直链"),
            save_path=str(config.get("download_root") or "/downloads"),
        )
        sign_secret = str(config.get("sign_secret") or "").strip()
        aria2_secret = str(config.get("aria2_secret") or "").strip()
        config_changed = False
        if not sign_secret:
            sign_secret = secrets.token_hex(16)
            config["sign_secret"] = sign_secret
            config_changed = True
        if not aria2_secret:
            aria2_secret = secrets.token_hex(16)
            config["aria2_secret"] = aria2_secret
            config_changed = True
        if config_changed:
            try:
                self.update_config(config)
            except Exception as error:  # noqa: BLE001 - 配置回写失败不应阻断启用
                logger.warning(f"直链频道密钥回写失败：{error}")
        self._secret = sign_secret
        self._aria2_secret = aria2_secret
        self._manager = DownloadManager(
            data_dir,
            self._make_client,
            max_tasks=max(1, min(int(config.get("max_tasks") or 3), 6)),
            aria2_enabled=bool(config.get("aria2_enabled", True)),
            aria2_connections=max(1, min(int(config.get("aria2_connections") or 64), 128)),
            aria2_split=max(1, min(int(config.get("aria2_split") or 64), 128)),
            aria2_launcher=Aria2Launcher(data_dir, self._download_root(), aria2_secret),
        )
        self._pansou = PansouSource(
            str(config.get("pansou_url") or "").strip(),
            token=str(config.get("pansou_token") or ""),
            username=str(config.get("pansou_user") or ""),
            password=str(config.get("pansou_password") or ""),
            timeout=max(5, min(int(config.get("pansou_timeout") or 20), 120)),
            src=str(config.get("pansou_src") or "all"),
            plugins=str(config.get("pansou_plugins") or ""),
            cloud_types=str(config.get("pansou_cloud_types") or "quark"),
        )
        dist_file = Path(__file__).resolve().parent / "dist/assets/remoteEntry.js"
        if not dist_file.exists():
            logger.warning("渠道管理前端资源未构建，暂时保留旧版 JSON 配置页")
        self._start_tg_runtime()
        if self._enabled:
            self._ensure_downloader_entry()
            self._ensure_download_dir()
        logger.info(
            f"网盘直链频道初始化：enabled={self._enabled}，"
            f"pansou={bool(self._pansou.ready)}，下载根目录={self._download_root()}"
        )

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止 Telegram 监控、预热队列与下载线程并释放资源。"""
        self._stop_prewarm()
        self._stop_tg_monitor()
        if self._manager:
            self._manager.shutdown()
        self._manager = None
        self._picker = None
        self._enabled = False

    def _stop_prewarm(self) -> None:
        """停止后台预热线程池，不等待正在执行的快照。"""
        with self._prewarm_lock:
            pool = self._prewarm_pool
            self._prewarm_pool = None
            self._prewarm_inflight.clear()
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)

    def _stop_tg_monitor(self) -> None:
        """停止并释放全部 Telegram 监控线程。"""
        monitors = list(self._tg_monitors)
        self._tg_monitors = []
        for monitor in monitors:
            try:
                monitor.stop()
            except Exception as error:  # noqa: BLE001 - 停止失败不阻断插件卸载
                logger.warning(f"停止 Telegram 监控失败：{type(error).__name__}")

    def _shared_tg_index(self, data_dir: Path) -> TgIndex:
        """返回进程内唯一索引，避免空索引被布尔判断重建。"""
        if self._tg_index is None:
            self._tg_index = TgIndex(data_dir)
        return self._tg_index

    def _tg_credentials(self, config: dict) -> Optional[tuple[int, str, str]]:
        """读取全局 Telegram 凭据；缺失或格式错误时返回 None。"""
        try:
            api_id = int(config.get("tg_api_id") or 0)
        except (TypeError, ValueError):
            logger.warning("Telegram api_id 无效，未启动监控")
            return None
        api_hash = str(config.get("tg_api_hash") or "").strip()
        session = str(config.get("tg_session") or "").strip()
        if not api_id or not api_hash or not session:
            return None
        return api_id, api_hash, session

    def _start_tg_runtime(self) -> None:
        """按渠道模式启动监控；无渠道时回退旧的全局配置。"""
        config = dict(self._config or {})
        data_dir = self.get_data_path()
        channels = self._channels.list() if self._channels else []
        if channels:
            self._start_channel_monitors(channels, config, data_dir)
        else:
            self._start_legacy_monitor(config, data_dir)

    def _start_channel_monitors(self, channels: list[dict], config: dict,
                                data_dir: Path) -> None:
        """为每个启用的 tg_channel 渠道建立独立监控，同频道只保留一个。"""
        targets = [
            item for item in channels
            if item.get("enabled") and item.get("type") == "tg_channel"
        ]
        if not targets:
            return
        credentials = self._tg_credentials(config)
        if not credentials:
            logger.warning("Telegram 监控配置不完整，未启动")
            return
        api_id, api_hash, session = credentials
        index = self._shared_tg_index(data_dir)
        seen: set[str] = set()
        for channel in targets:
            cfg = channel.get("config") or {}
            target = str(cfg.get("channel") or "").strip()
            if not target or target in seen:
                continue
            seen.add(target)
            try:
                monitor = TgMonitor(
                    data_dir, api_id, api_hash, session, [target],
                    scan_limit=int(cfg.get("scan_limit") or 100),
                    interval_minutes=int(cfg.get("interval_minutes") or 10),
                    backfill=bool(cfg.get("backfill")),
                    index=index,
                    on_new_entries=self._on_tg_entries,
                )
                monitor.start()
                self._tg_monitors.append(monitor)
            except Exception as error:  # noqa: BLE001 - 单频道失败不影响其它频道
                logger.warning(f"Telegram 渠道监控启动失败：{type(error).__name__}")
        if self._tg_monitors:
            logger.info(f"Telegram 渠道监控已启动：{len(self._tg_monitors)} 个渠道")

    def _start_legacy_monitor(self, config: dict, data_dir: Path) -> None:
        """无渠道数据时沿用旧 tg_enabled/tg_channels 配置。"""
        if not bool(config.get("tg_enabled")):
            return
        credentials = self._tg_credentials(config)
        if not credentials:
            logger.warning("Telegram 监控配置不完整，未启动")
            return
        raw_channels = config.get("tg_channels") or ""
        if isinstance(raw_channels, (list, tuple)):
            raw_items = [str(item) for item in raw_channels]
        else:
            raw_items = re.split(r"[,，\n|]+", str(raw_channels))
        channels = list(dict.fromkeys(
            item.strip() for item in raw_items if str(item).strip()
        ))
        if not channels:
            logger.warning("Telegram 监控配置不完整，未启动")
            return
        try:
            interval = max(1, min(int(config.get("tg_interval_minutes") or 10), 1440))
            scan_limit = max(1, min(int(config.get("tg_scan_limit") or 100), 1000))
        except (TypeError, ValueError):
            logger.warning("Telegram 监控参数无效，未启动")
            return
        api_id, api_hash, session = credentials
        index = self._shared_tg_index(data_dir)
        try:
            monitor = TgMonitor(
                data_dir, api_id, api_hash, session, channels,
                scan_limit=scan_limit, interval_minutes=interval,
                backfill=bool(config.get("tg_backfill")), index=index,
                on_new_entries=self._on_tg_entries,
            )
            monitor.start()
            self._tg_monitors.append(monitor)
            logger.info(
                f"Telegram 频道监控已启动：{len(channels)} 个频道，"
                f"间隔 {interval} 分钟"
            )
        except Exception as error:  # noqa: BLE001 - 监控失败不影响搜索下载
            logger.warning(f"Telegram 监控启动失败：{type(error).__name__}")

    def _on_tg_entries(self, entries: list[dict]) -> None:
        """监控发现新条目后交给自动追更决策器。

        :param entries: 本轮新入索引的条目
        """
        picker = self._picker
        if picker is None:
            return
        try:
            picker.process(entries)
        except Exception as error:  # noqa: BLE001 - 自动追更不能影响监控线程
            logger.warning(f"自动追更处理新条目失败：{type(error).__name__}")

    def _refresh_channel_runtime(self) -> None:
        """渠道变更后重建 pansou 实例与 Telegram 监控。"""
        self._channel_sources = {}
        self._stop_tg_monitor()
        self._start_tg_runtime()

    # ---------- 页面与接口 ----------

    def get_command(self) -> list[dict]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> list[dict]:
        """注册插件接口。"""
        return [
            {
                "path": "/torrent",
                "endpoint": self.api_torrent,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "按分享 id 输出合成种子",
            },
            {
                "path": "/share",
                "endpoint": self.api_share,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "登记一条分享（手工测试用）",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看任务与缓存状态",
            },
            {
                "path": "/linkdiag",
                "endpoint": self.api_linkdiag,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取链后对比 python 与 aria2 的下载响应（排障用）",
            },
            {
                "path": "/selftest",
                "endpoint": self.api_selftest,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按分享链路自检：快照→转存→取链（排障用）",
            },
            {
                "path": "/probe",
                "endpoint": self.api_probe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按关键词探测盘搜与分享可用性（排障用）",
            },
            {
                "path": "/cleanup",
                "endpoint": self.api_cleanup,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理过期分享缓存",
            },
            {
                "path": "/search_test",
                "endpoint": self.api_search_test,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "直接调用搜索链，查看返回的条目（排障用）",
            },
            {
                "path": "/auto_pick",
                "endpoint": self.api_auto_pick,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看最近自动追更决策",
            },
            {
                "path": "/shares",
                "endpoint": self.api_shares,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "按关键词列出分享（不展开集）",
            },
            {
                "path": "/shares/{sid}/episodes",
                "endpoint": self.api_share_episodes,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "列出分享内文件与集号",
            },
            {
                "path": "/download_episodes",
                "endpoint": self.api_download_episodes,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按集号或文件 id 提交下载",
            },
            {
                "path": "/channels",
                "endpoint": self.api_channels,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "列出全部渠道",
            },
            {
                "path": "/channels",
                "endpoint": self.api_channel_create,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "新增渠道",
            },
            {
                "path": "/channels/summary",
                "endpoint": self.api_channels_summary,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "渠道汇总统计",
            },
            {
                "path": "/channels/{channel_id}",
                "endpoint": self.api_channel_update,
                "methods": ["PUT"],
                "auth": "bear",
                "summary": "更新渠道",
            },
            {
                "path": "/channels/{channel_id}",
                "endpoint": self.api_channel_delete,
                "methods": ["DELETE"],
                "auth": "bear",
                "summary": "删除渠道",
            },
            {
                "path": "/channels/{channel_id}/test",
                "endpoint": self.api_channel_test,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "探测渠道可用性",
            },
            {
                "path": "/channels/{channel_id}/scan",
                "endpoint": self.api_channel_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描 TG 渠道",
            },
        ]

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面与默认值：按渠道分卡片，用选项卡切换。"""

        def field(model: str, label: str, *, kind: str = "text", hint: str = "",
                  placeholder: str = "", cols: int = 6, items: list | None = None) -> dict:
            """生成一个配置控件。

            :param model: 配置键
            :param label: 显示名
            :param kind: text / password / number / switch / select
            :param hint: 说明文字
            :param placeholder: 占位提示
            :param cols: 栅格宽度
            :param items: select 选项
            :return: Vuetify JSON 片段
            """
            props: dict[str, Any] = {
                "model": model,
                "label": label,
                "hide-details": "auto",
            }
            if kind == "switch":
                if hint:
                    props["hint"] = hint
                component = "VSwitch"
            else:
                if hint:
                    props["hint"] = hint
                if placeholder:
                    props["placeholder"] = placeholder
                if kind == "password":
                    props["type"] = "password"
                elif kind == "number":
                    props["type"] = "number"
                    props["inputmode"] = "numeric"
                if kind == "select":
                    component = "VSelect"
                    props["items"] = items or []
                elif kind == "textarea":
                    component = "VTextarea"
                    props.update({
                        "rows": 4,
                        "auto-grow": True,
                        "variant": "outlined",
                        "class": "text-body-2",
                    })
                else:
                    component = "VTextField"
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": cols},
                "content": [{"component": component, "props": props}],
            }

        def card(title: str, fields: list[dict], subtitle: str = "") -> dict:
            """把一组控件包成一张卡片。

            :param title: 卡片标题
            :param fields: 控件列表
            :param subtitle: 卡片副标题
            :return: 卡片片段
            """
            header: dict[str, Any] = {
                "component": "VCardTitle",
                "props": {"class": "text-subtitle-1 py-3"},
                "text": title,
            }
            content: list[dict] = [header]
            if subtitle:
                content.append({
                    "component": "VCardSubtitle",
                    "props": {"class": "text-caption text-medium-emphasis pt-0"},
                    "text": subtitle,
                })
            content.append({
                "component": "VCardText",
                "props": {"class": "pt-1 px-3 pb-3"},
                "content": [{"component": "VRow", "props": {"dense": True}, "content": fields}],
            })
            return {"component": "VCard", "props": {"variant": "outlined", "class": "mb-3"},
                    "content": content}

        def tab(value: str, text: str) -> dict:
            """生成一个选项卡标签。"""
            return {"component": "VTab", "props": {"value": value}, "text": text}

        def panel(value: str, *blocks: Any) -> dict:
            """生成一个选项卡面板。

            :param value: 选项卡值
            :param blocks: 卡片或卡片列表
            """
            flat: list[dict] = []
            for block in blocks:
                if isinstance(block, (list, tuple)):
                    flat.extend(block)
                else:
                    flat.append(block)
            return {"component": "VWindowItem", "props": {"value": value}, "content": flat}

        basic = card("基础", [
            field("enabled", "启用插件", kind="switch", cols=4,
                  hint="关闭后不参与搜索，MoviePilot 回到原生 BT/PT"),
            field("downloader_name", "下载器名称", cols=4,
                  hint="下载页里显示的标签名"),
            field("download_root", "下载根目录", cols=4, placeholder="/downloads"),
            field("max_tasks", "并发下载任务数", kind="number", cols=4),
            field("only_whitelist", "仅白名单关键词生效", kind="switch", cols=4),
            field("expand_in_search", "搜索结果按集展开", kind="switch", cols=4,
                  hint="开启后每个集数单独一条，可直接点某一集下载"),
            field("expand_max_files", "单次最多展开集数", kind="number", cols=4,
                  hint="0 表示不限制（完整展开）"),
            field("results_per_page", "每页资源条数", kind="number", cols=4,
                  hint="0 = 不分页（推荐）；设成 50 则按 MP 的 page 参数分页"),
            field("keyword_whitelist", "关键词白名单", cols=4,
                  placeholder="多个关键词用英文逗号分隔"),
        ])

        pansou = card("盘搜", [
            field("pansou_url", "服务地址", cols=6, placeholder="http://pansou:8888"),
            field("pansou_src", "搜索来源", kind="select", cols=6, items=[
                {"title": "全部来源", "value": "all"},
                {"title": "仅 TG 频道", "value": "tg"},
                {"title": "仅插件源", "value": "plugin"},
            ]),
            field("pansou_user", "账号", cols=6, hint="开启鉴权时填写，插件自动登录换令牌"),
            field("pansou_password", "密码", kind="password", cols=6),
            field("pansou_token", "访问令牌（可选）", kind="password", cols=6),
            field("pansou_plugins", "指定插件", cols=6,
                  placeholder="逗号分隔，留空表示全部"),
            field("pansou_cloud_types", "网盘类型", cols=12, placeholder="quark,115"),
        ], subtitle="关键词搜索源，兜底全量资源")

        tg_switch = card("监控开关", [
            field("tg_enabled", "启用频道监控", kind="switch", cols=12,
                  hint="按频道游标增量拉取，新消息进本地索引，用于追更"),
        ], subtitle="关闭后仅保留盘搜，索引不再更新")

        tg_account = card("账号凭据", [
            field("tg_api_id", "api_id", cols=6, placeholder="例如 12345678"),
            field("tg_api_hash", "api_hash", kind="password", cols=6),
            field("tg_session", "StringSession", kind="password", cols=12,
                  hint="与 api_id/api_hash 配套；不会展示在日志里"),
        ], subtitle="Telethon 用户会话才能读私有频道")

        tg_channels_card = card("监控频道", [
            field("tg_channels", "频道列表", kind="textarea", cols=12,
                  placeholder="@channel\nhttps://t.me/xxxxx\nhttps://t.me/+invite",
                  hint="每行一个，支持 @name、t.me 链接、邀请链接；竖线/逗号分隔也能识别"),
            field("tg_interval_minutes", "轮询间隔（分钟）", kind="number", cols=4),
            field("tg_scan_limit", "单次扫描条数", kind="number", cols=4),
            field("tg_backfill", "首次接入回补历史", kind="switch", cols=4,
                  hint="把最近消息也建入索引"),
        ], subtitle="每个频道各自维护游标，互不影响")

        auto_pick = card("自动追更", [
            field("auto_pick_enabled", "TG 命中即处理", kind="switch", cols=4,
                  hint="新消息匹配启用订阅后，按媒体库缺集立即提交下载"),
            field("auto_pick_dry_run", "仅记录不下载", kind="switch", cols=4,
                  hint="建议先观察一段时间决策记录再关闭"),
            field("auto_pick_min_similarity", "标题相似度阈值", kind="number", cols=4,
                  hint="0-1，默认 0.75；越高越严格"),
            field("auto_pick_downloader", "下载器名称", cols=6,
                  hint="默认网盘直链；需与 MoviePilot 中的下载器名称一致"),
        ], subtitle="不依赖订阅总集数，按媒体库已有集计算缺集")

        tg = [tg_switch, tg_account, tg_channels_card, auto_pick]

        download = card("下载", [
            field("aria2_enabled", "启用 aria2", kind="switch", cols=4,
                  hint="使用容器内置 aria2c 多线程下载"),
            field("aria2_connections", "连接数", kind="number", cols=4),
            field("aria2_split", "分片数", kind="number", cols=4),
            field("keep_saved", "保留网盘副本", kind="switch", cols=12,
                  hint="关闭时下载成功后清理转存副本，失败时保留"),
        ], subtitle="aria2 不可用时自动回落到内置分片下载")

        advanced = card("高级", [
            field("mp_base_url", "MoviePilot 内部地址", cols=6,
                  placeholder="http://127.0.0.1:3000",
                  hint="插件生成合成种子 URL 用，MoviePilot 会自己来拉"),
        ], subtitle="一般不需要修改")

        # 移动端：选项卡可横向滚动（show-arrows），标签保持 2-3 字避免换行
        tabs = {
            "component": "VTabs",
            "props": {
                "model": "active_tab",
                "density": "compact",
                "show-arrows": True,
                "align-tabs": "start",
                "grow": False,
                "class": "mb-2",
            },
            "content": [
                tab("basic", "基础"), tab("pansou", "盘搜"), tab("tg", "频道"),
                tab("download", "下载"), tab("advanced", "高级"),
            ],
        }
        window = {"component": "VWindow", "props": {"model": "active_tab", "eager": True},
                  "content": [
                      panel("basic", basic), panel("pansou", pansou), panel("tg", tg),
                      panel("download", download), panel("advanced", advanced),
                  ]}
        defaults = {
            "active_tab": "basic",
            "enabled": False,
            "downloader_name": "网盘直链",
            "download_root": "/downloads",
            "max_tasks": 3,
            "only_whitelist": False,
            "expand_in_search": True,
            "expand_max_files": 0,
            "results_per_page": 0,
            "keyword_whitelist": "",
            "pansou_url": "",
            "pansou_src": "all",
            "pansou_user": "",
            "pansou_password": "",
            "pansou_token": "",
            "pansou_plugins": "",
            "pansou_cloud_types": "quark",
            "tg_enabled": False,
            "tg_api_id": "",
            "tg_api_hash": "",
            "tg_session": "",
            "tg_channels": "",
            "tg_interval_minutes": 10,
            "tg_scan_limit": 100,
            "tg_backfill": False,
            "auto_pick_enabled": False,
            "auto_pick_dry_run": True,
            "auto_pick_min_similarity": 0.75,
            "auto_pick_downloader": "网盘直链",
            "aria2_enabled": True,
            "aria2_connections": 64,
            "aria2_split": 64,
            "keep_saved": False,
            "rename_on_download": True,
            "mp_base_url": "http://127.0.0.1:3000",
        }
        return [{"component": "VForm", "content": [tabs, window]}], defaults

    def get_render_mode(self) -> tuple[str, str]:
        """声明 Vue 远端页面入口；前端资源未构建时仍保留旧配置页。"""
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> list[dict]:
        """在系统侧栏注册渠道管理入口。"""
        return [
            {
                "nav_key": "channels",
                "title": "渠道管理",
                "icon": "mdi-rss",
                "section": "system",
                "permission": "manage",
                "order": 6,
            },
        ]

    def get_page(self) -> Optional[list[dict]]:
        """返回插件详情页；插件尚未初始化时按空任务展示。"""
        manager = self._manager
        tasks = manager.tasks() if manager else []
        running = len([item for item in tasks if item.state == STATE_DOWNLOADING])
        completed = len([item for item in tasks if item.state == STATE_COMPLETED])
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        f"任务 {len(tasks)} 个（下载中 {running}，已完成 {completed}）；"
                        f"盘搜：{'已配置' if self._pansou and self._pansou.ready else '未配置'}；"
                        f"下载根目录：{self._download_root()}"
                    ),
                },
            }
        ]

    # ---------- 模块能力 ----------

    def get_module(self) -> dict:
        """按模块合同提供能力。"""
        return {
            "search_torrents": self.search_torrents,
            "download": self.download,
            "list_torrents": self.list_torrents,
            "torrent_files": self.torrent_files,
            "start_torrents": self.start_torrents,
            "stop_torrents": self.stop_torrents,
            "remove_torrents": self.remove_torrents,
        }

    def search_torrents(self, site: Optional[dict] = None, keyword: Optional[str] = None,
                        mtype: Optional[MediaType] = None, cat: Optional[str] = None,
                        page: int = 0) -> list[TorrentInfo]:
        """优先查 Telegram 索引，再合并 pansou；搜索阶段不再同步取快照。"""
        started = time.time()
        if not self._enabled or not keyword:
            return []
        try:
            page = max(0, int(page or 0))
        except (TypeError, ValueError):
            page = 0
        if not self._keyword_allowed(keyword):
            logger.debug(f"直链频道跳过关键词：{keyword}")
            return []
        limit = max(1, min(int(self._config.get("max_results") or 3), 10))
        channels = self._channels.list() if self._channels else []
        if channels:
            candidates = self._collect_channel_candidates(channels, keyword, limit)
        else:
            candidates = self._collect_legacy_candidates(keyword, limit)
        # 首轮搜索：对前两个候选同步取一次快照，保证「按集展开」立刻可用，
        # 否则要等下一轮预热完成才能选单集。
        missing_inline = [
            item for item in candidates
            if item.get("share_url") and not self._cached_entry(item.get("sid", ""))
        ][:6]
        if missing_inline:
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(max_workers=len(missing_inline))
            futures = [
                pool.submit(
                    self._ensure_entry, item["share_url"],
                    item.get("passcode", ""), item.get("title", ""),
                )
                for item in missing_inline
            ]
            for future in futures:
                try:
                    # 不设超时：宁可搜索慢 1-2 秒，也要把整包拆成单集
                    future.result()
                except Exception as error:  # noqa: BLE001 - 单条失败不影响其它候选
                    logger.debug(f"分享快照失败：{type(error).__name__}")
            pool.shutdown(wait=False)
        cache_key = f"{keyword}|{getattr(mtype, 'value', mtype)}"
        cached = self._search_cache_get(cache_key)
        if cached is not None:
            results = cached
        else:
            results = []
            if bool(self._config.get("expand_in_search", True)):
                results.extend(self._expanded_results(candidates, mtype, base_title=keyword))
            else:
                for candidate in candidates:
                    results.extend(self._results_for_candidate(candidate, mtype))
            self._search_cache_put(cache_key, results)
        per_page = self._results_per_page()
        start = page * per_page if per_page else 0
        if per_page:
            paged = results[start:start + per_page]
        else:
            paged = results[start:]
        logger.info(
            f"直链频道为「{keyword}」返回 {len(paged)} 条资源（第 {page + 1} 页，"
            f"共 {len(results)} 条，缓存命中={cached is not None}），"
            f"耗时 {time.time() - started:.2f}s"
        )
        return paged
        # 可分集的单集条目排在整包前面，方便直接选集
        # 稳定排序：单集条目在前，整包在后，保持集号顺序不变
        results.sort(key=lambda item: 0 if "fid=" in (item.enclosure or "") else 1)
        missing = [item for item in candidates if not self._cached_entry(item["sid"])]
        self._submit_prewarm(missing[:PREWARM_COUNT])
        return paged

    def _channel_pansou(self, channel: dict) -> PansouSource:
        """按渠道配置复用 pansou 实例，保留进程内缓存。"""
        config = channel.get("config") or {}
        source_id = str(channel.get("id") or "")
        signature = (
            str(config.get("base_url") or ""),
            str(config.get("token") or ""),
            str(config.get("username") or ""),
            str(config.get("password") or ""),
            str(config.get("src") or "all"),
            tuple(config.get("plugins") or []),
            tuple(config.get("cloud_types") or []),
        )
        cached = self._channel_sources.get(source_id)
        if cached and cached[0] == signature:
            return cached[1]
        source = PansouSource(
            str(config.get("base_url") or ""),
            token=str(config.get("token") or ""),
            username=str(config.get("username") or ""),
            password=str(config.get("password") or ""),
            timeout=max(5, min(int(config.get("timeout") or self._config.get("pansou_timeout") or 20), 120)),
            src=str(config.get("src") or "all"),
            plugins=list(config.get("plugins") or []),
            cloud_types=list(config.get("cloud_types") or []),
            cache_namespace=source_id,
        )
        self._channel_sources[source_id] = (signature, source)
        return source

    def _collect_channel_candidates(self, channels: list[dict], keyword: str,
                                    limit: int) -> list[dict]:
        """按 priority 倒序遍历启用渠道，合并去重并生成候选。"""
        candidates: list[dict] = []
        seen: set[str] = set()
        for channel in channels:
            if len(candidates) >= limit:
                break
            if not channel.get("enabled"):
                continue
            channel_type = str(channel.get("type") or "")
            config = channel.get("config") or {}
            priority = int(channel.get("priority") or 0)
            site_name = str(channel.get("name") or channel_type)
            if channel_type == "pansou":
                source = self._channel_pansou(channel)
                if not source.ready:
                    continue
                try:
                    rows = source.search(keyword, limit=limit * 2)
                except Exception as error:  # noqa: BLE001 - 单渠道失败不影响其它渠道
                    logger.warning(f"盘搜渠道「{site_name}」搜索失败：{type(error).__name__}")
                    continue
                for item in rows:
                    if len(candidates) >= limit:
                        break
                    self._append_candidate(candidates, seen, item, site_name,
                                           priority, priority, False)
            elif channel_type == "tg_channel" and self._tg_index is not None:
                target = str(config.get("channel") or "").strip()
                if not target:
                    continue
                try:
                    rows = self._tg_index.search(
                        keyword, limit=limit, channel=target,
                    )
                except Exception as error:  # noqa: BLE001 - 索引损坏不应阻断其它渠道
                    logger.warning(f"读取 Telegram 渠道「{site_name}」失败：{type(error).__name__}")
                    continue
                for item in rows:
                    if len(candidates) >= limit:
                        break
                    self._append_candidate(candidates, seen, item, site_name,
                                           priority, priority, True)
        return candidates

    def _append_candidate(self, candidates: list[dict], seen: set[str], item: Any,
                          site_name: str, pri_order: int, expand_pri: int,
                          is_tg: bool) -> None:
        """解析单条渠道结果并按 sid 去重。"""
        if not isinstance(item, dict):
            return
        url = str(item.get("share_url") or item.get("url") or "").strip()
        if not url:
            return
        normalized = normalize_share_url(url) or url
        sid = build_sid(normalized)
        if sid in seen:
            return
        seen.add(sid)
        lowered = str(item.get("title") or "").lower()
        if re.search(r"\.(?:epub|txt|pdf|mobi|azw3|docx?|zip|rar|7z)\b", lowered) or re.search(
            r"(?:txt\s*全文|epub|电子书|mobi|azw3|小说|漫画|txt版)", lowered
        ):
            # 电子书/压缩包不是视频资源，直接跳过
            return
        label = str(item.get("channel") or site_name) if is_tg else site_name
        candidate = self._make_candidate(
            sid, normalized, str(item.get("passcode") or ""),
            str(item.get("title") or ""), label,
            pri_order, expand_pri, item.get("size"),
        )
        item_sid = str(item.get("sid") or "").strip()
        if item_sid and item_sid != sid:
            candidate["lookup_sids"] = [item_sid]
        candidates.append(candidate)

    def _collect_legacy_candidates(self, keyword: str, limit: int) -> list[dict]:
        """无渠道数据时沿用旧逻辑：TG 索引优先，pansou 补充。"""
        candidates: list[dict] = []
        seen: set[str] = set()
        if self._tg_index is not None:
            try:
                tg_items = self._tg_index.search(keyword, limit=limit)
            except Exception as error:  # noqa: BLE001 - 索引损坏不应阻断 pansou
                logger.warning(f"读取 Telegram 索引失败：{type(error).__name__}")
                tg_items = []
            for item in tg_items:
                if len(candidates) >= limit:
                    break
                self._append_candidate(
                    candidates, seen, item,
                    str((item or {}).get("channel") or "telegram") if isinstance(item, dict) else "telegram",
                    10, 10, True,
                )
        if self._pansou and self._pansou.ready and len(candidates) < limit:
            for item in self._pansou.search(keyword, limit=limit * 2):
                if len(candidates) >= limit:
                    break
                self._append_candidate(candidates, seen, item, "pansou", 0, 5, False)
        return candidates

    @staticmethod
    def _size_of(value: Any) -> int:
        """把候选大小安全转换为字节数。"""
        try:
            return max(0, int(float(str(value).replace(",", ""))))
        except (TypeError, ValueError):
            return 0

    def _make_candidate(self, sid: str, share_url: str, passcode: str, title: str,
                        site_name: str, pri_order: int, expand_pri: int,
                        size: Any) -> dict:
        """登记一个搜索结果候选，供后续展开与懒加载。"""
        candidate = {
            "sid": sid,
            "share_url": share_url,
            "passcode": passcode,
            "title": title.strip() or share_url,
            "site_name": site_name or "pansou",
            "pri_order": int(pri_order),
            "expand_pri": int(expand_pri),
            "size": self._size_of(size),
        }
        self._remember_pending(candidate)
        return candidate

    def _remember_pending(self, candidate: dict) -> None:
        """记住 sid 对应的分享地址，供 /torrent 无快照时现场取清单。"""
        with self._pending_lock:
            sid = str(candidate.get("sid") or "")
            if not sid:
                return
            self._pending_shares[sid] = {
                "share_url": str(candidate.get("share_url") or ""),
                "passcode": str(candidate.get("passcode") or ""),
                "title": str(candidate.get("title") or ""),
            }
            self._pending_shares.move_to_end(sid)
            while len(self._pending_shares) > PENDING_SHARE_LIMIT:
                self._pending_shares.popitem(last=False)

    def _pending_info(self, sid: str) -> Optional[dict]:
        """读取候选登记信息副本。"""
        with self._pending_lock:
            item = self._pending_shares.get(sid)
            return dict(item) if item else None

    def _cached_entry(self, sid: str) -> Optional[ShareEntry]:
        """安全读取已有快照。"""
        if not self._cache:
            return None
        try:
            return self._cache.get(sid)
        except Exception as error:  # noqa: BLE001 - 缓存损坏按未命中处理
            logger.warning(f"读取分享缓存失败：{type(error).__name__}")
            return None

    def _results_for_candidate(self, candidate: dict,
                               mtype: Optional[MediaType]) -> list[TorrentInfo]:
        """按配置返回一条整包候选，或保留旧的按集展开行为。"""
        entry = self._candidate_entry(candidate)
        if not bool(self._config.get("expand_in_search", False)):
            return [self._whole_candidate(candidate, entry, mtype)]
        if entry:
            expanded = self._expand_candidate(candidate, entry, mtype)
            if expanded:
                return expanded
        return [self._whole_candidate(candidate, entry, mtype)]

    def _candidate_entry(self, candidate: dict) -> Optional[ShareEntry]:
        """读取候选对应快照，并兼容渠道原始 sid 的二次查表。"""
        entry = self._cached_entry(str(candidate.get("sid") or ""))
        if entry:
            return entry
        for alt_sid in candidate.get("lookup_sids") or []:
            entry = self._cached_entry(str(alt_sid))
            if entry:
                candidate["sid"] = entry.sid
                self._remember_pending(candidate)
                return entry
        return None

    def _expand_candidate(self, candidate: dict, entry: ShareEntry,
                          mtype: Optional[MediaType]) -> list[TorrentInfo]:
        """按集号展开快照；无集号或同集重复时按规则收敛。"""
        by_episode: dict[int, tuple[int, dict]] = {}
        for item in entry.files:
            if not isinstance(item, dict) or item.get("dir"):
                continue
            size = self._size_of(item.get("size"))
            fid = str(item.get("fid") or "").strip()
            if size <= 0 or not fid:
                continue
            file_name = str(item.get("name") or "")
            if not file_name.lower().endswith(VIDEO_SUFFIXES):
                continue
            episode = self._episode_of(file_name)
            if episode is None:
                continue
            existing = by_episode.get(episode)
            if existing is None or size > existing[0]:
                by_episode[episode] = (size, item)
        if not by_episode:
            return []
        share_title = self._clean_title(
            str(candidate.get("title") or entry.title or entry.folder_name)
        )
        results: list[TorrentInfo] = []
        limit = self._expand_limit()
        for episode, (size, item) in sorted(by_episode.items())[:limit or None]:
            file_name = str(item.get("name") or "file")
            title = f"{share_title} {self._episode_label(file_name, episode)}".strip()
            results.append(self._make_torrent(
                candidate, title, size, int(candidate.get("expand_pri") or 5),
                str(item.get("fid") or ""), mtype,
                f"第{episode}集 · {file_name}",
            ))
        return results

    def _expanded_results(self, candidates: list[dict],
                           mtype: Optional[MediaType],
                           base_title: str = "") -> list[TorrentInfo]:
        """按分享分组输出：先整包，紧跟该分享胜出的单集。

        - 整包与它下面的单集一定来自同一个分享，避免"整包已过期、单集却能下"的错觉；
        - 同一集号在多个分享里出现时，只保留体积最大的版本，并归到那个分享的分组里。
        """
        per_candidate: list[Tuple[dict, Optional[ShareEntry], dict[int, tuple[int, dict]]]] = []
        best: dict[int, tuple[int, dict, dict]] = {}
        for candidate in candidates:
            entry = self._candidate_entry(candidate)
            if not entry:
                # 拿不到文件清单（预热失败/分享失效）：保留整包候选，
                # 下载时会现场再取一次；不展开单集，避免整包与单集错配。
                per_candidate.append((candidate, None, {}))
                continue
            by_episode: dict[int, tuple[int, dict]] = {}
            for item in entry.files:
                if not isinstance(item, dict) or item.get("dir"):
                    continue
                size = self._size_of(item.get("size"))
                fid = str(item.get("fid") or "").strip()
                if size <= 0 or not fid:
                    continue
                file_name = str(item.get("name") or "")
                if not file_name.lower().endswith(VIDEO_SUFFIXES):
                    continue
                episode = self._episode_of(file_name)
                if episode is None:
                    continue
                existing = by_episode.get(episode)
                if existing is None or size > existing[0]:
                    by_episode[episode] = (size, item)
            per_candidate.append((candidate, entry, by_episode))
            for episode, (size, item) in by_episode.items():
                current = best.get(episode)
                if current is None or size > current[0]:
                    best[episode] = (size, item, candidate)

        limit = self._expand_limit()
        results: list[TorrentInfo] = []
        shown = 0
        for candidate, entry, by_episode in per_candidate:
            # 有单集可展开时整包与单集同优先级；纯整包沿用渠道优先级
            pack_pri = (
                int(candidate.get("expand_pri") or candidate.get("pri_order") or 0)
                if by_episode
                else int(candidate.get("pri_order") or 0)
            )
            results.append(self._whole_candidate(
                candidate, entry, mtype, pri_order=pack_pri,
            ))
            if limit and shown >= limit:
                continue
            for episode in sorted(by_episode):
                if limit and shown >= limit:
                    break
                owner = best.get(episode)
                if owner is None or owner[2] is not candidate:
                    # 该集在别的分享里有更大的版本，交给那个分享输出
                    continue
                size, item = by_episode[episode]
                file_name = str(item.get("name") or "file")
                share_title = str(candidate.get("title") or "").strip()
                display = (str(base_title).strip() or self._clean_title(share_title))[:80]
                title = f"{display} {self._episode_label(file_name, episode)}".strip()
                results.append(self._make_torrent(
                    candidate, title, size, int(candidate.get("expand_pri") or 5),
                    str(item.get("fid") or ""), mtype, f"第{episode}集 · {file_name}",
                ))
                shown += 1
        return results

    def _results_per_page(self) -> int:
        """每页返回多少条资源；0 表示不分页（全部返回）。"""
        try:
            return max(0, int(self._config.get("results_per_page", 0)))
        except (TypeError, ValueError):
            return 0

    def _search_cache_get(self, key: str) -> Optional[list]:
        """读取搜索结果缓存（10 分钟有效）。"""
        with self._pending_lock:
            item = self._search_cache.get(key)
            if not item:
                return None
            stamp, rows = item
            if time.time() - stamp > 600:
                self._search_cache.pop(key, None)
                return None
            return rows

    def _search_cache_put(self, key: str, rows: list) -> None:
        """写入搜索结果缓存，最多保留 20 个关键词。"""
        with self._pending_lock:
            self._search_cache[key] = (time.time(), rows)
            while len(self._search_cache) > 20:
                self._search_cache.pop(next(iter(self._search_cache)))

    def _expand_limit(self) -> int:
        """单次搜索最多展开多少集；0 表示不限制（默认完整展开）。"""
        try:
            return max(0, int(self._config.get("expand_max_files") or 0))
        except (TypeError, ValueError):
            return 0

    def _whole_candidate(self, candidate: dict, entry: Optional[ShareEntry],
                         mtype: Optional[MediaType],
                         pri_order: Optional[int] = None) -> TorrentInfo:
        """构造整包候选；有快照时使用总大小与文件数。"""
        size = self._size_of(candidate.get("size"))
        title = str(candidate.get("title") or "")
        detail = "整包（未取到文件清单，点下载会下全部）"
        if entry:
            size = int(entry.total_size or size)
            title = title or entry.title
            detail = f"整包 · {len(entry.files)} 个文件 · 可在插件内选集"
        return self._make_torrent(
            candidate, title, size,
            int(pri_order if pri_order is not None else (candidate.get("pri_order") or 0)),
            "", mtype, detail,
        )

    def _make_torrent(self, candidate: dict, title: str, size: int, pri_order: int,
                      fid: str, mtype: Optional[MediaType], detail: str) -> TorrentInfo:
        """把候选包装成 MoviePilot 种子信息。"""
        return TorrentInfo(
            site=None,
            site_name=str(candidate.get("site_name") or "pansou"),
            title=str(title or candidate.get("share_url") or ""),
            description=f"网盘直链 · {detail}",
            enclosure=self._torrent_url(str(candidate.get("sid") or ""), fid),
            page_url=str(candidate.get("share_url") or ""),
            size=float(max(0, int(size))),
            seeders=0,
            pubdate=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pri_order=int(pri_order),
            category=mtype.value if isinstance(mtype, MediaType) else None,
        )

    @staticmethod
    def _episode_label(name: str, episode: int) -> str:
        """生成 SxxExx 标签；缺失季号时默认 S01。"""
        match = SEASON_EPISODE_RE.search(name or "")
        season = int(match.group(1)) if match else 1
        return f"S{season:02d}E{int(episode):02d}"

    def _submit_prewarm(self, candidates: list[dict]) -> None:
        """把最多两个候选提交给单线程后台队列预热快照。"""
        if not candidates or not self._cache or not hasattr(self._cache, "put"):
            return
        ensure_entry = self._ensure_entry
        pending: list[dict] = []
        with self._prewarm_lock:
            for candidate in candidates:
                sid = str(candidate.get("sid") or "")
                if not sid or sid in self._prewarm_inflight:
                    continue
                self._prewarm_inflight.add(sid)
                pending.append(candidate)
            if not pending:
                return
            if self._prewarm_pool is None:
                self._prewarm_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="quarkchannel-prewarm",
                )
            for candidate in pending:
                try:
                    self._prewarm_pool.submit(self._prewarm_one, candidate, ensure_entry)
                except RuntimeError:
                    self._prewarm_inflight.discard(str(candidate.get("sid") or ""))

    def _prewarm_one(self, candidate: dict, ensure_entry: Any) -> None:
        """后台预热单个候选；慢一点启动，避免干扰搜索返回路径。"""
        sid = str(candidate.get("sid") or "")
        try:
            time.sleep(PREWARM_DELAY_SECONDS)
            with self._prewarm_lock:
                if self._prewarm_pool is None:
                    return
            if self._cached_entry(sid):
                return
            ensure_entry(
                str(candidate.get("share_url") or ""),
                str(candidate.get("passcode") or ""),
                str(candidate.get("title") or ""),
            )
        except Exception as error:  # noqa: BLE001 - 预热失败忽略
            logger.debug(f"后台预热失败：{sid}，{type(error).__name__}")
        finally:
            with self._prewarm_lock:
                self._prewarm_inflight.discard(sid)

    def download(self, content: Union[Path, str, bytes], download_dir: Path,
                 cookie: Optional[str] = None, episodes: Optional[set] = None,
                 category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None,
                 custom_words: Optional[str] = None):
        """接管本插件合成种子的下载。"""
        if not self._enabled or not self._manager:
            return None
        marker = parse_marker(content if isinstance(content, bytes) else b"")
        if not marker:
            return None
        sid, _provider = marker
        entry = self._cache.get(sid) if self._cache else None
        if not entry:
            return None, None, None, "分享快照已过期，请重新搜索"
        selected = self._select_files(entry, episodes)
        if not selected:
            return None, None, None, "没有匹配到需要下载的文件"
        task_hash = sha1_of_torrent(content) if isinstance(content, bytes) else ""
        if not task_hash:
            task_hash = hashlib.md5(str(content).encode("utf-8")).hexdigest()
        save_dir = self._resolve_download_dir(download_dir)
        if save_dir is None:
            return None, None, None, (
                "MoviePilot 的下载目录配置在远程存储上，直链频道只能落到本地目录；"
                "请在下载目录中增加本地目录（如 /downloads）并为该资源指定保存路径"
            )
        task = DownloadTask(
            hash=task_hash,
            sid=sid,
            title=entry.title,
            folder=entry.folder_name,
            save_dir=str(save_dir),
            files=[
                TaskFile(
                    name=item.get("name", "file"),
                    size=int(item.get("size") or 0),
                    fid=str(item.get("fid") or ""),
                    share_token=str(item.get("share_token") or ""),
                    out_name=self._build_out_name(entry, item),
                )
                for item in selected
            ],
        )
        existing = self._manager.get(task_hash)
        if existing and existing.state in {STATE_DOWNLOADING, STATE_COMPLETED}:
            logger.info(f"网盘直链任务已存在：{entry.title}（{existing.state}）")
            return self._downloader_name(), task_hash, "", DOWNLOAD_TASK_ALREADY_EXISTS
        submitted = self._manager.submit(task, entry.share_url, entry.passcode)
        if submitted is not task:
            logger.info(f"网盘直链任务已存在：{entry.title}（{submitted.state}）")
            return self._downloader_name(), submitted.hash, "", DOWNLOAD_TASK_ALREADY_EXISTS
        logger.info(f"已提交网盘直链任务：{entry.title}，{len(selected)} 个文件 → {task.save_dir}")
        return self._downloader_name(), task_hash, "", ""

    def list_torrents(self, status: Optional[Union[str, TorrentQueryStatus]] = None,
                      hashs: Optional[Union[list[str], str]] = None,
                      downloader: Optional[str] = None,
                      include_all_tags: bool = False) -> list[DownloaderTorrent]:
        """把本插件任务投影成统一任务列表。"""
        if not self._manager:
            return []
        wanted: Optional[set[str]] = None
        if isinstance(hashs, str):
            wanted = {hashs}
        elif isinstance(hashs, (list, tuple, set)):
            wanted = {str(item) for item in hashs if item}
        query = str(getattr(status, "value", status) or "all").lower()
        if self._manager:
            try:
                self._manager.refresh_live()
            except Exception as error:  # noqa: BLE001 - 刷新失败沿用缓存值
                logger.debug(f"刷新 aria2 实时进度失败：{type(error).__name__}")
        own_name = self._downloader_name()
        if downloader and downloader != own_name:
            # 下载页按标签查询：其他下载器的标签不返回本插件任务，避免重复显示
            return []
        results: list[DownloaderTorrent] = []
        for task in self._manager.tasks():
            if wanted is not None and task.hash not in wanted:
                continue
            # 有 aria2 gid 且未下完 → 一律按下载中上报，避免重载后误显示暂停
            state = (
                "downloading"
                if getattr(task, "gids", None) and (task.progress or 0) < 100
                else self._map_state(task.state)
            )
            if query in {"transfer", "completed"} and state != STATE_COMPLETED:
                continue
            if query in {"downloading"} and state != STATE_DOWNLOADING:
                continue
            if query in {"paused", "stopped"} and state != STATE_STOPPED:
                continue
            results.append(DownloaderTorrent(
                downloader=own_name,
                hash=task.hash,
                title=task.folder,
                name=task.title,
                path=Path(task.content_path),
                save_path=task.save_dir,
                content_path=task.content_path,
                size=float(task.total),
                progress=task.progress,
                state=state,
                dlspeed=f"{task.speed / 1024 / 1024:.1f} MB/s" if task.speed else None,
                tags=None,
                category=None,
            ))
        if query in {"transfer", "completed"}:
            results = [item for item in results if (item.progress or 0) >= 100]
        return results

    def torrent_files(self, tid: Optional[str] = None, downloader: Optional[str] = None,
                      **kwargs: Any) -> list[DownloaderFile]:
        """返回任务文件清单与单文件进度。"""
        if not self._manager or not tid:
            return []
        task = self._manager.get(str(tid))
        if not task:
            return []
        return [
            DownloaderFile(
                id=item.name,
                name=item.rel_path,
                size=item.size,
                priority=0,
                progress=(item.done * 100.0 / item.size) if item.size else 0.0,
            )
            for item in task.files
        ]

    def start_torrents(self, hashs: Optional[Union[list[str], str]] = None,
                       downloader: Optional[str] = None, **kwargs: Any) -> bool:
        """继续已停止的任务。"""
        if not self._manager:
            return False
        changed = False
        for task_hash in self._as_list(hashs):
            changed = self._manager.resume(task_hash) or changed
        return changed

    def stop_torrents(self, hashs: Optional[Union[list[str], str]] = None,
                      downloader: Optional[str] = None, **kwargs: Any) -> bool:
        """暂停任务。"""
        if not self._manager:
            return False
        changed = False
        for task_hash in self._as_list(hashs):
            changed = self._manager.stop(task_hash) or changed
        return changed

    def remove_torrents(self, hashs: Optional[Union[list[str], str]] = None,
                        delete_file: bool = False, downloader: Optional[str] = None,
                        **kwargs: Any) -> bool:
        """删除任务记录，可选清理文件。"""
        if not self._manager:
            return False
        return self._manager.remove(self._as_list(hashs), delete_file=bool(delete_file))

    # ---------- 插件接口实现 ----------

    def api_torrent(self, sid: str = "", sig: str = "", fid: str = "") -> Response:
        """按分享 id 输出整包或单文件合成种子，缺失快照时现场补齐。"""
        if not verify(sid, sig, self._secret):
            return Response(content=b"forbidden", status_code=403)
        entry = self._cache.get(sid) if self._cache else None
        if not entry:
            entry = self._lazy_snapshot(sid)
        if not entry:
            return Response(content=b"not found", status_code=404)
        try:
            payload = render_torrent(entry, fid=fid or None)
        except ValueError as error:
            logger.warning(f"生成种子失败：{error}")
            return Response(content=b"invalid share", status_code=404)
        return Response(
            content=payload,
            media_type="application/x-bittorrent",
            headers={"Content-Disposition": f'attachment; filename="{sid}.torrent"'},
        )

    def _lazy_snapshot(self, sid: str) -> Optional[ShareEntry]:
        """按候选登记信息现场取快照，整体最多等待 8 秒。"""
        pending = self._pending_info(sid)
        if not pending or not pending.get("share_url"):
            return None
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="quarkchannel-snapshot",
        )
        try:
            future = executor.submit(
                self._ensure_entry,
                str(pending.get("share_url") or ""),
                str(pending.get("passcode") or ""),
                str(pending.get("title") or ""),
            )
            return future.result(timeout=SNAPSHOT_TIMEOUT_SECONDS)
        except FuturesTimeoutError:
            logger.warning(f"现场读取分享超时：{sid}")
            return None
        except Exception as error:  # noqa: BLE001 - 取快照失败按 404 处理
            logger.warning(f"现场读取分享失败：{sid}，{type(error).__name__}")
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def api_share(self, payload: Optional[dict] = None) -> dict:
        """登记一条分享，返回种子地址（用于手工验证链路）。"""
        payload = payload or {}
        share_url = str(payload.get("share_url") or "").strip()
        passcode = str(payload.get("passcode") or "").strip()
        if not share_url:
            return {"success": False, "message": "缺少 share_url"}
        entry = self._ensure_entry(share_url, passcode, "", force=True)
        if not entry:
            return {"success": False, "message": "分享不可用或没有文件"}
        return {
            "success": True,
            "data": {
                "sid": entry.sid,
                "title": entry.title,
                "files": len(entry.files),
                "size": entry.total_size,
                "torrent": self._torrent_url(entry.sid),
            },
        }

    def _channel_store(self) -> ChannelStore:
        """返回渠道存储；未初始化时按当前数据目录惰性创建。"""
        if self._channels is None:
            self._channels = ChannelStore(self.get_data_path())
        return self._channels

    def api_channels(self) -> dict:
        """列出全部渠道（含配置与统计）。"""
        return {"success": True, "data": {"items": self._channel_store().list()}}

    def api_channel_create(self, payload: Optional[dict] = None) -> dict:
        """新增渠道；仅返回新渠道 id。"""
        try:
            item = self._channel_store().create(dict(payload or {}))
        except ValueError as error:
            return {"success": False, "message": str(error)}
        self._refresh_channel_runtime()
        return {"success": True, "data": {"id": item["id"]}}

    def api_channel_update(self, channel_id: str = "",
                           payload: Optional[dict] = None) -> dict:
        """局部更新渠道字段。"""
        try:
            item = self._channel_store().update(str(channel_id or ""), dict(payload or {}))
        except ValueError as error:
            return {"success": False, "message": str(error)}
        if not item:
            return {"success": False, "message": "渠道不存在"}
        self._refresh_channel_runtime()
        return {"success": True, "data": item}

    def api_channel_delete(self, channel_id: str = "") -> dict:
        """删除渠道；删除后立即刷新运行时。"""
        if not self._channel_store().delete(str(channel_id or "")):
            return {"success": False, "message": "渠道不存在"}
        self._refresh_channel_runtime()
        return {"success": True}

    def api_channel_test(self, channel_id: str = "") -> dict:
        """探测渠道可用性：pansou 走鉴权，TG 走 get_entity。"""
        channel = self._channel_store().get(str(channel_id or ""))
        if not channel:
            return {"success": False, "message": "渠道不存在"}
        if channel.get("type") == "pansou":
            try:
                source = self._channel_pansou(channel)
                ok, reason = source.check_auth()
            except Exception as error:  # noqa: BLE001 - 探活失败只返回结果
                return {"success": False, "message": f"探测失败：{type(error).__name__}"}
            if ok:
                return {"success": True, "message": "盘搜渠道可用"}
            return {"success": False, "message": reason or "盘搜渠道不可用"}
        config = channel.get("config") or {}
        target = str(config.get("channel") or "").strip()
        credentials = self._tg_credentials(self._config)
        if not credentials:
            return {"success": False, "message": "Telegram 凭据不完整"}
        if not target:
            return {"success": False, "message": "渠道缺少频道标识"}
        api_id, api_hash, session = credentials
        data_dir = self.get_data_path()

        async def probe() -> tuple[bool, str]:
            monitor = TgMonitor(
                data_dir, api_id, api_hash, session, [target],
                index=self._shared_tg_index(data_dir),
            )
            client = await monitor._ensure_client()
            if client is None:
                return False, "Telegram 客户端不可用"
            try:
                entity = await client.get_entity(target)
                title = str(getattr(entity, "title", "") or target)
                return True, f"频道可用：{title}"
            except Exception as error:  # noqa: BLE001 - 单次探测失败
                return False, f"频道探测失败：{type(error).__name__}"
            finally:
                await monitor._disconnect()

        try:
            ok, message = asyncio.run(probe())
        except Exception as error:  # noqa: BLE001 - 探测异常不拖垮接口
            ok, message = False, f"频道探测失败：{type(error).__name__}"
        return {"success": ok, "message": message}

    def api_channel_scan(self, channel_id: str = "") -> dict:
        """立即扫描一个 TG 渠道一次，返回新增条目数。"""
        store = self._channel_store()
        channel = store.get(str(channel_id or ""))
        if not channel:
            return {"success": False, "message": "渠道不存在"}
        if channel.get("type") != "tg_channel":
            return {"success": False, "message": "pansou 渠道不支持扫描"}
        config = channel.get("config") or {}
        target = str(config.get("channel") or "").strip()
        credentials = self._tg_credentials(self._config)
        if not credentials:
            return {"success": False, "message": "Telegram 凭据不完整"}
        if not target:
            return {"success": False, "message": "渠道缺少频道标识"}
        api_id, api_hash, session = credentials
        data_dir = self.get_data_path()
        index = self._shared_tg_index(data_dir)

        async def scan() -> list[dict]:
            monitor = TgMonitor(
                data_dir, api_id, api_hash, session, [target],
                scan_limit=int(config.get("scan_limit") or 100),
                interval_minutes=int(config.get("interval_minutes") or 10),
                backfill=bool(config.get("backfill")), index=index,
            )
            try:
                return await monitor.poll_once()
            finally:
                await monitor._disconnect()

        try:
            added = asyncio.run(scan())
        except Exception as error:  # noqa: BLE001 - 扫描失败记录到统计
            store.record_stat(channel["id"], error=type(error).__name__)
            return {"success": False, "message": f"扫描失败：{type(error).__name__}"}
        store.record_stat(channel["id"], hits=len(added))
        return {
            "success": True,
            "message": f"扫描完成，新增 {len(added)} 条",
            "data": {"added": len(added)},
        }

    def api_channels_summary(self) -> dict:
        """返回渠道总数、启用数、TG 索引条目与最近轮询时间。"""
        items = self._channel_store().list()
        index = self._tg_index
        return {
            "success": True,
            "data": {
                "total": len(items),
                "enabled": len([item for item in items if item.get("enabled")]),
                "tg_entries": len(getattr(index, "entries", []) or []) if index is not None else 0,
                "last_poll": float(getattr(index, "last_poll", 0.0) or 0.0) if index is not None else 0.0,
            },
        }

    def api_search_test(self, payload: Optional[dict] = None) -> dict:
        """按关键词跑一次 search_torrents，返回标题/大小/种子地址（排障用）。

        :param payload: {"keyword": "...", "limit": 5}
        :return: 条目列表
        """
        payload = payload or {}
        keyword = str(payload.get("keyword") or "").strip()
        if not keyword:
            return {"success": False, "message": "缺少 keyword"}
        items = self.search_torrents(keyword=keyword, page=0)
        rows = [
            {
                "title": item.title,
                "size": item.size,
                "site": item.site_name,
                "enclosure": (item.enclosure or "")[:120],
            }
            for item in items[: int(payload.get("limit") or 20)]
        ]
        return {"success": True, "data": {"keyword": keyword, "count": len(items), "items": rows}}

    def _entry_for_sid(self, sid: str) -> Optional[ShareEntry]:
        """读取分享快照；缓存未命中时按候选登记信息现场取一次。"""
        entry = self._cached_entry(str(sid or ""))
        if entry:
            return entry
        pending = self._pending_info(str(sid or ""))
        if not pending or not pending.get("share_url"):
            return None
        return self._ensure_entry(
            str(pending.get("share_url") or ""),
            str(pending.get("passcode") or ""),
            str(pending.get("title") or ""),
        )

    def _episode_items(self, entry: ShareEntry) -> list[dict]:
        """把快照文件转换为接口条目，并按集号升序、其他文件置后。"""
        items: list[dict] = []
        for item in entry.files:
            if not isinstance(item, dict) or item.get("dir"):
                continue
            name = str(item.get("name") or "file")
            items.append({
                "episode": self._episode_of(name),
                "fid": str(item.get("fid") or ""),
                "name": name,
                "size": self._size_of(item.get("size")),
            })
        items.sort(key=lambda row: (
            row["episode"] is None,
            row["episode"] if row["episode"] is not None else 0,
            row["name"].lower(),
        ))
        return items

    def api_shares(self, keyword: str = "", limit: int = 20) -> dict:
        """按关键词列出分享，不展开集列表。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": True, "data": {"items": []}}
        try:
            count = int(limit or 20)
        except (TypeError, ValueError):
            count = 20
        count = max(1, min(count, 100))
        channels = self._channels.list() if self._channels else []
        if channels:
            candidates = self._collect_channel_candidates(channels, keyword, count)
        else:
            candidates = self._collect_legacy_candidates(keyword, count)
        items: list[dict] = []
        for candidate in candidates:
            entry = self._candidate_entry(candidate)
            sid = str(candidate.get("sid") or (entry.sid if entry else ""))
            title = str(candidate.get("title") or (entry.title if entry else ""))
            files = len([
                item for item in (entry.files if entry else [])
                if isinstance(item, dict) and not item.get("dir")
            ])
            size = int(entry.total_size or 0) if entry else self._size_of(candidate.get("size"))
            items.append({
                "sid": sid,
                "title": title,
                "size": size,
                "files": files,
                "site_name": str(candidate.get("site_name") or ""),
                "share_url": str(candidate.get("share_url") or ""),
            })
        return {"success": True, "data": {"items": items}}

    def api_share_episodes(self, sid: str = "") -> dict:
        """列出分享内文件与集号；无集号的文件排在最后。"""
        sid = str(sid or "").strip()
        if not sid:
            return {"success": False, "message": "缺少 sid"}
        entry = self._entry_for_sid(sid)
        if not entry:
            return {"success": False, "message": "分享不可用或没有文件"}
        items = self._episode_items(entry)
        return {"success": True, "data": {"items": items, "total": len(items)}}

    def _batch_task_hash(self, entry: ShareEntry, selected: list[dict],
                         whole: bool) -> str:
        """为一批文件生成稳定 hash，供下载管理器幂等复用。"""
        if whole:
            return sha1_of_torrent(render_torrent(entry))
        if len(selected) == 1 and str(selected[0].get("fid") or ""):
            return sha1_of_torrent(render_torrent(entry, fid=str(selected[0]["fid"])))
        keys = sorted(
            str(item.get("fid") or item.get("name") or "") for item in selected
        )
        return hashlib.sha1(
            f"{entry.sid}|{','.join(keys)}".encode("utf-8")
        ).hexdigest()

    def api_download_episodes(self, payload: Optional[dict] = None) -> dict:
        """按集号或文件 id 提交一批文件；两者都为空时下载整包。"""
        payload = payload or {}
        sid = str(payload.get("sid") or "").strip()
        raw_episodes = payload.get("episodes") or []
        raw_fids = payload.get("fids") or []
        if not isinstance(raw_episodes, (list, tuple, set)):
            raw_episodes = [raw_episodes]
        if not isinstance(raw_fids, (list, tuple, set)):
            raw_fids = [raw_fids]
        if not sid:
            return {"success": False, "message": "缺少 sid"}
        if raw_episodes and raw_fids:
            return {"success": False, "message": "episodes 与 fids 只能二选一"}
        if not self._manager:
            return {"success": False, "message": "插件未初始化"}
        entry = self._entry_for_sid(sid)
        if not entry:
            return {"success": False, "message": "分享不可用或没有文件"}
        files = [
            item for item in entry.files
            if isinstance(item, dict) and not item.get("dir")
            and self._size_of(item.get("size")) > 0
        ]
        whole = not raw_episodes and not raw_fids
        selected: list[dict] = []
        if raw_fids:
            wanted = []
            for value in raw_fids:
                fid = str(value or "").strip()
                if fid and fid not in wanted:
                    wanted.append(fid)
            by_fid = {str(item.get("fid") or ""): item for item in files}
            selected = [by_fid[fid] for fid in wanted if fid in by_fid]
        elif raw_episodes:
            wanted = set()
            for value in raw_episodes:
                try:
                    episode = int(value)
                except (TypeError, ValueError):
                    continue
                if episode > 0:
                    wanted.add(episode)
            if not wanted:
                return {"success": False, "message": "episodes 格式无效"}
            selected = self._select_files(entry, wanted)
            selected = [
                item for item in selected
                if self._size_of(item.get("size")) > 0
            ]
        else:
            selected = files
        if not selected:
            return {"success": False, "message": "没有匹配到需要下载的文件"}
        task_hash = self._batch_task_hash(entry, selected, whole)
        existing = self._manager.get(task_hash)
        if existing and existing.state in {STATE_DOWNLOADING, STATE_COMPLETED}:
            return {
                "success": True,
                "data": {
                    "hash": task_hash,
                    "count": len(selected),
                    "size": sum(self._size_of(item.get("size")) for item in selected),
                },
            }
        task = DownloadTask(
            hash=task_hash,
            sid=entry.sid,
            title=entry.title,
            folder=entry.folder_name,
            save_dir=str(self._download_root()),
            files=[
                TaskFile(
                    name=str(item.get("name") or "file"),
                    size=self._size_of(item.get("size")),
                    fid=str(item.get("fid") or ""),
                    share_token=str(item.get("share_token") or ""),
                    out_name=self._build_out_name(entry, item),
                )
                for item in selected
            ],
        )
        try:
            submitted = self._manager.submit(task, entry.share_url, entry.passcode)
        except Exception as error:  # noqa: BLE001 - 提交失败返回明确原因
            logger.warning(f"选集下载提交失败：{type(error).__name__}")
            return {"success": False, "message": f"提交失败：{type(error).__name__}"}
        return {
            "success": True,
            "data": {
                "hash": submitted.hash,
                "count": len(selected),
                "size": sum(self._size_of(item.get("size")) for item in selected),
            },
        }

    def api_auto_pick(self) -> dict:
        """返回自动追更开关与最近决策记录。"""
        picker = self._picker
        picks = picker.recent(50) if picker else []
        return {
            "success": True,
            "data": {
                "enabled": bool(picker.enabled) if picker else False,
                "dry_run": bool(picker.dry_run) if picker else True,
                "processed": picker.store.processed_count() if picker else 0,
                "picks": picks,
                "recent": picks,
            },
        }

    def api_status(self) -> dict:
        """返回运行状态（含盘搜鉴权自检）。"""
        tasks = self._manager.tasks() if self._manager else []
        pansou_ok, pansou_reason = (False, "未初始化")
        if self._pansou:
            pansou_ok, pansou_reason = self._pansou.check_auth()
        tg_index = self._tg_index
        tg_entries = len(getattr(tg_index, "entries", []) or []) if tg_index is not None else 0
        tg_last_poll = float(getattr(tg_index, "last_poll", 0.0) or 0.0) if tg_index is not None else 0.0
        for monitor in self._tg_monitors:
            try:
                tg_last_poll = max(
                    tg_last_poll, float(getattr(monitor, "last_poll", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                pass
        picker = self._picker
        auto_pick_picks = picker.recent(10) if picker else []
        channel_items = self._channels.list() if self._channels else []
        channel_stats = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "last_scan": float((item.get("stats") or {}).get("last_scan") or 0.0),
                "last_error": str((item.get("stats") or {}).get("last_error") or ""),
                "hits": int((item.get("stats") or {}).get("hits") or 0),
            }
            for item in channel_items
        ]
        return {
            "success": True,
            "data": {
                "enabled": self._enabled,
                "pansou": bool(self._pansou and self._pansou.ready),
                "pansou_ok": pansou_ok,
                "pansou_reason": pansou_reason,
                "tg_entries": tg_entries,
                "tg_last_poll": tg_last_poll,
                "auto_pick_enabled": bool(picker.enabled) if picker else False,
                "auto_pick_dry_run": bool(picker.dry_run) if picker else True,
                "auto_pick_picks": auto_pick_picks,
                "expand_in_search": bool(self._config.get("expand_in_search", True)),
                "channels": len(channel_items),
                "channel_stats": channel_stats,
                "channel_last_scan": {
                    item["id"]: item["last_scan"] for item in channel_stats
                },
                "download_root": self._download_root(),
                "tasks": [
                    {
                        "hash": item.hash,
                        "title": item.title,
                        "state": item.state,
                        "progress": item.progress,
                        "error": item.error,
                        "save_path": item.content_path,
                    }
                    for item in tasks
                ],
            },
        }

    def api_linkdiag(self, payload: Optional[dict] = None) -> dict:
        """取链后立刻用 Range 请求探测直链状态（快速排障）。

        :param payload: {"share_url": "...", "passcode": "", "file_index": 0}
        :return: 各步骤结果
        """
        payload = payload or {}
        share_url = str(payload.get("share_url") or "").strip()
        passcode = str(payload.get("passcode") or "").strip()
        index = int(payload.get("file_index") or 0)
        client = self._make_client()
        pwd_id = extract_pwd_id(share_url)
        try:
            token = client.stoken(pwd_id, passcode)
            if not token:
                return {"success": False, "message": "分享不可用"}
            snapshot = client.snapshot(share_url, passcode, token=token)
            files = [item for item in snapshot.files if not item.is_dir]
            if not files:
                return {"success": False, "message": "分享内没有文件"}
            target = files[min(index, len(files) - 1)]
            saved = client.save_files(pwd_id, token, [target])
            if not saved:
                return {"success": False, "message": "转存未返回 fid"}
            url, headers = client.download_url_by_fid(saved[0], name=target.name)
            from app.sdk.network import RequestUtils as _Req
            probe = _Req().get_res(
                url, headers={**headers, "Range": "bytes=0-1023"}, timeout=20,
            )
            status = getattr(probe, "status_code", None)
            return {
                "success": status in (200, 206),
                "file": {"name": target.name, "size": target.size},
                "link": {"status": status, "host": url.split("/")[2]},
                "headers": sorted(headers.keys()),
            }
        except Exception as error:  # noqa: BLE001 - 排障接口返回真实原因
            return {"success": False, "message": f"{type(error).__name__}: {error}"[:300]}
        finally:
            try:
                client.delete_files(saved)  # type: ignore[name-defined]
            except Exception:  # noqa: BLE001 - 清理失败忽略
                pass

    def api_selftest(self, payload: Optional[dict] = None) -> dict:
        """按真实链路自检一条分享：快照 → 转存 → 取链，不下正文。

        :param payload: {"share_url": "...", "passcode": "", "file_index": 0}
        :return: 各步结果
        """
        payload = payload or {}
        share_url = str(payload.get("share_url") or "").strip()
        passcode = str(payload.get("passcode") or "").strip()
        index = int(payload.get("file_index") or 0)
        if not share_url:
            return {"success": False, "message": "缺少 share_url"}
        steps: dict = {}
        client = self._make_client()
        pwd_id = extract_pwd_id(share_url)
        token = client.stoken(pwd_id, passcode)
        steps["stoken"] = bool(token)
        if not token:
            return {"success": False, "steps": steps, "message": "分享不可用或提取码错误"}
        try:
            # 必须复用同一个 stoken：share_fid_token 与 stoken 绑定
            snapshot = client.snapshot(share_url, passcode, token=token)
            steps["snapshot"] = {"files": len(snapshot.files), "size": snapshot.total_size,
                                 "title": snapshot.title}
        except Exception as error:  # noqa: BLE001 - 自检需要返回失败原因
            return {"success": False, "steps": steps, "message": f"读取分享失败：{error}"}
        if not snapshot.files:
            return {"success": False, "steps": steps, "message": "分享内没有文件"}
        target = snapshot.files[min(index, len(snapshot.files) - 1)]
        steps["file"] = {"name": target.name, "size": target.size, "fid": target.fid}
        try:
            saved = client.save_files(pwd_id, token, [target])
            steps["save"] = len(saved)
        except Exception as error:  # noqa: BLE001 - 自检需要返回失败原因
            return {"success": False, "steps": steps, "message": f"转存失败：{error}"}
        if not saved:
            return {"success": False, "steps": steps, "message": "转存未返回 fid"}
        try:
            url, headers = client.download_url_by_fid(saved[0])
            steps["link"] = {"ok": True, "host": url.split("/")[2], "ua": headers.get("User-Agent", "")[:40]}
            result = {"success": True, "steps": steps}
        except Exception as error:  # noqa: BLE001 - 自检需要返回失败原因
            steps["link"] = {"ok": False}
            result = {"success": False, "steps": steps, "message": f"取链失败：{error}"}
        try:
            client.delete_files(saved)
            steps["cleanup"] = True
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            steps["cleanup"] = False
        return result

    def api_probe(self, payload: Optional[dict] = None) -> dict:
        """按关键词跑一遍取链流程，返回候选与文件数（不下载）。

        :param payload: {"keyword": "...", "limit": 3}
        :return: 探测结果
        """
        payload = payload or {}
        keyword = str(payload.get("keyword") or "").strip()
        limit = max(1, min(int(payload.get("limit") or 3), 5))
        if not keyword:
            return {"success": False, "message": "缺少 keyword"}
        if not self._pansou or not self._pansou.ready:
            return {"success": False, "message": "盘搜未配置"}
        hits = self._pansou.search(keyword, limit=limit * 2)
        rows = []
        errors = []
        for item in hits:
            if len(rows) >= limit:
                break
            entry = self._ensure_entry(item["url"], item.get("passcode", ""), item.get("title", ""))
            if not entry:
                errors.append(item["url"])
                continue
            rows.append({
                "title": entry.title,
                "sid": entry.sid,
                "size": entry.total_size,
                "files": len(entry.files),
                "share_url": entry.share_url,
                "torrent": self._torrent_url(entry.sid),
            })
        return {
            "success": bool(rows),
            "data": {"keyword": keyword, "hits": len(hits), "entries": rows, "failed": errors[:5]},
        }

    def api_cleanup(self) -> dict:
        """清理过期分享缓存。"""
        removed = self._cache.purge_expired() if self._cache else 0
        return {"success": True, "message": f"已清理 {removed} 条过期分享"}

    # ---------- 内部工具 ----------

    def _downloader_name(self) -> str:
        """本插件在 MoviePilot 下载器列表中的名称。"""
        return str(self._config.get("downloader_name") or "网盘直链")

    def _ensure_download_dir(self) -> None:
        """确保 MoviePilot 的下载目录里存在本插件使用的本地目录。

        MoviePilot 只允许把文件落到「下载目录」配置里的路径，
        否则会报「保存路径不在允许的下载目录范围内」。
        """
        root = self._download_root()
        name = str(self._config.get("download_dir_name") or "本地直链")
        try:
            current = self.systemconfig.get(SystemConfigKey.Directories) or []
        except Exception as error:  # noqa: BLE001 - 读取失败不阻断插件
            logger.warning(f"读取下载目录配置失败：{error}")
            return
        items = [dict(item) for item in current if isinstance(item, dict)]
        if any(item.get("download_path") == root for item in items):
            return
        items.append({
            "name": name,
            "priority": 9,
            "storage": "local",
            "download_path": root,
            "media_type": "",
            "media_category": None,
            "media_category_id": None,
            "download_type_folder": True,
            "download_category_folder": False,
            "monitor_type": "manual",
            "monitor_mode": "fast",
            "transfer_type": "move",
            "overwrite_mode": "size",
            "library_path": str(Path(root) / "媒体库"),
            "library_storage": "local",
            "renaming": True,
            "scraping": False,
            "notify": True,
            "library_type_folder": True,
            "library_category_folder": False,
        })
        try:
            self.systemconfig.set(SystemConfigKey.Directories, items)
            logger.info(f"已登记本地下载目录：{name} → {root}")
        except Exception as error:  # noqa: BLE001 - 登记失败仅影响手动下载
            logger.warning(f"登记下载目录失败：{error}")

    def _ensure_downloader_entry(self) -> None:
        """在系统下载器配置里登记本插件，让下载页出现独立标签。

        MoviePilot 的下载页按系统配置里的下载器名称分标签，
        因此插件提供一个同名条目；实际任务由本插件自己的 list_torrents 返回。
        """
        name = self._downloader_name()
        try:
            current = self.systemconfig.get(SystemConfigKey.Downloaders) or []
        except Exception as error:  # noqa: BLE001 - 配置读取失败不应阻断插件
            logger.warning(f"读取下载器配置失败：{error}")
            return
        items = [dict(item) for item in current if isinstance(item, dict)]
        existing = next((item for item in items if item.get("name") == name), None)
        if existing is not None and existing.get("enabled"):
            return
        if existing is not None:
            existing["enabled"] = True
        else:
            items.append({
                "name": name,
                "type": "QuarkChannel",
                "default": False,
                "config": {},
                "enabled": True,
                "path_mapping": [],
            })
        try:
            self.systemconfig.set(SystemConfigKey.Downloaders, items)
            logger.info(f"已登记下载器条目：{name}")
        except Exception as error:  # noqa: BLE001 - 登记失败时仅影响界面展示
            logger.warning(f"登记下载器条目失败：{error}")

    def _make_client(self) -> QuarkShareClient:
        """创建取链客户端，并传入副本保留策略。"""
        return QuarkShareClient(
            str(self._config.get("quark_cookie") or ""),
            keep_saved=bool(self._config.get("keep_saved")),
            on_cookie_refresh=self._save_refreshed_cookie,
        )

    def _save_refreshed_cookie(self, cookie: str) -> None:
        """把夸克自动刷新的 Cookie 回写插件配置。

        :param cookie: 刷新后的 Cookie
        """
        if not cookie or cookie == self._config.get("quark_cookie"):
            return
        config = dict(self._config or {})
        config["quark_cookie"] = cookie
        self._config = config
        try:
            self.update_config(config)
        except Exception as error:  # noqa: BLE001 - 回写失败不影响下载
            logger.warning(f"回写夸克 Cookie 失败：{type(error).__name__}")

    def _download_root(self) -> str:
        """下载根目录。"""
        return str(self._config.get("download_root") or "/downloads")

    def _resolve_download_dir(self, download_dir: Any) -> Optional[Path]:
        """把 MoviePilot 的下载目录转换为本地路径；远程存储返回 None。

        :param download_dir: MoviePilot 传入的下载目录（可能带存储前缀）
        :return: 本地路径；下载目录为远程存储时返回 None
        """
        text = str(download_dir or "")
        if text.startswith("local:"):
            return Path(text[len("local:"):])
        if text.startswith(STORAGE_PREFIX):
            logger.warning(f"直链频道不支持远程存储下载目录：{text}")
            return None
        if text.startswith("/"):
            return Path(text)
        return Path(self._download_root())

    def _torrent_url(self, sid: str, fid: str = "") -> str:
        """合成种子的下载地址；带 fid 时指向单个文件。"""
        base = str(self._config.get("mp_base_url") or "http://127.0.0.1:3000").rstrip("/")
        url = (
            f"{base}/api/v1/plugin/QuarkChannel/torrent"
            f"?sid={quote(str(sid))}&sig={sign(str(sid), self._secret)}"
        )
        if fid:
            url += f"&fid={quote(str(fid))}"
        return url

    def _keyword_allowed(self, keyword: str) -> bool:
        """按白名单决定是否参与本次搜索。"""
        if not bool(self._config.get("only_whitelist")):
            return True
        raw = str(self._config.get("keyword_whitelist") or "")
        words = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
        if not words:
            return True
        return any(word.lower() in keyword.lower() for word in words)

    def _ensure_entry(self, share_url: str, passcode: str, title: str,
                      force: bool = False) -> Optional[ShareEntry]:
        """确保分享快照存在并返回它。"""
        normalized = normalize_share_url(share_url) or share_url
        sid = build_sid(normalized)
        if self._cache:
            cached = self._cache.get(sid)
            if cached and not force and time.time() - cached.updated_at < 1800:
                return cached
        try:
            snapshot = self._make_client().snapshot(normalized, passcode)
        except Exception as error:  # noqa: BLE001 - 单条分享失败不影响其它候选
            logger.info(f"读取分享失败：{share_url}，{error}")
            if self._cache:
                self._cache.drop(sid)
            return None
        entry = ShareEntry(
            sid=sid,
            provider="quark",
            share_url=normalized,
            passcode=passcode,
            title=title or snapshot.title,
            files=[item.as_dict() for item in snapshot.files if not item.is_dir],
            total_size=snapshot.total_size,
        )
        if self._cache:
            self._cache.put(entry)
        return entry

    def _build_out_name(self, entry: ShareEntry, item: dict) -> str:
        """下载时按「剧名 SxxExx」重命名落盘文件。

        :param entry: 分享快照
        :param item: 文件条目（含 name/size）
        :return: 目标文件名；关闭重命名时返回空串
        """
        if not bool(self._config.get("rename_on_download", True)):
            return ""
        raw_name = str(item.get("name") or "")
        suffix = Path(raw_name).suffix.lower() or ".mkv"
        base = entry.folder_name or "video"
        # 去掉标题里常见的发布标记，避免文件名过长
        for token in ("名称：", "名称:", "【", "】", "[", "]"):
            base = base.replace(token, " ")
        base = " ".join(base.split())[:80]
        episode = self._episode_of(raw_name)
        if episode is None:
            return ""
        season = 1
        match = re.search(r"[Ss](\d{1,2})[Ee]\d{1,4}", raw_name)
        if match:
            try:
                season = int(match.group(1))
            except (TypeError, ValueError):
                season = 1
        return f"{base} S{season:02d}E{episode:02d}{suffix}"

    @staticmethod
    def _select_files(entry: ShareEntry, episodes: Optional[set]) -> list[dict]:
        """按目标集数筛选文件；目标集数非空但无一命中时返回空，避免整包下载。"""
        files = [item for item in entry.files if not item.get("dir")]
        if not episodes:
            return files
        wanted = {int(item) for item in episodes if str(item).isdigit()}
        if not wanted:
            return files
        matched = [
            item for item in files
            if str(item.get("name") or "").lower().endswith(VIDEO_SUFFIXES)
            and QuarkChannel._episode_of(str(item.get("name") or "")) in wanted
        ]
        return matched

    @staticmethod
    def _episode_of(name: str) -> Optional[int]:
        """从文件名解析集号。

        支持 S01E01-E02、EP01-02、01-02 等连集写法；连集只返回起始集号，
        因为 MoviePilot 的 episodes 是集号集合，本插件不再展开区间（局限）。
        """
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(name)
            if match:
                try:
                    return int(match.group(match.lastindex or 1))
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def _map_state(state: str) -> str:
        """把内部状态映射成 MoviePilot 统一状态（downloading/paused/completed）。"""
        if state == STATE_COMPLETED:
            return "completed"
        if state == STATE_DOWNLOADING:
            return "downloading"
        return "paused"

    @staticmethod
    def _as_list(value: Optional[Union[list[str], str]]) -> list[str]:
        """把 hash 参数归一为列表。"""
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if item]
        return []
