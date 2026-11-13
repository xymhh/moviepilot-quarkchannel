"""盘搜（pansou）资源源。

pansou 的返回结构在不同版本间有差异，这里做容错解析：
递归查找带分享链接的对象，只保留夸克分享。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

QUARK_HOSTS = ("pan.quark.cn",)


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


class PansouSource:
    """pansou 搜索源。"""

    def __init__(self, base_url: str, timeout: int = 20, proxy: bool = False) -> None:
        """保存服务地址。

        :param base_url: 自建 pansou 地址，例如 http://pansou:8888
        :param timeout: 单次请求超时
        :param proxy: 是否走系统代理
        """
        self._base = (base_url or "").rstrip("/")
        self._timeout = timeout
        self._proxy = proxy

    @property
    def ready(self) -> bool:
        """是否已配置服务地址。"""
        return bool(self._base)

    def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索夸克分享。

        :param keyword: 关键词
        :param limit: 返回上限
        :return: [{title, url, passcode, source}]，失败返回空列表
        """
        if not self._base or not keyword:
            return []
        response = RequestUtils().get_res(
            f"{self._base}/api/search",
            params={
                "kw": keyword,
                "res": "merge",
                "src": "all",
                "cloud_types": "quark",
                "ext": "",
            },
            timeout=self._timeout,
        )
        if response is None:
            logger.warning("盘搜请求失败：无响应")
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
                "source": "pansou",
            })
            if len(candidates) >= limit:
                break
        logger.info(f"盘搜返回 {len(candidates)} 条夸克分享：{keyword}")
        return candidates


def normalize_share_url(url: str) -> Optional[str]:
    """去掉分享地址上的追踪参数，保证 sid 稳定。"""
    parsed = urlparse((url or "").strip())
    if not parsed.hostname or "quark" not in parsed.hostname:
        return None
    path = parsed.path.rstrip("/")
    return f"https://pan.quark.cn{path}"
