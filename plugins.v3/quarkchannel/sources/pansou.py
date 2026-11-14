"""盘搜（pansou）资源源。

pansou 的返回结构在不同版本间有差异，这里做容错解析：
递归查找带分享链接的对象，只保留夸克分享。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Iterable, Optional, Union
from urllib.parse import urlparse

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

QUARK_HOSTS = ("pan.quark.cn",)
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ITEMS = 200


def _walk(node: Any) -> Iterable[dict]:
    """深度遍历 JSON，产出全部字典节点。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _pick_url(node: dict) -> str:
    """从节点里挑出可用的分享地址。"""
    for key in ("url", "link", "share_url", "shareUrl"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def _pick_title(node: dict) -> str:
    """从节点里挑出可读标题。"""
    for key in ("title", "note", "content", "name", "desc", "message"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:200]
    return ""


def _pick_passcode(node: dict, url: str) -> str:
    """从节点或链接中取提取码。"""
    for key in ("password", "passcode", "pwd", "code"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    query = urlparse(url).query
    for part in query.split("&"):
        if part.startswith(("pwd=", "passcode=", "code=")):
            return part.split("=", 1)[-1]
    return ""


def _as_items(value: Any) -> list[str]:
    """把字符串、列表统一成去重候选项列表。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = str(value or "").replace("，", ",").split(",")
    return [item.strip() for item in items if item.strip()]


def _pick_size(node: dict) -> int:
    """从节点中读取字节大小；缺失或格式异常时返回 0。"""
    for key in ("size", "file_size", "size_bytes", "total_size", "bytes"):
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            try:
                return max(0, int(float(value.strip().replace(",", ""))))
            except (TypeError, ValueError):
                continue
    return 0


class PansouSource:
    """pansou 搜索源。"""

    def __init__(self, base_url: str, token: str = "", username: str = "",
                 password: str = "", timeout: int = 20, src: str = "all",
                 plugins: Union[str, list[str]] = "",
                 cloud_types: Union[str, list[str]] = "quark",
                 cache_namespace: str = "") -> None:
        """保存服务地址与访问凭据。

        :param base_url: 自建 pansou 地址，例如 http://pansou:8888
        :param token: 已有 JWT，留空则由用户名密码自动登录
        :param username: pansou 账号（AUTH_USERS 中的用户名）
        :param password: pansou 密码
        :param timeout: 单次请求超时
        :param src: 数据来源类型：all / tg（仅 Telegram 频道）/ plugin（仅插件）
        :param plugins: 指定启用的 pansou 插件，逗号分隔；空表示全部
        :param cloud_types: 返回的网盘类型，逗号分隔；空表示全部
        :param cache_namespace: 进程内缓存命名空间，多渠道时按渠道隔离
        """
        self._base = (base_url or "").rstrip("/")
        self._token = (token or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._timeout = timeout
        self._src = (src or "all").strip() or "all"
        self._plugins = _as_items(plugins)
        self._cloud_types = _as_items(cloud_types)
        self._cache_namespace = str(cache_namespace or "").strip()
        self._search_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
        self._cache_lock = threading.RLock()

    def _login(self) -> str:
        """用账号密码换取 JWT。

        :return: 新 token；失败返回空串
        """
        if not self._username or not self._password:
            return ""
        response = RequestUtils().post_res(
            f"{self._base}/api/auth/login",
            json={"username": self._username, "password": self._password},
            timeout=self._timeout,
        )
        if response is None:
            logger.warning("盘搜登录失败：请求无响应")
            return ""
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 视为登录失败
            logger.warning(f"盘搜登录失败：响应非 JSON（HTTP {response.status_code}）")
            return ""
        token = str((payload or {}).get("token") or "")
        if not token:
            reason = str((payload or {}).get("error") or (payload or {}).get("message") or "")
            logger.warning(f"盘搜登录失败：HTTP {response.status_code} {reason}".strip())
        if token:
            logger.info("盘搜登录成功，已缓存访问令牌")
        return token

    def _headers(self) -> dict:
        """构造请求头，无 token 时尝试自动登录一次。"""
        if not self._token:
            self._token = self._login()
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @property
    def ready(self) -> bool:
        """是否已配置服务地址。"""
        return bool(self._base)

    def _query_payload(self, keyword: str) -> dict:
        """按配置组装 POST JSON 搜索参数。

        :param keyword: 关键词
        :return: pansou /api/search 的 JSON 请求体
        """
        return {
            "kw": keyword,
            "res": "merge",
            "src": self._src,
            "plugins": list(self._plugins),
            "cloud_types": list(self._cloud_types),
        }

    def _cache_key(self, keyword: str) -> tuple:
        """生成插件侧搜索缓存键。"""
        return (
            self._cache_namespace, keyword, self._src,
            tuple(self._plugins), tuple(self._cloud_types),
        )

    def _cache_get(self, key: tuple) -> Optional[list[dict]]:
        """读取未过期的缓存副本，并刷新 LRU 顺序。"""
        now = time.monotonic()
        with self._cache_lock:
            cached = self._search_cache.get(key)
            if not cached:
                return None
            expires_at, rows = cached
            if expires_at <= now:
                self._search_cache.pop(key, None)
                return None
            self._search_cache.move_to_end(key)
            return [dict(item) for item in rows]

    def _cache_put(self, key: tuple, rows: list[dict]) -> None:
        """写入缓存并按 LRU 限制条目数。"""
        with self._cache_lock:
            self._search_cache[key] = (
                time.monotonic() + CACHE_TTL_SECONDS,
                [dict(item) for item in rows],
            )
            self._search_cache.move_to_end(key)
            while len(self._search_cache) > CACHE_MAX_ITEMS:
                self._search_cache.popitem(last=False)

    def check_auth(self) -> tuple[bool, str]:
        """检查鉴权状态，供插件状态页展示。

        :return: (是否可用, 失败原因)；未开启鉴权时视为可用
        """
        if not self._base:
            return False, "未配置盘搜地址"
        token = self._token or self._login()
        if token:
            return True, ""
        if not self._username:
            return False, "未填写盘搜账号或令牌"
        return False, "登录失败（账号密码错误或服务不可用）"

    def _request_search(self, keyword: str) -> Any:
        """发起一次 POST 搜索，401 时自动重新登录后重试。"""
        response = RequestUtils().post_res(
            f"{self._base}/api/search",
            headers=self._headers(),
            json=self._query_payload(keyword),
            timeout=self._timeout,
        )
        if response is not None and response.status_code == 401:
            self._token = self._login()
            if self._token:
                response = RequestUtils().post_res(
                    f"{self._base}/api/search",
                    headers=self._headers(),
                    json=self._query_payload(keyword),
                    timeout=self._timeout,
                )
        return response

    def search(self, keyword: str, limit: int = 20,
               bypass_cache: bool = False) -> list[dict]:
        """搜索夸克分享，优先命中 15 分钟进程内缓存。

        :param keyword: 关键词
        :param limit: 返回上限
        :param bypass_cache: 为 True 时跳过缓存读取，但仍刷新缓存
        :return: [{title, url, passcode, size, source}]，失败返回空列表
        """
        if not self._base or not keyword:
            return []
        key = self._cache_key(keyword)
        if not bypass_cache:
            cached = self._cache_get(key)
            if cached is not None:
                logger.info(f"盘搜缓存命中：{keyword}，{len(cached)} 条")
                return cached[:max(0, int(limit))]
        response = self._request_search(keyword)
        if response is None:
            logger.warning("盘搜请求失败：无响应")
            return []
        if response.status_code == 401:
            logger.warning("盘搜鉴权失败：请检查账号密码或令牌")
            return []
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 视为失败
            logger.warning("盘搜返回非 JSON")
            return []
        candidates: list[dict] = []
        seen: set[str] = set()
        for node in _walk(payload):
            url = _pick_url(node)
            if not url:
                continue
            host = urlparse(url).hostname or ""
            if not any(host.endswith(item) for item in QUARK_HOSTS):
                continue
            title = _pick_title(node) or keyword
            if url in seen:
                continue
            seen.add(url)
            candidates.append({
                "title": title,
                "url": url,
                "passcode": _pick_passcode(node, url),
                "size": _pick_size(node),
                "source": "pansou",
            })
            if len(candidates) >= CACHE_MAX_ITEMS:
                break
        self._cache_put(key, candidates)
        logger.info(f"盘搜返回 {len(candidates)} 条夸克分享：{keyword}")
        return candidates[:max(0, int(limit))]


def normalize_share_url(url: str) -> Optional[str]:
    """去掉分享地址上的追踪参数，保证 sid 稳定。"""
    parsed = urlparse((url or "").strip())
    if not parsed.hostname or "quark" not in parsed.hostname:
        return None
    path = parsed.path.rstrip("/")
    return f"https://pan.quark.cn{path}"
