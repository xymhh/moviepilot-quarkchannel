"""TG 命中即处理的自动追更决策。

监控线程拿到新条目后，本模块负责：解析分享文件集号、匹配启用订阅、
用媒体库已有集号计算缺集，最后通过 MoviePilot 的下载链提交单集任务。
整个过程不依赖订阅的 ``total_episode``，避免 TMDB 元数据滞后时整批被过滤。
"""

from __future__ import annotations

import difflib
import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.parse import quote

from app.sdk.logging import logger

try:  # 宿主公开领域对象；测试环境通过 _stubs 提供同名桩。
    from app.domain.context import Context, MediaInfo, MetaInfo, TorrentInfo
except ImportError:  # pragma: no cover - 仅兼容极简宿主/测试桩
    Context = MediaInfo = MetaInfo = TorrentInfo = None  # type: ignore[assignment]

try:  # MediaInfo.type 使用宿主枚举，订阅表里保存的是字符串。
    from app.domain.context import MediaType
except ImportError:  # pragma: no cover - 仅兼容极简宿主/测试桩
    MediaType = None  # type: ignore[assignment]

try:  # 公共数据访问入口，禁止直接依赖 app.db.models。
    from app.db.oper.subscribe import SubscribeOper
except ImportError:  # pragma: no cover - 测试可注入替身
    SubscribeOper = None  # type: ignore[assignment]

try:
    from app.db.oper.mediaserver import MediaServerOper
except ImportError:  # pragma: no cover - 测试可注入替身
    MediaServerOper = None  # type: ignore[assignment]

try:
    from app.chain.download import DownloadChain
except ImportError:  # pragma: no cover - 测试可注入替身
    DownloadChain = None  # type: ignore[assignment]

from .torrents import ShareEntry, build_sid


AUTO_PICK_FILE = "auto_pick.json"
PROCESSED_KEEP_DAYS = 30
PROCESSED_KEEP_COUNT = 2000
RECENT_KEEP_COUNT = 200
MAX_RANGE_EPISODES = 500

SEASON_EPISODE_RE = re.compile(
    r"[Ss](\d{1,2})[._ ]?[Ee](\d{1,4})"
    r"(?:\s*[-~]\s*(?:[Ss]\d{1,2}[._ ]?)?[Ee]?(\d{1,4}))?"
)
EPISODE_RE = re.compile(
    r"(?:第|EP|Ep|ep|E)\s*(\d{1,4})\s*(?:[集话話])?"
    r"(?:\s*[-~]\s*(\d{1,4}))?"
)
BRACKET_EPISODE_RE = re.compile(
    r"[\[\(【]\s*(\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?\s*[\]\)】]"
)
RANGE_RE = re.compile(r"(?<![\dA-Za-z])(\d{1,4})\s*[-~]\s*(\d{1,4})(?![\dA-Za-z])")
PLAIN_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,4})(?!\d)")
SEASON_ONLY_RE = re.compile(r"[Ss](\d{1,2})")
VIDEO_EXT_RE = re.compile(
    r"\.(?:mkv|mp4|avi|ts|m2ts|mov|wmv|flv|rmvb|rm|webm|mpg|mpeg)$",
    re.I,
)
FILE_EXT_RE = VIDEO_EXT_RE
NOISE_RE = re.compile(
    r"(?:更新至|更新到|更至|全)\s*\d{1,4}\s*[集话話]?|"
    r"\d{3,4}[pP]|[248][kK]|蓝光|高清|超清|中字|国语|粤语|双语|"
    r"WEB[- .]?DL|BluRay|BDRip|HDRip|x26[45]|H26[45]|HEVC|AVC|HDR|"
    r"DTS|AAC|AC3|Atmos|杜比",
    re.I,
)
BRACKET_BLOCK_RE = re.compile(r"[\[\(【][^\[\]\(\)【】]*[\]\)】]")
NON_WORD_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
RESOLUTION_TOKENS = {480, 576, 720, 1080, 2160, 4320}
MOVIE_TYPES = {"电影", "movie", "MOVIE"}


