"""宿主模块桩（测试专用）。

真实运行时这些符号由 MoviePilot 提供；这里只为了让纯逻辑离线可测。
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

_DATA_DIR = Path(tempfile.mkdtemp(prefix="qc-test-"))


class _Logger:
    """空日志实现。"""

    def debug(self, *args, **kwargs):
        """忽略 debug 日志。"""

    def info(self, *args, **kwargs):
        """忽略 info 日志。"""

    def warning(self, *args, **kwargs):
        """忽略 warning 日志。"""

    def error(self, *args, **kwargs):
        """忽略 error 日志。"""


class _RequestUtils:
    """HTTP 桩：测试不得访问网络。"""

    def get_res(self, *args, **kwargs):
        """返回空响应。"""
        return None

    def post_res(self, *args, **kwargs):
        """返回空响应。"""
        return None


class _PluginBase:
    """插件基类桩。"""

    plugin_name = ""
    plugin_desc = ""
    plugin_version = "0.0.0"
    plugin_icon = ""
    plugin_author = ""
    author_url = ""
    plugin_config_prefix = ""
    plugin_order = 999
    auth_level = 1

    def get_data_path(self) -> Path:
        """返回临时数据目录。"""
        return _DATA_DIR

    def update_config(self, config):
        """忽略配置回写。"""
        return None

    def post_message(self, *args, **kwargs):
        """忽略通知。"""


class _TorrentInfo:
    """种子信息桩。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _DownloaderTorrent:
    """下载任务桩。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _DownloaderFile:
    """下载文件桩。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MediaType:
    """媒体类型桩。"""

    MOVIE = "电影"
    TV = "电视剧"


class _TorrentQueryStatus:
    """查询状态桩。"""

    ALL = "all"


class _Response:
    """响应桩。"""

    def __init__(self, content=b"", status_code=200, media_type=None, headers=None):
        self.content = content
        self.status_code = status_code
        self.media_type = media_type
        self.headers = headers or {}


def _module(name: str) -> types.ModuleType:
    """创建并登记一个空模块。"""
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    """安装宿主模块桩，重复调用安全。"""
    if "app.sdk.logging" in sys.modules:
        return

    app = _module("app")
    sdk = _module("app.sdk")
    app.sdk = sdk

    logging_module = _module("app.sdk.logging")
    logging_module.logger = _Logger()
    sdk.logging = logging_module

    network_module = _module("app.sdk.network")
    network_module.RequestUtils = _RequestUtils
    sdk.network = network_module

    plugin_module = _module("app.sdk.plugin")
    plugin_module._PluginBase = _PluginBase
    sdk.plugin = plugin_module

    schemas = _module("app.schemas")
    schemas.DOWNLOAD_TASK_ALREADY_EXISTS = "下载任务已存在"
    schemas.TorrentInfo = _TorrentInfo
    schemas.DownloaderTorrent = _DownloaderTorrent
    schemas.DownloaderFile = _DownloaderFile
    app.schemas = schemas

    domain = _module("app.domain")
    context = _module("app.domain.context")
    context.TorrentInfo = _TorrentInfo
    domain.context = context
    app.domain = domain

    types_module = _module("app.schemas.types")
    types_module.MediaType = _MediaType
    types_module.TorrentQueryStatus = _TorrentQueryStatus
    schemas.types = types_module

    fastapi = _module("fastapi")
    responses = _module("fastapi.responses")
    responses.Response = _Response
    fastapi.responses = responses
