"""夸克分享取链客户端。

只做三件事：换 stoken、列分享目录、取文件直链。
请求统一走宿主 `app.sdk.network.RequestUtils`，不自行管理连接池。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)
BASE_HOSTS = (
    "https://drive-h.quark.cn",
    "https://drive-pc.quark.cn",
    "https://drive.quark.cn",
)
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
MAX_PAGES = 20


@dataclass
class ShareFile:
    """分享内的单个文件。"""

    fid: str
    name: str
    size: int
    share_token: str = ""
    is_dir: bool = False

    def as_dict(self) -> dict:
        """转换为缓存结构。"""
        return {
            "fid": self.fid,
            "name": self.name,
            "size": self.size,
            "share_token": self.share_token,
            "dir": self.is_dir,
        }


@dataclass
class ShareSnapshot:
    """分享整体快照。"""

    title: str
    files: list[ShareFile]
    total_size: int

    def file_dicts(self) -> list[dict]:
        """转换为缓存结构，目录不参与下载。"""
        return [item.as_dict() for item in self.files if not item.is_dir]


class QuarkShareClient:
    """夸克分享读取与取链。"""

    def __init__(self, cookie: str = "", proxy: bool = False) -> None:
        """保存凭据与代理开关。

        :param cookie: 可选，带上自己的账号 cookie 可降低匿名请求被限的概率
        :param proxy: 是否走系统代理
        """
        self._cookie = (cookie or "").strip()
        self._proxy = proxy
        self._request = RequestUtils()

    def _headers(self, referer: str = "https://pan.quark.cn/") -> dict[str, str]:
        """构造统一请求头。"""
        headers = {
            "User-Agent": PC_UA,
            "Referer": referer,
            "Origin": "https://pan.quark.cn",
            "Accept": "application/json, text/plain, */*",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    @staticmethod
    def _json(response: Any) -> dict:
        """安全读取响应 JSON。"""
        if response is None:
            return {}
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应按失败处理
            return {}
        return payload if isinstance(payload, dict) else {}

    def _call(self, method: str, path: str, *, params: dict, payload: Optional[dict] = None,
              timeout: int = 20) -> dict:
        """在候选域名上依次尝试接口，返回首个成功响应。"""
        last: dict = {}
        for host in BASE_HOSTS:
            url = f"{host}{path}"
            merged = {**COMMON_PARAMS, **params}
            try:
                if method == "GET":
                    response = self._request.get_res(
                        url, params=merged, headers=self._headers(),
                        proxies=self._proxy_opt(), timeout=timeout,
                    )
                else:
                    response = self._request.post_res(
                        url, params=merged, json=payload or {}, headers=self._headers(),
                        proxies=self._proxy_opt(), timeout=timeout,
                    )
            except Exception as error:  # noqa: BLE001 - 单域名异常继续尝试
                logger.debug(f"夸克接口 {path} 域名 {host} 请求异常：{error}")
                continue
            data = self._json(response)
            if not data:
                continue
            last = data
            if int(data.get("status") or 0) == 200 or data.get("data") is not None:
                return data
        return last

    def _proxy_opt(self) -> Optional[dict]:
        """代理参数透传。"""
        return None if self._proxy else None

    def stoken(self, pwd_id: str, passcode: str = "") -> str:
        """换取分享 stoken。

        :param pwd_id: 分享 id（URL 中 /s/ 之后的部分）
        :param passcode: 提取码，无则不传
        :return: stoken 字符串，失败返回空串
        """
        payload = {"pwd_id": pwd_id, "passcode": passcode or ""}
        data = self._call("POST", "/1/clouddrive/share/sharepage/token", params={}, payload=payload)
        token = str(((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("stoken") or "")
        if not token:
            logger.warning(f"夸克分享换 stoken 失败：{data.get('message') or data.get('code')}")
        return token

    def _page(self, pwd_id: str, token: str, pdir_fid: str, page: int) -> dict:
        """读取一页分享目录。"""
        params = {
            "pwd_id": pwd_id,
            "stoken": token,
            "pdir_fid": pdir_fid or "0",
            "pagenum": page,
            "pagesize": 100,
            "force": 0,
            "_fetch_banner": 0,
            "_fetch_share": 1,
            "_fetch_total": 1,
            "_sort": "file_type:asc,file_name:asc",
        }
        return self._call("GET", "/1/clouddrive/share/sharepage/detail", params=params)

    def snapshot(self, share_url: str, passcode: str = "") -> ShareSnapshot:
        """读取分享整体文件清单（递归一层目录，避免遗漏子目录里的剧集）。

        :param share_url: 分享链接
        :param passcode: 提取码
        :return: 分享快照；失败时抛 ValueError
        """
        pwd_id = extract_pwd_id(share_url)
        if not pwd_id:
            raise ValueError("无法从链接中解析分享 id")
        token = self.stoken(pwd_id, passcode)
        if not token:
            raise ValueError("分享不可用或提取码错误")
        files: list[ShareFile] = []
        queue: list[str] = [""]
        visited = 0
        while queue and visited < MAX_PAGES:
            pdir_fid = queue.pop(0)
            page = 1
            while page <= MAX_PAGES:
                data = self._page(pwd_id, token, pdir_fid, page)
                payload = data.get("data") or {}
                if not isinstance(payload, dict):
                    break
                rows = payload.get("list") or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    file_item = ShareFile(
                        fid=str(row.get("fid") or ""),
                        name=str(row.get("file_name") or ""),
                        size=int(row.get("size") or 0),
                        share_token=str(row.get("share_fid_token") or ""),
                        is_dir=bool(row.get("dir")),
                    )
                    if not file_item.fid or not file_item.name:
                        continue
                    if file_item.is_dir:
                        if len(queue) < 50:
                            queue.append(file_item.fid)
                        continue
                    files.append(file_item)
                visited += 1
                total = int(payload.get("count") or 0)
                if page * 100 >= total or not rows:
                    break
                page += 1
            if visited >= MAX_PAGES:
                break
        if not files:
            raise ValueError("分享内没有文件")
        title = str((((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("share") or {}).get("title") or "")
        total_size = sum(item.size for item in files)
        return ShareSnapshot(title=title or pwd_id, files=files, total_size=total_size)

    def download_url(self, share_url: str, passcode: str, file_item: ShareFile) -> tuple[str, dict]:
        """取单个文件的直链。

        :return: (下载地址, 需要携带的请求头)
        """
        pwd_id = extract_pwd_id(share_url)
        token = self.stoken(pwd_id, passcode)
        if not token:
            raise ValueError("分享不可用，无法取链")
        # 夸克分享内的文件可以直接用分享路径的 fid 换直链：入口参数 entry=ft，
        # 必须伪装 PC 客户端 UA，否则会被判成游客并撞上 23018 限制。
        payload = {"fids": [file_item.fid]}
        data = self._call(
            "POST",
            "/1/clouddrive/file/download",
            params={"entry": "ft", "fr": "pc", "pr": "ucpro"},
            payload=payload,
        )
        rows = data.get("data")
        entry = rows[0] if isinstance(rows, list) and rows else {}
        url = str((entry or {}).get("download_url") or "")
        if not url:
            message = str(data.get("message") or "")
            raise ValueError(f"夸克未返回下载地址：{message or '未知原因'}")
        headers = {"User-Agent": PC_UA, "Referer": "https://pan.quark.cn/"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        time.sleep(0.2)
        return url, headers


def extract_pwd_id(share_url: str) -> str:
    """从分享链接中解析 pwd_id。

    :param share_url: https://pan.quark.cn/s/xxxx 形式
    :return: 分享 id，解析失败返回空串
    """
    text = (share_url or "").strip()
    if not text:
        return ""
    marker = "/s/"
    if marker in text:
        text = text.split(marker, 1)[1]
    for sep in ("?", "#", "/"):
        text = text.split(sep, 1)[0]
    return text.strip()