def _int_value(value: Any, default: int = 0) -> int:
    """把任意值安全转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容对象属性与字典字段的读取。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _episode_range(start: Any, end: Any = None) -> list[int]:
    """把起始集号与可选结束集号展开为连续集号列表。"""
    first = _int_value(start)
    if first <= 0:
        return []
    last = _int_value(end, first) if end is not None else first
    if last < first:
        last = first
    if last - first + 1 > MAX_RANGE_EPISODES:
        last = first + MAX_RANGE_EPISODES - 1
    return list(range(first, last + 1))


def _plain_episodes(name: str) -> list[int]:
    """从没有显式集号标记的文件名中提取一个裸集号。"""
    stem = FILE_EXT_RE.sub("", str(name or ""))
    stem = NOISE_RE.sub(" ", stem)
    numbers: list[int] = []
    for token in PLAIN_NUMBER_RE.findall(stem):
        value = _int_value(token)
        if value <= 0 or value > 2000:
            continue
        if 1900 <= value <= 2099 or value in RESOLUTION_TOKENS:
            continue
        numbers.append(value)
    if not numbers:
        return []
    compact = re.sub(r"[\s._\-\[\]()【】]", "", stem)
    if compact.isdigit():
        value = _int_value(compact)
        return [value] if 0 < value <= 2000 else []
    return [numbers[-1]]


def _season_of(name: str) -> Optional[int]:
    """从文件名读取季号；没有显式 Sxx 时返回 None。"""
    match = SEASON_EPISODE_RE.search(str(name or ""))
    if match:
        return _int_value(match.group(1)) or None
    match = SEASON_ONLY_RE.search(str(name or ""))
    return _int_value(match.group(1)) or None if match else None


@dataclass
class EpisodePlan:
    """把分享文件清单解析成可下载的单集计划。"""

    files: list[dict] = field(default_factory=list)

    def plan(self) -> list[dict]:
        """返回 [{episode, fid, file_name, size}]，同一集保留体积最大的文件。"""
        return self.from_files(self.files)

    @classmethod
    def from_files(cls, files: Optional[Iterable[dict]]) -> list[dict]:
        """解析文件清单并展开区间集号。

        :param files: 分享快照里的文件列表或 ShareEntry
        :return: 单集计划列表，按集号升序
        """
        if isinstance(files, ShareEntry):
            files = files.files
        by_episode: dict[int, dict] = {}
        for item in files or []:
            if not isinstance(item, dict) or item.get("dir"):
                continue
            file_name = str(item.get("name") or item.get("file_name") or "file")
            if not VIDEO_EXT_RE.search(file_name):
                # 只处理视频文件：字幕、图片、电子书与广告文件一律忽略
                continue
            size = _int_value(item.get("size"))
            if size <= 0:
                continue
            fid = str(item.get("fid") or "").strip()
            for episode in cls.episodes_of(file_name):
                candidate = {
                    "episode": int(episode),
                    "fid": fid,
                    "file_name": file_name,
                    "size": size,
                }
                existing = by_episode.get(episode)
                if existing is None or size > existing["size"]:
                    by_episode[episode] = candidate
        return [by_episode[key] for key in sorted(by_episode)]

    @classmethod
    def parse_files(cls, files: Optional[Iterable[dict]]) -> list[dict]:
        """兼容入口：同 :meth:`from_files`。"""
        return cls.from_files(files)

    parse = parse_files

    def entries(self) -> list[dict]:
        """返回当前文件清单的计划列表。"""
        return self.plan()

    @staticmethod
    def episodes_of(name: str) -> list[int]:
        """从文件名解析集号，支持单集与 ``21-25`` 区间。"""
        text = str(name or "")
        match = SEASON_EPISODE_RE.search(text)
        if match:
            return _episode_range(match.group(2), match.group(3))
        match = EPISODE_RE.search(text)
        if match:
            return _episode_range(match.group(1), match.group(2))
        match = BRACKET_EPISODE_RE.search(text)
        if match:
            return _episode_range(match.group(1), match.group(2))
        match = RANGE_RE.search(text)
        if match:
            return _episode_range(match.group(1), match.group(2))
        return _plain_episodes(text)

    @staticmethod
    def episode_of(name: str) -> Optional[int]:
        """只返回第一个解析到的集号。"""
        episodes = EpisodePlan.episodes_of(name)
        return episodes[0] if episodes else None

    @staticmethod
    def season_of(name: str) -> Optional[int]:
        """从文件名解析季号。"""
        return _season_of(name)


class SubscriptionMatcher:
    """使用标题相似度匹配启用订阅，不访问 TMDB。"""

    def __init__(self, min_similarity: float = 0.75,
                 subscribe_oper: Any = None) -> None:
        """保存阈值与可注入的订阅数据入口。

        :param min_similarity: 标题相似度阈值
        :param subscribe_oper: 可选 SubscribeOper 替身
        """
        if subscribe_oper is None and not isinstance(min_similarity, (int, float)):
            subscribe_oper = min_similarity
            min_similarity = 0.75
        threshold = 0.75 if min_similarity is None else float(min_similarity)
        self.min_similarity = max(0.0, min(threshold, 1.0))
        self._oper = subscribe_oper

    @staticmethod
    def normalize_title(title: Any) -> str:
        """归一化标题：去噪声词、集号与标点，保留中英文数字。"""
        text = str(title or "")
        text = FILE_EXT_RE.sub("", text)
        text = BRACKET_BLOCK_RE.sub(" ", text)
        text = NOISE_RE.sub(" ", text)
        text = re.sub(
            r"[Ss]\d{1,2}[._ ]?[Ee]\d{1,4}(?:\s*[-~]\s*(?:[Ee])?\d{1,4})?",
            " ",
            text,
        )
        text = re.sub(
            r"(?:第|EP|Ep|ep|E)\s*\d{1,4}\s*(?:[集话話])?"
            r"(?:\s*[-~]\s*\d{1,4})?",
            " ",
            text,
        )
        text = RANGE_RE.sub(" ", text)
        return NON_WORD_RE.sub("", text).lower()

    def _subscriptions(self) -> list[Any]:
        """读取并过滤启用订阅。"""
        oper = self._oper
        if oper is None:
            if SubscribeOper is None:
                return []
            oper = SubscribeOper()
        try:
            rows = oper.list()
        except Exception as error:  # noqa: BLE001 - 读取订阅失败不阻断监控
            logger.warning(f"自动追更读取订阅失败：{type(error).__name__}")
            return []
        return [item for item in rows or [] if self._is_enabled(item)]

    @staticmethod
    def _is_enabled(subscription: Any) -> bool:
        """兼容 enabled 字段与订阅状态两种启用表达。"""
        if isinstance(subscription, dict):
            enabled = subscription.get("enabled")
        else:
            enabled = getattr(subscription, "enabled", None)
        if enabled is not None and not bool(enabled):
            return False
        state = str(_field(subscription, "state", "") or "").upper()
        return state not in {"S", "STOP", "PAUSED"}

    @staticmethod
    def _is_movie(subscription: Any) -> bool:
        """判断订阅是否为电影，自动追更只处理剧集。"""
        return str(_field(subscription, "type", "") or "") in MOVIE_TYPES

    @classmethod
    def similarity(cls, left: Any, right: Any) -> float:
        """计算两个标题归一化后的相似度。"""
        first = cls.normalize_title(left)
        second = cls.normalize_title(right)
        if not first or not second:
            return 0.0
        return difflib.SequenceMatcher(None, first, second).ratio()

    def find(self, title: Any, subscriptions: Optional[Sequence[Any]] = None,
             season: Optional[int] = None,
             episode: Optional[int] = None) -> tuple[Optional[Any], float]:
        """返回最高分订阅及分数；没有达到阈值时返回 ``(None, 0.0)``。

        :param title: 条目标题
        :param subscriptions: 可选的已取订阅列表
        :param season: 候选季号，用于过滤不同季订阅
        :param episode: 候选集号，用于过滤订阅起始集
        """
        rows = list(subscriptions) if subscriptions is not None else self._subscriptions()
        best: Optional[Any] = None
        best_score = 0.0
        for subscription in rows:
            if not self._is_enabled(subscription) or self._is_movie(subscription):
                continue
            sub_season = _field(subscription, "season", None)
            if season is not None and sub_season is not None \
                    and _int_value(sub_season, -1) != _int_value(season, -2):
                continue
            if episode is not None:
                start = _int_value(_field(subscription, "start_episode", 1), 1) or 1
                if _int_value(episode) < start:
                    continue
            name_text = self.normalize_title(_field(subscription, "name", ""))
            title_text = self.normalize_title(title)
            if len(name_text) >= 2 and name_text in title_text:
                # 订阅名是条目标题的一部分时直接判定为命中（标题常带「名称：」「更至N集」等后缀）
                score = 1.0
            else:
                score = self.similarity(title, _field(subscription, "name", ""))
            if score >= self.min_similarity and score > best_score:
                best = subscription
                best_score = score
        return best, best_score

    def match(self, title: Any, subscriptions: Optional[Sequence[Any]] = None,
              season: Optional[int] = None,
              episode: Optional[int] = None) -> Optional[Any]:
        """返回最佳匹配订阅。"""
        return self.find(title, subscriptions, season, episode)[0]

    best_match = match


class AutoPickStore:
    """自动追更去重表与最近决策记录。"""

    def __init__(self, data_dir: Path,
                 keep_days: int = PROCESSED_KEEP_DAYS,
                 keep_count: int = PROCESSED_KEEP_COUNT) -> None:
        """加载 ``auto_pick.json`` 并清理过期数据。

        :param data_dir: 插件数据目录
        :param keep_days: processed 保留天数
        :param keep_count: processed 最大条数
        """
        self._dir = Path(data_dir)
        self._file = self._dir / AUTO_PICK_FILE
        self._keep_days = max(1, int(keep_days))
        self._keep_count = max(1, int(keep_count))
        self._lock = threading.RLock()
        self._processed: list[dict] = []
        self._recent: list[dict] = []
        self._load()

    @staticmethod
    def _key(tmdb_id: Any, season: Any, episode: Any, share_id: Any) -> tuple[str, int, int, str]:
        """生成去重键。"""
        return (
            str(tmdb_id or ""),
            _int_value(season),
            _int_value(episode),
            str(share_id or ""),
        )

    def is_processed(self, tmdb_id: Any, season: Any, episode: Any,
                     share_id: Any) -> bool:
        """判断同一作品、季、集与分享是否已处理。"""
        key = self._key(tmdb_id, season, episode, share_id)
        with self._lock:
            return any(self._key(
                item.get("tmdb_id"), item.get("season"),
                item.get("episode"), item.get("share_id"),
            ) == key for item in self._processed)

    def mark_processed(self, tmdb_id: Any, season: Any, episode: Any,
                       share_id: Any, at: Optional[float] = None) -> None:
        """写入一条去重记录并持久化。"""
        record = {
            "tmdb_id": str(tmdb_id or ""),
            "season": _int_value(season),
            "episode": _int_value(episode),
            "share_id": str(share_id or ""),
            "at": float(at if at is not None else time.time()),
        }
        with self._lock:
            key = self._key(
                record["tmdb_id"], record["season"],
                record["episode"], record["share_id"],
            )
            if not any(self._key(
                item.get("tmdb_id"), item.get("season"),
                item.get("episode"), item.get("share_id"),
            ) == key for item in self._processed):
                self._processed.append(record)
            self._purge()
            self._flush()

    def add_recent(self, record: dict) -> dict:
        """追加一条决策记录。"""
        item = dict(record or {})
        item.setdefault("at", time.time())
        with self._lock:
            self._recent.append(item)
            if len(self._recent) > RECENT_KEEP_COUNT:
                self._recent = self._recent[-RECENT_KEEP_COUNT:]
            self._flush()
        return item

    def recent(self, limit: int = 10) -> list[dict]:
        """返回最近决策副本，新的在前。"""
        with self._lock:
            rows = list(reversed(self._recent))
        return [dict(item) for item in rows[:max(1, int(limit or 10))]]

    def processed_count(self) -> int:
        """返回当前去重记录数量。"""
        with self._lock:
            return len(self._processed)

    def snapshot(self) -> dict:
        """返回可序列化的完整快照。"""
        with self._lock:
            return {
                "processed": [dict(item) for item in self._processed],
                "recent": [dict(item) for item in self._recent],
            }

    def _purge(self) -> None:
        """按天数与条数收敛 processed。"""
        cutoff = time.time() - self._keep_days * 86400
        kept: list[dict] = []
        for item in self._processed:
            raw = item.get("at")
            if raw is None:
                kept.append(item)
                continue
            try:
                if float(raw) >= cutoff:
                    kept.append(item)
            except (TypeError, ValueError):
                kept.append(item)
        self._processed = kept
        if len(self._processed) > self._keep_count:
            self._processed = self._processed[-self._keep_count:]

    def _load(self) -> None:
        """读取数据文件，坏文件按空表处理。"""
        if not self._file.exists():
            return
        try:
            payload = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        processed = payload.get("processed")
        recent = payload.get("recent")
        if isinstance(processed, list):
            self._processed = [dict(item) for item in processed if isinstance(item, dict)]
        if isinstance(recent, list):
            self._recent = [dict(item) for item in recent if isinstance(item, dict)][-RECENT_KEEP_COUNT:]
        self._purge()

    def _flush(self) -> None:
        """原子写回数据文件，失败只记录 warning。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "processed": self._processed,
                "recent": self._recent,
            }
            temp = self._file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError as error:
            logger.warning(f"写入自动追更去重表失败：{type(error).__name__}")


