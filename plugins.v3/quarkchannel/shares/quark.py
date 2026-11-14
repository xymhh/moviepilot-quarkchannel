"""夸克分享取链客户端。

只做三件事：换 stoken、列分享目录、取文件直链。
请求统一走宿主 `app.sdk.network.RequestUtils`，不自行管理连接池。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

# 夸克按 UA 区分「游客 / 客户端 / 网页」三档：换链与下载必须使用同一 UA，
# 客户端 UA 可以绕过网页端的单文件大小限制（download file size limit）。
CLIENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
)
CLIENT_PARAMS = {"sys": "win32", "ve": "2.5.20", "ut": "", "guid": ""}
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

    def __init__(self, cookie: str = "", proxy: bool = False, keep_saved: bool = False,
                 on_cookie_refresh: Optional[Callable[[str], None]] = None) -> None:
        """保存凭据、代理开关与副本保留策略。

        :param cookie: 可选，带上自己的账号 cookie 可降低匿名请求被限的概率
        :param proxy: 是否走系统代理
        :param keep_saved: 下载完成后是否保留转存到网盘的副本
        """
        self._cookie = (cookie or "").strip()
        self._on_cookie_refresh = on_cookie_refresh
        self._proxy = proxy
        self.keep_saved = bool(keep_saved)
        self._folder_fid: Optional[str] = None
        self._last_pwd_id: str = ""
        self._risk_failures = 0
        self._risk_blocked_until = 0.0
        self._request = RequestUtils()

    def _headers(self, referer: str = "https://pan.quark.cn/",
                 user_agent: str = "") -> dict[str, str]:
        """构造统一请求头。

        :param referer: Referer 头
        :param user_agent: 覆盖 UA，用于切换游客/客户端通道
        """
        headers = {
            "User-Agent": user_agent or PC_UA,
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
              payload: Optional[dict] = None, timeout: int = 20,
              user_agent: str = "", merge_common: bool = True) -> dict:
        """向指定域名发起一次接口调用，失败返回空字典。

        :param host: 接口域名（HOST_PAGE / HOST_SAVE / HOST_PC）
        :param method: GET 或 POST
        :param path: 接口路径
        :param params: 查询参数
        :param payload: JSON 请求体
        :param timeout: 超时秒数
        :param user_agent: 覆盖 UA，空值使用默认浏览器 UA
        :return: 响应 JSON；异常或非 JSON 时返回空字典
        """
        url = f"{host}{path}"
        merged = {**COMMON_PARAMS, **params} if merge_common else dict(params)
        try:
            if method == "GET":
                response = self._request.get_res(
                    url, params=merged, headers=self._headers(user_agent=user_agent),
                    timeout=timeout, raise_exception=True,
                )
            else:
                response = self._request.post_res(
                    url, params=merged, json=payload or {},
                    headers=self._headers(user_agent=user_agent), timeout=timeout,
                    raise_exception=True,
                )
        except Exception as error:  # noqa: BLE001 - 宿主客户端异常时改用独立连接重试
            logger.warning(
                f"夸克接口 {path} 请求异常：{type(error).__name__}: {str(error)[:120]}，"
                f"改用独立连接重试"
            )
            try:
                response = self._session().request(
                    method.upper(), url, params=merged,
                    json=payload if method != "GET" else None,
                    headers=self._headers(user_agent=user_agent),
                    timeout=(5, timeout),
                )
                return self._json(response)
            except Exception as retry_error:  # noqa: BLE001 - 重试仍失败按失败处理
                logger.warning(
                    f"夸克接口 {path} 独立连接重试失败："
                    f"{type(retry_error).__name__}: {str(retry_error)[:120]}"
                )
                return {}
        self._refresh_cookie(response)
        payload = self._json(response)
        status_code = getattr(response, "status_code", None)
        if status_code == 401:
            logger.warning(
                "夸克接口返回 401：夸克 Cookie 已失效或缺少登录态，"
                "请在插件设置 → 下载 中更新 Cookie"
            )
        if not payload:
            status = getattr(response, "status_code", None)
            snippet = ""
            try:
                snippet = (getattr(response, "text", "") or "")[:120].replace("\n", " ")
            except Exception:  # noqa: BLE001 - 诊断字段读取失败不影响主流程
                snippet = ""
            logger.warning(f"夸克接口 {path} 返回空 JSON：status={status} body={snippet}")
        return payload

    def _refresh_cookie(self, response: Any) -> None:
        """按响应的 Set-Cookie 刷新 __puus，保持会话有效。

        :param response: 宿主/独立客户端返回的响应对象
        """
        try:
            raw = getattr(response, "headers", {}) or {}
            values = raw.get("Set-Cookie") or raw.get("set-cookie") or []
            if isinstance(values, str):
                values = [values]
            for item in values:
                if not str(item).startswith("__puus"):
                    continue
                pair = str(item).split(";")[0]
                parts = [seg.strip() for seg in self._cookie.split(";") if seg.strip()]
                replaced = False
                for index, seg in enumerate(parts):
                    if seg.startswith("__puus="):
                        parts[index] = pair
                        replaced = True
                        break
                if not replaced:
                    parts.append(pair)
                self._cookie = "; ".join(parts)
                if self._on_cookie_refresh:
                    try:
                        self._on_cookie_refresh(self._cookie)
                    except Exception:  # noqa: BLE001 - 回写失败不影响请求
                        pass
                logger.info("夸克 Cookie 已按 Set-Cookie 刷新 __puus")
                break
        except Exception:  # noqa: BLE001 - 刷新失败不影响主流程
            return

    def mark_risk(self) -> None:
        """记录一次 412 风控命中；连续 3 次进入 30 分钟冷却。"""
        self._risk_failures += 1
        if self._risk_failures >= 3:
            self._risk_blocked_until = time.monotonic() + 1800
            self._risk_failures = 0
            logger.warning("夸克直链连续 3 次 412，暂停取链 30 分钟")

    def _session(self) -> Any:
        """返回独立的 requests 会话，避免复用宿主连接池。

        :return: requests.Session 实例
        """
        session = getattr(self, "_own_session", None)
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = True
            self._own_session = session
        return session

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
        self._last_pwd_id = pwd_id
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

    def _walk_files(self, pdir_fid: str, depth: int = 0) -> list[dict]:
        """递归列出目录下的文件（最多 3 层），返回文件行。

        :param pdir_fid: 起始目录 fid
        :param depth: 当前深度
        :return: 文件行列表
        """
        if depth > 3:
            return []
        files: list[dict] = []
        for row in self._folder_rows(pdir_fid):
            if bool(row.get("dir")):
                child = str(row.get("fid") or "")
                if child:
                    files.extend(self._walk_files(child, depth + 1))
                continue
            files.append(row)
        return files

    def _match_folder_fids(self, names: list[str]) -> list[str]:
        """按文件名从转存目录递归反查文件 fid（跳过目录）。"""
        available: dict[str, list[str]] = {}
        for row in self._walk_files(self._folder_fid or ""):
            name = str(row.get("file_name") or "")
            fid = str(row.get("fid") or "")
            if name and fid:
                available.setdefault(name, []).append(fid)
        rows = self._folder_rows(self._folder_fid or "")
        logger.info(
            f"转存目录 fid={self._folder_fid} 条目={len(rows)} "
            f"名称={[row.get('file_name') for row in rows][:10]}"
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
                task_body = task_data.get("data") if isinstance(task_data.get("data"), dict) else {}
                logger.info(
                    f"转存任务完成：键={sorted(task_body.keys())} "
                    f"fid数={len(self._extract_top_fids(task_data))}"
                )
                batch_saved = self._extract_top_fids(task_data) or batch_saved
            # 以「转存目录里真实存在的文件」为准：save_as 返回的 fid 在刚完成时
            # 可能短暂不可用（21001 file not found），缺 fid 时轮询目录补齐。
            try:
                verified = self._match_folder_fids(names)
            except ValueError:
                verified = []
            if len(verified) != len(batch) and len(batch_saved) != len(batch):
                logger.info(
                    f"转存响应未给出完整 fid（{len(batch_saved)}/{len(batch)}），"
                    f"按目录名轮询补齐"
                )
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        verified = self._match_folder_fids(names)
                    except ValueError:
                        verified = []
                    if len(verified) == len(batch):
                        break
                    time.sleep(1.5)
            if len(verified) == len(batch):
                batch_saved = verified
            elif len(batch_saved) != len(batch):
                batch_saved = self._match_folder_fids(names)
            saved.extend(batch_saved[:len(batch)])
        return saved

    def resolve_saved_fid(self, name: str) -> str:
        """按文件名在转存目录里重新解析 fid。

        :param name: 文件名
        :return: 目录中的 fid；找不到返回空串
        """
        rows = self._folder_rows(self._folder_fid or "")
        for row in rows:
            if not bool(row.get("dir")) and str(row.get("file_name") or "") == name:
                return str(row.get("fid") or "")
        return ""

    def download_url_by_fid(self, fid: str, name: str = "", retries: int = 3) -> tuple[str, dict]:
        """用自己网盘中的文件 fid 换取直链。

        转存刚完成时 fid 可能短暂不可用（21001 file not found），
        因此支持按文件名重新解析并重试。

        :param fid: 转存后的文件 fid
        :param name: 文件名，用于 21001 时重新解析
        :param retries: 重试次数
        :return: (下载地址, 需要携带的请求头)
        """
        if time.monotonic() < self._risk_blocked_until:
            remain = int(self._risk_blocked_until - time.monotonic())
            raise ValueError(f"夸克直链被风控限制，冷却中（剩余 {remain} 秒）")
        data: dict = {}
        url = ""
        used_ua = CLIENT_UA
        attempts: list[tuple[str, str, dict, str, bool]] = (
            # 先用不带公共参数的形态（历史可用形态），失败再带 pr/fr。
            ("drive-pc/客户端-UA", HOST_PC, {}, CLIENT_UA, False),
            ("drive-pc/客户端-UA+参数", HOST_PC, {}, CLIENT_UA, True),
            ("drive-pc/网页-UA+参数", HOST_PC, {}, PC_UA, True),
        )
        for attempt in range(max(1, retries)):
            for channel, host, params, candidate_ua, merge_common in attempts:
                data = self._call(
                    host, "POST", "/1/clouddrive/file/download", params=params,
                    payload={"fids": [fid]}, user_agent=candidate_ua,
                    merge_common=merge_common,
                )
                rows = data.get("data")
                entry = rows[0] if isinstance(rows, list) and rows else {}
                url = str((entry or {}).get("download_url") or "")
                if url:
                    used_ua = candidate_ua
                    logger.info(f"夸克取链成功：{channel}")
                    break
                logger.info(
                    f"夸克取链通道 {channel} 未返回地址："
                    f"code={data.get('code')} {data.get('message') or ''}".strip()
                )
            if url:
                break
            message = str(data.get("message") or "")
            if "file not found" not in message.lower() or attempt == max(1, retries) - 1:
                break
            time.sleep(1.5)
            if name:
                refreshed = self.resolve_saved_fid(name)
                if refreshed and refreshed != fid:
                    logger.info(f"转存 fid 暂不可用，改用目录中的新 fid：{refreshed}")
                    fid = refreshed
        if not url:
            # 传进来的可能是目录 fid：展开成文件后重试一次
            children = []
            try:
                children = [str(row.get("fid") or "") for row in self._walk_files(fid) if row.get("fid")]
            except Exception:  # noqa: BLE001 - 展开失败按原错误返回
                children = []
            self._risk_failures += 1
            if self._risk_failures >= 3:
                self._risk_blocked_until = time.monotonic() + 1800
                self._risk_failures = 0
                logger.warning(
                    "夸克直链连续 3 次拿不到地址（多为 412 风控），暂停取链 30 分钟"
                )
            if children:
                logger.info(f"取链 fid 为目录，改用其中 {len(children)} 个文件的第一个")
                return self.download_url_by_fid(children[0], name=name, retries=1)
            message = str(data.get("message") or "")
            raise ValueError(f"夸克未返回下载地址：{message or '未知原因'}")
        headers = {
            "User-Agent": used_ua,
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
