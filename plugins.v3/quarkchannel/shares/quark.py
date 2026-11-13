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

# 与生产可用实现保持一致：接口与下载统一使用浏览器 UA，
# 换链与下载的 UA 不一致会被 CDN 拒绝（403）。
PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/94.0.4606.71 Safari/537.36 "
    "Core/1.94.225.400 QQBrowser/12.2.5544.400"
)
# 夸克各接口绑定不同域名，且 stoken / share_fid_token 与域名绑定：
# token 与 detail 必须走 drive-h，save 走 drive.quark.cn，文件类接口走 drive-pc。
HOST_PAGE = "https://drive-h.quark.cn"
HOST_SAVE = "https://drive.quark.cn"
HOST_PC = "https://drive-pc.quark.cn"
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
MAX_PAGES = 20
SAVE_BATCH_SIZE = 50
TASK_POLL_TIMEOUT = 60.0
TASK_POLL_INTERVAL = 1.0
DEFAULT_SAVE_FOLDER = "MoviePilotQuarkChannel"


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

    def __init__(self, cookie: str = "", proxy: bool = False, keep_saved: bool = False) -> None:
        """保存凭据、代理开关与副本保留策略。

        :param cookie: 可选，带上自己的账号 cookie 可降低匿名请求被限的概率
        :param proxy: 是否走系统代理
        :param keep_saved: 下载完成后是否保留转存到网盘的副本
        """
        self._cookie = (cookie or "").strip()
        self._proxy = proxy
        self.keep_saved = bool(keep_saved)
        self._folder_fid: Optional[str] = None
        self._request = RequestUtils()

    def _headers(self, referer: str = "https://pan.quark.cn/") -> dict[str, str]:
        """构造统一请求头。"""
        headers = {
            "User-Agent": PC_UA,
            "Referer": referer,
            "Origin": "https://pan.quark.cn",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
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

    def _call(self, host: str, method: str, path: str, *, params: dict,
              payload: Optional[dict] = None, timeout: int = 20) -> dict:
        """向指定域名发起一次接口调用，失败返回空字典。

        :param host: 接口域名（HOST_PAGE / HOST_SAVE / HOST_PC）
        :param method: GET 或 POST
        :param path: 接口路径
        :param params: 查询参数
        :param payload: JSON 请求体
        :param timeout: 超时秒数
        :return: 响应 JSON；异常或非 JSON 时返回空字典
        """
        url = f"{host}{path}"
        merged = {**COMMON_PARAMS, **params}
        try:
            if method == "GET":
                response = self._request.get_res(
                    url, params=merged, headers=self._headers(), timeout=timeout,
                )
            else:
                response = self._request.post_res(
                    url, params=merged, json=payload or {}, headers=self._headers(), timeout=timeout,
                )
        except Exception as error:  # noqa: BLE001 - 网络异常按失败处理
            logger.debug(f"夸克接口 {path} 请求异常：{error}")
            return {}
        return self._json(response)

    def _proxy_opt(self) -> Optional[dict]:
        """代理参数透传。"""
        return None if self._proxy else None

    def stoken(self, pwd_id: str, passcode: str = "") -> str:
        """换取分享 stoken。

        :param pwd_id: 分享 id（URL 中 /s/ 之后的部分）
        :param passcode: 提取码，无则不传
        :return: stoken 字符串，失败返回空串
        """
        payload = {
            "pwd_id": pwd_id,
            "passcode": passcode or "",
            "support_visit_limit_private_share": True,
        }
        data = self._call(
            HOST_PAGE, "POST", "/1/clouddrive/share/sharepage/token", params={}, payload=payload,
        )
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
            "force": "0",
            "_page": page,
            "_size": 100,
            "_fetch_banner": "1",
            "_fetch_share": "1",
            "_fetch_total": "1",
            "_sort": "file_type:asc,file_name:asc",
        }
        return self._call(
            HOST_PAGE, "GET", "/1/clouddrive/share/sharepage/detail", params=params,
        )

    def snapshot(self, share_url: str, passcode: str = "", token: str = "") -> ShareSnapshot:
        """读取分享整体文件清单（递归一层目录，避免遗漏子目录里的剧集）。

        :param share_url: 分享链接
        :param passcode: 提取码
        :param token: 复用调用方已换好的 stoken，保证文件 token 与转存时一致
        :return: 分享快照；失败时抛 ValueError
        """
        pwd_id = extract_pwd_id(share_url)
        if not pwd_id:
            raise ValueError("无法从链接中解析分享 id")
        token = token or self.stoken(pwd_id, passcode)
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
                if rows and isinstance(rows[0], dict):
                    logger.info(f"分享目录行字段：{sorted(rows[0].keys())}")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    file_item = ShareFile(
                        fid=str(row.get("fid") or ""),
                        name=str(row.get("file_name") or ""),
                        size=int(row.get("size") or 0),
                        # 不同版本的 detail 可能返回 share_fid_token 或 fid_token，
                        # 转存必须带其中任意一个，否则报 41020 token 校验异常。
                        share_token=str(row.get("share_fid_token") or row.get("fid_token") or ""),
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

    def _folder_rows(self, pdir_fid: str) -> list[dict]:
        """分页列出网盘目录内容，返回原始行。

        :param pdir_fid: 目录 fid，根目录传 "0"
        :return: 目录行列表
        """
        rows: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            data = self._call(
                HOST_PC,
                "GET",
                "/1/clouddrive/file/sort",
                params={
                    "pdir_fid": pdir_fid or "0",
                    "_page": page,
                    "_size": 100,
                    "_fetch_total": 1,
                    "_sort": "file_type:asc,file_name:asc",
                },
            )
            payload = data.get("data") or {}
            if not isinstance(payload, dict):
                break
            current = payload.get("list") or []
            rows.extend(item for item in current if isinstance(item, dict))
            total = int(payload.get("count") or 0)
            if page * 100 >= total or not current:
                break
            page += 1
        return rows

    def ensure_folder(self, name: str = DEFAULT_SAVE_FOLDER) -> str:
        """确保根目录下存在转存目录，返回其 fid。

        :param name: 目录名称
        :return: 目录 fid
        """
        if self._folder_fid:
            return self._folder_fid
        for row in self._folder_rows("0"):
            if bool(row.get("dir")) and str(row.get("file_name") or "") == name:
                fid = str(row.get("fid") or "")
                if fid:
                    self._folder_fid = fid
                    return fid
        data = self._call(
            HOST_PC,
            "POST",
            "/1/clouddrive/file",
            params={},
            payload={
                "pdir_fid": "0",
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": False,
            },
        )
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        fid = str(payload.get("fid") or "")
        if not fid:
            raise ValueError("创建网盘转存目录失败")
        self._folder_fid = fid
        return fid

    @staticmethod
    def _file_meta(item: Any) -> tuple[str, str, str]:
        """从 ShareFile、字典或任务文件对象提取 (fid, token, 文件名)。"""
        if isinstance(item, ShareFile):
            return item.fid, item.share_token, item.name
        if isinstance(item, dict):
            return (
                str(item.get("fid") or ""),
                str(item.get("share_token") or ""),
                str(item.get("name") or ""),
            )
        return (
            str(getattr(item, "fid", "") or ""),
            str(getattr(item, "share_token", "") or ""),
            str(getattr(item, "name", "") or ""),
        )

    @staticmethod
    def _extract_top_fids(data: Any) -> list[str]:
        """递归查找 save_as_top_fids，按出现顺序返回去重后的 fid。"""
        found: list[str] = []
        if isinstance(data, dict):
            value = data.get("save_as_top_fids")
            if isinstance(value, list):
                found.extend(str(item) for item in value if item)
            for child in data.values():
                if isinstance(child, (dict, list)):
                    found.extend(QuarkShareClient._extract_top_fids(child))
        elif isinstance(data, list):
            for child in data:
                if isinstance(child, (dict, list)):
                    found.extend(QuarkShareClient._extract_top_fids(child))
        return list(dict.fromkeys(found))

    def _wait_task(self, task_id: str) -> dict:
        """轮询转存任务，直到 data.status 为 2。

        :param task_id: 转存任务 id
        :return: status=2 时的任务响应
        """
        deadline = time.monotonic() + TASK_POLL_TIMEOUT
        while True:
            data = self._call(
                HOST_PC, "GET", "/1/clouddrive/task",
                params={"task_id": task_id, "retry_index": 0},
            )
            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            raw_status = payload.get("status", data.get("status"))
            try:
                status = int(raw_status) if raw_status is not None else None
            except (TypeError, ValueError):
                status = None
            if status == 2:
                return data
            if status not in (None, 0, 1):
                raise ValueError(f"转存任务失败：{data.get('message') or status}")
            if time.monotonic() >= deadline:
                raise ValueError("转存任务超时")
            time.sleep(TASK_POLL_INTERVAL)

    def _match_folder_fids(self, names: list[str]) -> list[str]:
        """缺少 save_as_top_fids 时，按文件名从转存目录反查 fid。"""
        available: dict[str, list[str]] = {}
        for row in self._folder_rows(self._folder_fid or ""):
            if bool(row.get("dir")):
                continue
            name = str(row.get("file_name") or "")
            fid = str(row.get("fid") or "")
            if name and fid:
                available.setdefault(name, []).append(fid)
        logger.info(
            f"转存目录现有文件：{[row.get('file_name') for row in self._folder_rows(self._folder_fid or '')][:20]}"
        )
        result: list[str] = []
        for name in names:
            choices = available.get(name) or []
            if not choices:
                raise ValueError(f"转存后未找到文件：{name}")
            result.append(choices.pop(0))
        return result

    def save_files(self, pwd_id: str, stoken: str, files: list[Any]) -> list[str]:
        """把分享文件分批转存到本插件目录，返回转存后的 fid。

        :param pwd_id: 分享 id
        :param stoken: 分享 stoken
        :param files: ShareFile 或包含 fid/share_token/name 的对象
        :return: 转存后的 fid 列表，顺序与入参一致
        """
        metas = [self._file_meta(item) for item in files]
        if not metas:
            return []
        folder_fid = self.ensure_folder()
        saved: list[str] = []
        for start in range(0, len(metas), SAVE_BATCH_SIZE):
            batch = metas[start:start + SAVE_BATCH_SIZE]
            names = [item[2] for item in batch]
            payload = {
                "fid_list": [item[0] for item in batch],
                "fid_token_list": [item[1] for item in batch],
                "to_pdir_fid": folder_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "scene": "link",
            }
            data = self._call(
                HOST_SAVE, "POST", "/1/clouddrive/share/sharepage/save", params={}, payload=payload,
            )
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            logger.info(
                f"转存请求返回：status={data.get('status')} code={data.get('code')} "
                f"message={data.get('message')} 键={sorted(body.keys())}"
            )
            task_id = str(body.get("task_id") or "")
            batch_saved = self._extract_top_fids(data)
            if task_id:
                task_data = self._wait_task(task_id)
                batch_saved = self._extract_top_fids(task_data) or batch_saved
            if len(batch_saved) != len(batch):
                logger.info(
                    f"转存响应未给出完整 fid（{len(batch_saved)}/{len(batch)}），"
                    f"响应键={sorted((data.get('data') or {}).keys())}，按目录名兜底"
                )
                batch_saved = self._match_folder_fids(names)
            saved.extend(batch_saved[:len(batch)])
        return saved

    def download_url_by_fid(self, fid: str) -> tuple[str, dict]:
        """用自己网盘中的文件 fid 换取直链。

        :param fid: 转存后的文件 fid
        :return: (下载地址, 需要携带的请求头)
        """
        data = self._call(
            HOST_PC, "POST", "/1/clouddrive/file/download", params={}, payload={"fids": [fid]},
        )
        rows = data.get("data")
        entry = rows[0] if isinstance(rows, list) and rows else {}
        url = str((entry or {}).get("download_url") or "")
        if not url:
            message = str(data.get("message") or "")
            raise ValueError(f"夸克未返回下载地址：{message or '未知原因'}")
        headers = {
            "User-Agent": PC_UA,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        time.sleep(0.2)
        return url, headers

    def delete_files(self, fids: list[str]) -> bool:
        """删除自己网盘中的副本，失败只记录 warning。

        :param fids: 待删除文件 fid 列表
        :return: 是否删除成功
        """
        items = [str(item) for item in fids if item]
        if not items:
            return True
        try:
            data = self._call(
                HOST_PC,
                "POST",
                "/1/clouddrive/file/delete",
                params={},
                payload={"filelist": items, "action_type": 2},
            )
            if int(data.get("status") or 0) != 200:
                logger.warning(f"删除网盘副本失败：{data.get('message') or data.get('code')}")
                return False
            return True
        except Exception as error:  # noqa: BLE001 - 清理失败不阻断下载
            logger.warning(f"删除网盘副本异常：{error}")
            return False

    def download_url(self, share_url: str, passcode: str, file_item: ShareFile,
                     keep_saved: bool = False) -> tuple[str, dict]:
        """转存单个分享文件后取直链。

        :param share_url: 分享链接
        :param passcode: 提取码
        :param file_item: 分享文件
        :param keep_saved: 取链成功后是否保留网盘副本
        :return: (下载地址, 需要携带的请求头)
        """
        pwd_id = extract_pwd_id(share_url)
        token = self.stoken(pwd_id, passcode)
        if not token:
            raise ValueError("分享不可用，无法取链")
        saved = self.save_files(pwd_id, token, [file_item])
        if not saved:
            raise ValueError("转存失败，无法取链")
        url, headers = self.download_url_by_fid(saved[0])
        if not keep_saved:
            self.delete_files(saved)
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