class MissingEpisodeResolver:
    """用媒体库已有集号计算待下载集，并查询去重表。"""

    def __init__(self, store: Optional[AutoPickStore] = None,
                 mediaserver_oper: Any = None) -> None:
        """保存去重表与媒体服务器数据入口。"""
        self.store = store
        self._oper = mediaserver_oper

    def _oper_instance(self) -> Any:
        """返回可用的媒体服务器数据入口。"""
        if self._oper is not None:
            return self._oper
        if MediaServerOper is None:
            return None
        self._oper = MediaServerOper()
        return self._oper

    @staticmethod
    def _episodes_of_item(item: Any, season: int) -> set[int]:
        """从媒体库条目的 seasoninfo 中提取某季已有集号。"""
        if item is None:
            return set()
        seasoninfo = _field(item, "seasoninfo", None) or {}
        values: Any = None
        if isinstance(seasoninfo, dict):
            values = seasoninfo.get(season)
            if values is None:
                values = seasoninfo.get(str(season))
        elif isinstance(seasoninfo, (list, tuple, set)):
            values = seasoninfo
        if values is None:
            return set()
        if isinstance(values, dict):
            values = values.get("episodes") or values.get("episode") or values
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        episodes: set[int] = set()
        for value in values:
            try:
                episode = int(value)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.add(episode)
        return episodes

    def existing_episodes(self, tmdb_id: Any = None, season: Any = 1,
                          media_source: Any = None, media_id: Any = None,
                          mtype: Any = None, title: str = "",
                          year: Any = None) -> set[int]:
        """查询媒体库中该作品/季已有集号。

        :param tmdb_id: 作品标识，缺省时用订阅 media_id
        :param season: 季号
        :param media_source: 媒体来源
        :param media_id: 来源原生 ID
        :param mtype: 媒体类型
        :param title: 标题，作为跨来源兜底
        :param year: 年份，作为跨来源兜底
        """
        oper = self._oper_instance()
        if oper is None:
            return set()
        season_no = _int_value(season, 1) or 1
        kwargs: dict[str, Any] = {"season": season_no}
        if media_source:
            kwargs["media_source"] = str(media_source)
        if media_id is not None:
            kwargs["media_id"] = str(media_id)
        elif tmdb_id is not None:
            kwargs["media_id"] = str(tmdb_id)
        if mtype:
            kwargs["mtype"] = str(mtype)
        if title:
            kwargs["title"] = str(title)
        if year:
            kwargs["year"] = str(year)
        try:
            item = oper.exists(**kwargs)
        except Exception as error:  # noqa: BLE001 - 媒体库查询失败按无已有集处理
            logger.warning(f"自动追更查询媒体库失败：{type(error).__name__}")
            return set()
        return self._episodes_of_item(item, season_no)

    def missing_episodes(self, candidates: Iterable[Any], tmdb_id: Any = None,
                         season: Any = 1, share_id: Any = "",
                         media_source: Any = None, media_id: Any = None,
                         mtype: Any = None, title: str = "",
                         year: Any = None) -> list[int]:
        """返回候选集中媒体库缺失且未被去重表处理的集号。"""
        wanted = sorted({
            episode for episode in (_int_value(item) for item in candidates or [])
            if episode > 0
        })
        if not wanted:
            return []
        season_no = _int_value(season, 1) or 1
        existing = self.existing_episodes(
            tmdb_id=tmdb_id, season=season_no, media_source=media_source,
            media_id=media_id, mtype=mtype, title=title, year=year,
        )
        missing: list[int] = []
        for episode in wanted:
            if episode in existing:
                continue
            if self.store is not None and self.store.is_processed(
                tmdb_id, season_no, episode, share_id,
            ):
                continue
            missing.append(episode)
        return missing

    def missing(self, candidates: Iterable[Any], tmdb_id: Any = None,
                season: Any = 1, share_id: Any = "",
                media_source: Any = None, media_id: Any = None,
                mtype: Any = None, title: str = "",
                year: Any = None) -> list[int]:
        """兼容入口：同 :meth:`missing_episodes`。"""
        return self.missing_episodes(
            candidates, tmdb_id=tmdb_id, season=season, share_id=share_id,
            media_source=media_source, media_id=media_id, mtype=mtype,
            title=title, year=year,
        )

    def resolve(self, candidates: Iterable[Any], subscription: Any,
                season: Any = None, share_id: Any = "") -> list[int]:
        """按订阅上下文计算缺集的便捷入口。"""
        season_no = _int_value(season, _int_value(_field(subscription, "season", 1), 1)) or 1
        return self.missing_episodes(
            candidates, tmdb_id=_subscription_tmdb_id(subscription),
            season=season_no, share_id=share_id,
            media_source=_field(subscription, "media_source", None),
            media_id=_field(subscription, "media_id", None),
            mtype=_field(subscription, "type", None),
            title=str(_field(subscription, "name", "") or ""),
            year=_field(subscription, "year", None),
        )


