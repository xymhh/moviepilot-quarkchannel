"""网盘直链频道（QuarkChannel）。

把网盘分享链接转换成 MoviePilot 能识别的种子资源，
再由本插件自己完成「取链 → 分片下载 → 回报任务状态」。

MoviePilot 侧只负责：搜索聚合、订阅、过滤、下载管理、整理、STRM、Emby 与通知。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

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
from .downloader import (
    DownloadManager,
    DownloadTask,
    STATE_COMPLETED,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_STOPPED,
    TaskFile,
)
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
)
STORAGE_PREFIX = ("u115:", "alipan:", "rclone:", "alist:", "alistgo:", "smb:")


class QuarkChannel(_PluginBase):
    """网盘直链频道插件。"""

    plugin_name = "网盘直链频道"
    plugin_desc = "把夸克分享链接转成 MoviePilot 原生资源，并下载到本地目录。"
    plugin_icon = "QuarkChannel.png"
    plugin_version = "0.6.1"
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
    _tg_index: Optional[TgIndex] = None
    _tg_monitor: Optional[TgMonitor] = None
    _secret: str = ""
    _aria2_secret: str = ""

    # ---------- 生命周期 ----------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并重建运行状态；允许重复调用。"""
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._config = config
        data_dir = self.get_data_path()
        self._cache = TorrentCache(data_dir)
        self._stop_tg_monitor()
        self._tg_index = TgIndex(data_dir)
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
        if bool(config.get("tg_enabled")):
            api_id = api_hash = session = ""
            try:
                api_id = int(config.get("tg_api_id") or 0)
                scan_limit = max(1, min(int(config.get("tg_scan_limit") or 100), 1000))
                interval = max(1, min(int(config.get("tg_interval_minutes") or 10), 1440))
            except (TypeError, ValueError):
                logger.warning("Telegram 监控参数无效，未启动")
            else:
                api_hash = str(config.get("tg_api_hash") or "").strip()
                session = str(config.get("tg_session") or "").strip()
                raw_channels = str(config.get("tg_channels") or "")
                channels = list(dict.fromkeys(
                    item.strip() for item in re.split(r"[,，\n]+", raw_channels) if item.strip()
                ))
                if api_id and api_hash and session and channels:
                    try:
                        monitor = TgMonitor(
                            data_dir, api_id, api_hash, session, channels,
                            scan_limit=scan_limit, interval_minutes=interval,
                            backfill=bool(config.get("tg_backfill")),
                        )
                        monitor.start()
                        self._tg_monitor = monitor
                        self._tg_index = monitor.index
                        logger.info(
                            f"Telegram 频道监控已启动：{len(channels)} 个频道，"
                            f"间隔 {interval} 分钟"
                        )
                    except Exception as error:  # noqa: BLE001 - 监控失败不影响搜索下载
                        logger.warning(f"Telegram 监控启动失败：{type(error).__name__}")
                else:
                    logger.warning("Telegram 监控配置不完整，未启动")
        if self._enabled:
            self._ensure_downloader_entry()
        logger.info(
            f"网盘直链频道初始化：enabled={self._enabled}，"
            f"pansou={bool(self._pansou.ready)}，下载根目录={self._download_root()}"
        )

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止 Telegram 监控、下载线程并释放资源。"""
        self._stop_tg_monitor()
        if self._manager:
            self._manager.shutdown()
        self._manager = None
        self._enabled = False

    def _stop_tg_monitor(self) -> None:
        """停止并释放 Telegram 监控线程。"""
        monitor = self._tg_monitor
        self._tg_monitor = None
        if not monitor:
            return
        try:
            monitor.stop()
        except Exception as error:  # noqa: BLE001 - 停止失败不阻断插件卸载
            logger.warning(f"停止 Telegram 监控失败：{type(error).__name__}")

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
            props: dict[str, Any] = {"model": model, "label": label}
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
                if kind == "select":
                    component = "VSelect"
                    props["items"] = items or []
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
            header: dict[str, Any] = {"component": "VCardTitle", "text": title}
            content: list[dict] = [header]
            if subtitle:
                content.append({"component": "VCardSubtitle", "text": subtitle})
            content.append({"component": "VCardText", "props": {"class": "pt-2"},
                            "content": [{"component": "VRow", "content": fields}]})
            return {"component": "VCard", "props": {"variant": "tonal", "class": "mb-4"},
                    "content": content}

        def tab(value: str, text: str) -> dict:
            """生成一个选项卡标签。"""
            return {"component": "VTab", "props": {"value": value}, "text": text}

        def panel(value: str, *cards: dict) -> dict:
            """生成一个选项卡面板。"""
            return {"component": "VWindowItem", "props": {"value": value},
                    "content": list(cards)}

        basic = card("基础", [
            field("enabled", "启用插件", kind="switch", cols=4,
                  hint="关闭后不参与搜索，MoviePilot 回到原生 BT/PT"),
            field("downloader_name", "下载器名称", cols=4,
                  hint="下载页里显示的标签名"),
            field("download_root", "下载根目录", cols=4, placeholder="/downloads"),
            field("max_tasks", "并发下载任务数", kind="number", cols=4),
            field("only_whitelist", "仅白名单关键词生效", kind="switch", cols=4),
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

        tg = card("Telegram 频道", [
            field("tg_enabled", "启用频道监控", kind="switch", cols=4,
                  hint="按频道游标增量拉取，用于追更"),
            field("tg_api_id", "api_id", cols=4),
            field("tg_api_hash", "api_hash", kind="password", cols=4),
            field("tg_session", "StringSession", kind="password", cols=12,
                  hint="Telethon 会话串，与 api_id/api_hash 配套"),
            field("tg_channels", "频道列表", cols=12,
                  placeholder="https://t.me/xxx,https://t.me/yyy"),
            field("tg_interval_minutes", "轮询间隔（分钟）", kind="number", cols=4),
            field("tg_scan_limit", "单次扫描条数", kind="number", cols=4),
            field("tg_backfill", "首次接入回补历史", kind="switch", cols=4,
                  hint="开启后首次扫描会把最近消息也建入索引"),
        ], subtitle="增量索引优先参与搜索，实现追更")

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

        tabs = {"component": "VTabs", "props": {"model": "active_tab", "density": "comfortable"},
                "content": [
                    tab("basic", "基础"), tab("pansou", "盘搜"), tab("tg", "Telegram 频道"),
                    tab("download", "下载"), tab("advanced", "高级"),
                ]}
        window = {"component": "VWindow", "props": {"model": "active_tab"},
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
            "aria2_enabled": True,
            "aria2_connections": 64,
            "aria2_split": 64,
            "keep_saved": False,
            "mp_base_url": "http://127.0.0.1:3000",
        }
        return [{"component": "VForm", "content": [tabs, window]}], defaults

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
        """优先查 Telegram 增量索引，再合并 pansou 结果并返回种子资源。"""
        if not self._enabled or not keyword or page:
            return []
        if not self._keyword_allowed(keyword):
            logger.debug(f"直链频道跳过关键词：{keyword}")
            return []
        limit = max(1, min(int(self._config.get("max_results") or 3), 10))
        results: list[TorrentInfo] = []
        seen: set[str] = set()
        if self._tg_index:
            try:
                tg_items = self._tg_index.search(keyword, limit=limit)
            except Exception as error:  # noqa: BLE001 - 索引损坏不应阻断 pansou
                logger.warning(f"读取 Telegram 索引失败：{type(error).__name__}")
                tg_items = []
            for item in tg_items:
                if len(results) >= limit:
                    break
                if not isinstance(item, dict):
                    continue
                url = str(item.get("share_url") or item.get("url") or "").strip()
                if not url:
                    continue
                sid = str(item.get("sid") or "").strip()
                if not sid:
                    sid = build_sid(normalize_share_url(url) or url)
                if sid in seen:
                    continue
                entry = self._ensure_search_entry(
                    url, str(item.get("passcode") or ""), str(item.get("title") or ""),
                )
                if not entry or entry.sid in seen:
                    continue
                seen.add(sid)
                seen.add(entry.sid)
                results.append(self._torrent_from_entry(
                    entry, str(item.get("channel") or "telegram"), 10, mtype,
                ))
        if self._pansou and self._pansou.ready and len(results) < limit:
            candidates = self._pansou.search(keyword, limit=limit * 2)
            for item in candidates:
                if len(results) >= limit:
                    break
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or item.get("share_url") or "").strip()
                if not url:
                    continue
                candidate_sid = build_sid(normalize_share_url(url) or url)
                if candidate_sid in seen:
                    continue
                entry = self._ensure_search_entry(
                    url, str(item.get("passcode") or ""), str(item.get("title") or ""),
                )
                if not entry or entry.sid in seen:
                    continue
                seen.add(candidate_sid)
                seen.add(entry.sid)
                results.append(self._torrent_from_entry(
                    entry, str(item.get("source") or "pansou"), 0, mtype,
                ))
        logger.info(f"直链频道为「{keyword}」提供 {len(results)} 条资源")
        return results

    def _ensure_search_entry(self, share_url: str, passcode: str,
                             title: str) -> Optional[ShareEntry]:
        """搜索链获取快照；单条异常只跳过该条，不阻断后续来源。"""
        try:
            return self._ensure_entry(share_url, passcode, title)
        except Exception as error:  # noqa: BLE001 - 单条失败不影响其它候选
            logger.warning(f"读取分享快照失败：{share_url}，{type(error).__name__}")
            return None

    def _torrent_from_entry(self, entry: ShareEntry, site_name: str,
                            pri_order: int, mtype: Optional[MediaType]) -> TorrentInfo:
        """把分享快照包装成 MoviePilot 种子信息。"""
        return TorrentInfo(
            site=None,
            site_name=str(site_name or "pansou"),
            title=entry.title,
            description=f"网盘直链 · {len(entry.files)} 个文件",
            enclosure=self._torrent_url(entry.sid),
            page_url=entry.share_url,
            size=float(entry.total_size),
            seeders=0,
            pubdate=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pri_order=int(pri_order),
            category=mtype.value if isinstance(mtype, MediaType) else None,
        )

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
        own_name = self._downloader_name()
        if downloader and downloader != own_name:
            # 下载页按标签查询：其他下载器的标签不返回本插件任务，避免重复显示
            return []
        results: list[DownloaderTorrent] = []
        for task in self._manager.tasks():
            if wanted is not None and task.hash not in wanted:
                continue
            state = self._map_state(task.state)
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

    def api_torrent(self, sid: str = "", sig: str = "") -> Response:
        """按分享 id 输出合成种子。"""
        if not self._cache or not verify(sid, sig, self._secret):
            return Response(content=b"forbidden", status_code=403)
        entry = self._cache.get(sid)
        if not entry:
            return Response(content=b"not found", status_code=404)
        try:
            payload = render_torrent(entry)
        except ValueError as error:
            logger.warning(f"生成种子失败：{error}")
            return Response(content=b"invalid share", status_code=404)
        return Response(
            content=payload,
            media_type="application/x-bittorrent",
            headers={"Content-Disposition": f'attachment; filename="{sid}.torrent"'},
        )

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

    def api_status(self) -> dict:
        """返回运行状态（含盘搜鉴权自检）。"""
        tasks = self._manager.tasks() if self._manager else []
        pansou_ok, pansou_reason = (False, "未初始化")
        if self._pansou:
            pansou_ok, pansou_reason = self._pansou.check_auth()
        tg_index = self._tg_index or getattr(self._tg_monitor, "index", None)
        tg_entries = len(getattr(tg_index, "entries", []) or []) if tg_index else 0
        tg_last_poll = float(getattr(tg_index, "last_poll", 0.0) or 0.0)
        if self._tg_monitor:
            try:
                tg_last_poll = max(
                    tg_last_poll, float(getattr(self._tg_monitor, "last_poll", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                pass
        return {
            "success": True,
            "data": {
                "enabled": self._enabled,
                "pansou": bool(self._pansou and self._pansou.ready),
                "pansou_ok": pansou_ok,
                "pansou_reason": pansou_reason,
                "tg_entries": tg_entries,
                "tg_last_poll": tg_last_poll,
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
        """对比同一夸克直链在 python 与 aria2 下的响应状态。

        :param payload: {"share_url": "...", "passcode": "", "file_index": 0}
        :return: 两步结果
        """
        payload = payload or {}
        share_url = str(payload.get("share_url") or "").strip()
        passcode = str(payload.get("passcode") or "").strip()
        index = int(payload.get("file_index") or 0)
        client = self._make_client()
        pwd_id = extract_pwd_id(share_url)
        token = client.stoken(pwd_id, passcode)
        if not token:
            return {"success": False, "message": "分享不可用"}
        snapshot = client.snapshot(share_url, passcode, token=token)
        if not snapshot.files:
            return {"success": False, "message": "分享内没有文件"}
        target = snapshot.files[min(index, len(snapshot.files) - 1)]
        saved = client.save_files(pwd_id, token, [target])
        if not saved:
            return {"success": False, "message": "转存失败"}
        try:
            url, headers = client.download_url_by_fid(saved[0])
            from app.sdk.network import RequestUtils as _Req
            probe = _Req().get_res(url, headers={**headers, "Range": "bytes=0-1023"}, timeout=30)
            python_status = getattr(probe, "status_code", None)
            body = getattr(probe, "content", b"") or b""
            try:
                body_text = body.decode("utf-8", "ignore")[:200]
            except Exception:  # noqa: BLE001 - 排障输出不允许中断
                body_text = ""
            result = {
                "success": True,
                "python": {"status": python_status, "bytes": len(body), "body": body_text},
                "headers": sorted(headers.keys()),
                "file": {"name": target.name, "size": target.size},
                "aria2": self._aria2_variants(url, headers),
            }
        finally:
            client.delete_files(saved)
        return result

    def _aria2_variants(self, url: str, headers: dict) -> list[dict]:
        """用不同请求头组合让 aria2 试下载几秒，返回各自的状态。

        :param url: 夸克直链
        :param headers: 取链时使用的请求头
        :return: [{variant, status, error}]
        """
        launcher = self._manager._aria2_launcher if self._manager else None
        if launcher is None or not launcher.ensure():
            return [{"variant": "none", "error": "aria2 不可用"}]
        rpc = launcher.rpc
        variants = {
            "ua+referer": {
                "user-agent": headers.get("User-Agent", ""),
                "referer": headers.get("Referer", ""),
            },
            "ua+referer+cookie": {
                "user-agent": headers.get("User-Agent", ""),
                "referer": headers.get("Referer", ""),
                "header": [f"Cookie: {headers.get('Cookie', '')}"] if headers.get("Cookie") else [],
            },
            "no-header": {},
        }
        results: list[dict] = []
        for name, extra in variants.items():
            options = {
                "dir": "/tmp",
                "out": f"diag-{name}",
                "split": "4",
                "max-connection-per-server": "4",
                "min-split-size": "1M",
                "continue": "false",
                "auto-file-renaming": "true",
            }
            options.update(extra)
            gid = rpc.add_uri(url, options)
            if not gid:
                results.append({"variant": name, "error": "addUri 失败"})
                continue
            time.sleep(3)
            status = rpc.tell_status(gid) or {}
            results.append({
                "variant": name,
                "status": status.get("status"),
                "error": str(status.get("errorMessage") or "")[:120],
                "done": status.get("completedLength"),
            })
            rpc.remove(gid)
        return results

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
        )

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

    def _torrent_url(self, sid: str) -> str:
        """合成种子的下载地址。"""
        base = str(self._config.get("mp_base_url") or "http://127.0.0.1:3000").rstrip("/")
        return f"{base}/api/v1/plugin/QuarkChannel/torrent?sid={sid}&sig={sign(sid, self._secret)}"

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

    @staticmethod
    def _select_files(entry: ShareEntry, episodes: Optional[set]) -> list[dict]:
        """按目标集数筛选文件；目标集数非空但无一命中时返回空，避免整包下载。"""
        files = [item for item in entry.files if not item.get("dir")]
        if not episodes:
            return files
        wanted = {int(item) for item in episodes if str(item).isdigit()}
        if not wanted:
            return files
        matched = [item for item in files if QuarkChannel._episode_of(str(item.get("name") or "")) in wanted]
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