def _subscription_tmdb_id(subscription: Any) -> str:
    """返回订阅的稳定作品标识，优先 tmdb_id，其次 media_id。"""
    value = _field(subscription, "tmdb_id", None)
    if value in (None, ""):
        value = _field(subscription, "media_id", None)
    return str(value or "")


class AutoPicker:
    """把 TG 新条目转换为 MoviePilot 下载任务的决策器。"""

    def __init__(self, data_dir: Optional[Path] = None,
                 cache: Any = None,
                 snapshot_fetcher: Optional[Callable[..., Any]] = None,
                 torrent_url_builder: Optional[Callable[..., str]] = None,
                 config: Optional[dict] = None,
                 *,
                 enabled: bool = False,
                 dry_run: bool = True,
                 min_similarity: float = 0.75,
                 downloader: str = "网盘直链",
                 save_path: str = "",
                 store: Optional[AutoPickStore] = None,
                 subscribe_oper: Any = None,
                 mediaserver_oper: Any = None,
                 download_chain: Any = None) -> None:
        """保存运行依赖；不会在构造阶段访问网络或数据库。

        :param data_dir: 插件数据目录
        :param cache: 现有 TorrentCache
        :param snapshot_fetcher: 缓存未命中时现场取快照的调用
        :param torrent_url_builder: 生成合成种子地址的调用
        :param config: 插件配置字典，可覆盖显式参数
        :param enabled: 是否启用自动追更
        :param dry_run: 仅记录不下载
        :param min_similarity: 标题相似度阈值
        :param downloader: 下载器名称
        :param store: 去重表替身
        :param subscribe_oper: 订阅入口替身
        :param mediaserver_oper: 媒体服务器入口替身
        :param download_chain: 下载链替身
        """
        config = dict(config or {})
        if config:
            enabled = bool(config.get("auto_pick_enabled", enabled))
            dry_run = bool(config.get("auto_pick_dry_run", dry_run))
            if config.get("auto_pick_min_similarity") is not None:
                min_similarity = float(config["auto_pick_min_similarity"])
            downloader = str(config.get("auto_pick_downloader") or downloader)
        self.data_dir = Path(data_dir) if data_dir else Path(tempfile.gettempdir()) / "quarkchannel"
        self.cache = cache
        self.snapshot_fetcher = snapshot_fetcher
        self.torrent_url_builder = torrent_url_builder
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run)
        self.downloader = str(downloader or "网盘直链")
        self.save_path = str(save_path or "")
        self.store = store if store is not None else AutoPickStore(self.data_dir)
        self.matcher = SubscriptionMatcher(min_similarity, subscribe_oper)
        self.resolver = MissingEpisodeResolver(self.store, mediaserver_oper)
        self._download_chain = download_chain

    def recent(self, limit: int = 10) -> list[dict]:
        """返回最近决策记录。"""
        return self.store.recent(limit)

    def process(self, entries: Optional[Iterable[dict]]) -> list[dict]:
        """处理一批新条目，返回本次决策记录。

        单条目异常只记录 warning，不影响监控线程继续处理后续条目。
        """
        if not self.enabled:
            return []
        if isinstance(entries, dict):
            entries = [entries]
        rows = [item for item in entries or [] if isinstance(item, dict)]
        if not rows:
            return []
        subscriptions = self.matcher._subscriptions()
        decisions: list[dict] = []
        for entry in rows:
            try:
                decisions.extend(self._process_entry(entry, subscriptions))
            except Exception as error:  # noqa: BLE001 - 机关异常不能打断监控
                logger.warning(f"自动追更处理条目失败：{type(error).__name__}")
                decisions.append(self._record(
                    "error", f"处理失败：{type(error).__name__}",
                    entry=entry,
                ))
        return decisions

    def _process_entry(self, entry: dict, subscriptions: Sequence[Any]) -> list[dict]:
        """处理单条 TG 索引条目。"""
        title = str(entry.get("title") or "").strip()
        raw_sid = str(entry.get("sid") or "").strip()
        share_url = str(entry.get("share_url") or "").strip()
        if not share_url and raw_sid:
            share_url = f"https://pan.quark.cn/s/{raw_sid}"
        share_id = raw_sid or build_sid(share_url)
        decisions: list[dict] = []
        if not subscriptions:
            return [self._record("skipped", "没有可用订阅", entry=entry)]
        share = self._share_entry(entry)
        if share is None:
            return [self._record("skipped", "无法读取分享文件清单", entry=entry)]
        plan = EpisodePlan.from_files(share.files)
        if not plan:
            return [self._record("skipped", "分享内没有可识别集号", entry=entry, share=share)]

        grouped: dict[Optional[int], list[dict]] = {}
        for item in plan:
            season = _season_of(item["file_name"]) or _season_of(title)
            grouped.setdefault(season, []).append(item)

        for season, items in grouped.items():
            episodes = sorted(item["episode"] for item in items)
            subscription, score = self.matcher.find(
                title, subscriptions, season=season, episode=episodes[0],
            )
            if subscription is None:
                decisions.append(self._record(
                    "skipped", "未匹配到启用订阅", entry=entry, share=share,
                    season=season, episodes=episodes,
                ))
                continue
            season_no = _int_value(
                season, _int_value(_field(subscription, "season", 1), 1),
            ) or 1
            missing = self.resolver.missing_episodes(
                episodes,
                tmdb_id=_subscription_tmdb_id(subscription),
                season=season_no,
                share_id=share_id,
                media_source=_field(subscription, "media_source", None),
                media_id=_field(subscription, "media_id", None),
                mtype=_field(subscription, "type", None),
                title=str(_field(subscription, "name", "") or ""),
                year=_field(subscription, "year", None),
            )
            if not missing:
                decisions.append(self._record(
                    "skipped", "没有待下载缺集", entry=entry, share=share,
                    subscription=subscription, score=score,
                    season=season_no, episodes=episodes,
                ))
                continue
            by_fid: dict[str, dict] = {}
            item_by_episode = {item["episode"]: item for item in items}
            for episode in missing:
                item = item_by_episode.get(episode)
                if item is None:
                    continue
                group = by_fid.setdefault(item["fid"], {
                    "fid": item["fid"],
                    "file_name": item["file_name"],
                    "size": item["size"],
                    "episodes": [],
                })
                group["episodes"].append(episode)
            if not by_fid:
                decisions.append(self._record(
                    "skipped", "缺集没有对应文件", entry=entry, share=share,
                    subscription=subscription, score=score,
                    season=season_no, episodes=missing,
                ))
                continue
            for group in by_fid.values():
                decisions.append(self._submit(
                    entry=entry, share=share, subscription=subscription,
                    score=score, season=season_no, group=group,
                    share_id=share_id, title=title,
                ))
        return decisions

    def _share_entry(self, entry: dict) -> Optional[ShareEntry]:
        """优先从缓存取分享快照，未命中时现场取一次。"""
        raw_sid = str(entry.get("sid") or "").strip()
        share_url = str(entry.get("share_url") or "").strip()
        if not share_url and raw_sid:
            share_url = f"https://pan.quark.cn/s/{raw_sid}"
        candidates = [raw_sid, build_sid(share_url)] if share_url else [raw_sid]
        if self.cache is not None:
            for sid in candidates:
                if not sid:
                    continue
                try:
                    cached = self.cache.get(sid)
                except Exception as error:  # noqa: BLE001 - 缓存失败继续现场取
                    logger.warning(f"自动追更读取分享缓存失败：{type(error).__name__}")
                    cached = None
                if cached is not None:
                    return cached
        if self.snapshot_fetcher is None:
            return None
        try:
            fetched = self.snapshot_fetcher(
                share_url, str(entry.get("passcode") or ""),
                str(entry.get("title") or ""),
            )
        except Exception as error:  # noqa: BLE001 - 现场取快照失败只跳过该条
            logger.warning(f"自动追更现场读取分享失败：{type(error).__name__}")
            return None
        if isinstance(fetched, ShareEntry):
            return fetched
        if isinstance(fetched, dict) and isinstance(fetched.get("files"), list):
            try:
                return ShareEntry(**fetched)
            except TypeError:
                return None
        if fetched is not None and hasattr(fetched, "files"):
            return fetched
        return None

    def _submit(self, entry: dict, share: ShareEntry, subscription: Any,
                score: float, season: int, group: dict, share_id: str,
                title: str) -> dict:
        """构造领域上下文并提交或记录一次缺集下载。"""
        episodes = sorted(int(item) for item in group["episodes"])
        fid = str(group.get("fid") or "")
        enclosure = self._torrent_url(share.sid, fid)
        torrent = self._make_torrent(entry, share, enclosure, group, title)
        media = self._make_media(subscription, season, title)
        meta = self._make_meta(title, season, episodes)
        context = self._make_context(meta, media, torrent)
        common = {
            "entry": entry,
            "share": share,
            "subscription": subscription,
            "score": score,
            "season": season,
            "episodes": episodes,
            "fid": fid,
        }
        if self.dry_run:
            return self._record("dry_run", "仅记录不下载", **common)
        try:
            download_id = self._chain().download_single(
                context=context,
                torrent_content=None,
                episodes=set(episodes),
                downloader=self.downloader,
                save_path=self.save_path or None,
            )
        except Exception as error:  # noqa: BLE001 - 下载失败只记录
            logger.warning(f"自动追更提交下载失败：{type(error).__name__}")
            return self._record(
                "error", f"提交下载失败：{type(error).__name__}", **common,
            )
        for episode in episodes:
            self.store.mark_processed(
                _subscription_tmdb_id(subscription), season, episode, share_id,
            )
        if download_id is None:
            return self._record("failed", "下载器未受理", **common)
        return self._record(
            "downloaded", "已提交下载", download_id=str(download_id), **common,
        )

    def _chain(self) -> Any:
        """返回可用的下载链实例。"""
        if self._download_chain is not None:
            return self._download_chain
        if DownloadChain is None:
            raise RuntimeError("下载链不可用")
        self._download_chain = DownloadChain()
        return self._download_chain

    def _torrent_url(self, sid: str, fid: str) -> str:
        """生成合成种子地址。"""
        if self.torrent_url_builder is not None:
            return str(self.torrent_url_builder(sid, fid))
        url = f"/api/v1/plugin/QuarkChannel/torrent?sid={quote(str(sid))}"
        if fid:
            url += f"&fid={quote(str(fid))}"
        return url

    @staticmethod
    def _make_torrent(entry: dict, share: ShareEntry, enclosure: str,
                      group: dict, title: str) -> Any:
        """构造 MoviePilot 种子信息。"""
        if TorrentInfo is None:
            raise RuntimeError("TorrentInfo 不可用")
        return TorrentInfo(
            site=None,
            site_name=str(entry.get("channel") or "telegram"),
            title=title or share.title or share.folder_name,
            description=f"网盘直链 · 自动追更 · {group.get('file_name') or ''}".strip(),
            enclosure=enclosure,
            page_url=str(entry.get("share_url") or share.share_url or ""),
            size=float(max(0, _int_value(group.get("size")))),
            seeders=0,
            pubdate=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            category=None,
        )

    @staticmethod
    def _make_media(subscription: Any, season: int, title: str) -> Any:
        """用订阅字段构造媒体信息。"""
        if MediaInfo is None:
            raise RuntimeError("MediaInfo 不可用")
        media_type = _field(subscription, "type", None)
        if MediaType is not None and isinstance(media_type, str):
            aliases = {"tv": "电视剧", "movie": "电影", "music": "音乐"}
            try:
                media_type = MediaType(aliases.get(media_type.lower(), media_type))
            except (TypeError, ValueError):
                pass
        return MediaInfo(
            media_source=_field(subscription, "media_source", None),
            media_id=_field(subscription, "media_id", None),
            type=media_type,
            title=str(_field(subscription, "name", "") or title),
            year=_field(subscription, "year", None),
            season=season,
            tmdb_id=_field(subscription, "tmdb_id", None),
        )

    @staticmethod
    def _make_meta(title: str, season: int, episodes: Sequence[int]) -> Any:
        """构造识别信息，缺集列表直接交给下载链。"""
        if MetaInfo is None:
            raise RuntimeError("MetaInfo 不可用")
        return MetaInfo(
            title=title,
            season=season,
            episode_list=list(episodes),
        )

    @staticmethod
    def _make_context(meta: Any, media: Any, torrent: Any) -> Any:
        """构造下载上下文。"""
        if Context is None:
            raise RuntimeError("Context 不可用")
        return Context(
            meta_info=meta,
            media_info=media,
            torrent_info=torrent,
            resource_source="rss",
        )

    def _record(self, action: str, reason: str, entry: Optional[dict] = None,
                share: Optional[ShareEntry] = None,
                subscription: Any = None, score: float = 0.0,
                season: Any = None, episodes: Optional[Sequence[int]] = None,
                fid: str = "", download_id: str = "", **kwargs: Any) -> dict:
        """写入一条最近决策并返回它。"""
        record = {
            "at": time.time(),
            "action": action,
            "reason": reason,
            "title": str((entry or {}).get("title") or getattr(share, "title", "") or ""),
            "share_id": str((entry or {}).get("sid") or getattr(share, "sid", "") or ""),
            "channel": str((entry or {}).get("channel") or ""),
            "tmdb_id": _subscription_tmdb_id(subscription),
            "season": _int_value(season, _int_value(_field(subscription, "season", 0), 0)),
            "episodes": [int(item) for item in episodes or []],
            "fid": str(fid or ""),
            "subscription": str(_field(subscription, "name", "") or ""),
            "similarity": round(float(score or 0.0), 4),
            "dry_run": self.dry_run,
            "downloaded": action == "downloaded",
        }
        if download_id:
            record["download_id"] = download_id
        return self.store.add_recent(record)
