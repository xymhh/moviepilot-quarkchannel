"""aria2c RPC 客户端与本地进程启动器。

插件只连接自己启动的 aria2c 实例（默认 6805 端口），
不读取、不停止容器内其他插件启动的 aria2c。
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

DEFAULT_BINARY = "/downloads/.aria2/bin/aria2c"
DEFAULT_PORT = 6805
PROBE_TIMEOUT = 10.0
PROBE_INTERVAL = 0.2


class Aria2Rpc:
    """最小 aria2 JSON-RPC 客户端。"""

    def __init__(self, rpc_url: str, secret: str) -> None:
        """保存 RPC 地址与密钥。

        :param rpc_url: 例如 http://127.0.0.1:6805/jsonrpc
        :param secret: aria2 rpc-secret
        """
        self.rpc_url = rpc_url
        self.secret = secret

    def _call(self, method: str, params: list[Any]) -> Any:
        """调用 JSON-RPC 方法并返回 result；异常或非 JSON 返回 None。"""
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        try:
            response = RequestUtils().post_res(self.rpc_url, json=body)
        except Exception as error:  # noqa: BLE001 - RPC 不可用时由调用方回落
            logger.debug(f"aria2 RPC {method} 请求异常：{type(error).__name__}")
            return None
        if response is None:
            return None
        if isinstance(response, dict):
            payload = response
        else:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应按失败处理
                return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            logger.warning(f"aria2 RPC {method} 失败：{error.get('message') or error.get('code')}")
            return None
        return payload.get("result")

    def version(self) -> Optional[dict]:
        """返回 aria2 版本信息。"""
        result = self._call("aria2.getVersion", [])
        return result if isinstance(result, dict) else None

    def add_uri(self, url: str, options: dict) -> Optional[str]:
        """添加 HTTP 下载任务，返回 gid。"""
        result = self._call("aria2.addUri", [[url], options or {}])
        return str(result) if result else None

    def tell_active(self) -> list[dict]:
        """返回当前正在下载的任务列表。

        :return: aria2 任务字典列表
        """
        result = self._call("aria2.tellActive", [])
        return result if isinstance(result, list) else []

    def tell_waiting(self, offset: int = 0, num: int = 100) -> list[dict]:
        """返回等待队列中的任务。

        :param offset: 起始偏移
        :param num: 数量
        :return: aria2 任务字典列表
        """
        result = self._call("aria2.tellWaiting", [offset, num])
        return result if isinstance(result, list) else []

    def tell_status(self, gid: str) -> Optional[dict]:
        """查询任务状态。"""
        result = self._call("aria2.tellStatus", [gid])
        return result if isinstance(result, dict) else None

    def pause(self, gid: str) -> Optional[str]:
        """暂停任务。"""
        result = self._call("aria2.pause", [gid])
        return str(result) if result else None

    def unpause(self, gid: str) -> Optional[str]:
        """继续任务。"""
        result = self._call("aria2.unpause", [gid])
        return str(result) if result else None

    def remove(self, gid: str) -> Optional[str]:
        """移除任务。"""
        result = self._call("aria2.remove", [gid])
        return str(result) if result else None

    def global_stat(self) -> Optional[dict]:
        """返回全局下载统计。"""
        result = self._call("aria2.getGlobalStat", [])
        return result if isinstance(result, dict) else None


class Aria2Launcher:
    """启动并探测本插件专属的 aria2c 进程。"""

    def __init__(self, data_dir: Path, dir_root: str, secret: str,
                 binary: str = DEFAULT_BINARY, port: int = DEFAULT_PORT) -> None:
        """保存启动参数。

        :param data_dir: 插件数据目录，用于存放 aria2 日志
        :param dir_root: aria2 默认下载根目录
        :param secret: 本实例专属 rpc-secret
        :param binary: aria2c 可执行文件路径
        :param port: RPC 监听端口
        """
        self.binary = binary
        self._resolved_binary: Optional[str] = None
        self.port = int(port)
        self.dir_root = str(dir_root or "/downloads")
        self.secret = secret
        self.rpc_url = f"http://127.0.0.1:{self.port}/jsonrpc"
        self.rpc = Aria2Rpc(self.rpc_url, secret)
        self._data_dir = Path(data_dir)
        self._process: Optional[subprocess.Popen] = None

    def _resolve_binary(self) -> str:
        """在多个候选路径中查找 aria2c 可执行文件。

        :return: 可执行文件路径
        :raises FileNotFoundError: 所有候选都不存在
        """
        import shutil

        candidates = [
            self.binary,
            str(self._data_dir / "bin" / "aria2c"),
            "/config/plugins/CloudSubscribeX/bin/aria2c",
            "/config/plugins/cloudsubscribex/bin/aria2c",
            "/usr/local/bin/aria2c",
            "/usr/bin/aria2c",
        ]
        found = shutil.which("aria2c")
        if found:
            candidates.append(found)
        for path in candidates:
            if path and Path(path).exists():
                return path
        raise FileNotFoundError(f"aria2c 不存在，已尝试：{candidates}")

    def _command(self) -> list[str]:
        """构造 aria2c 启动参数；密钥只在进程参数中使用。"""
        if self._resolved_binary is None:
            self._resolved_binary = self._resolve_binary()
        log_path = self._data_dir / "aria2.log"
        return [
            self._resolved_binary,
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            f"--dir={self.dir_root}",
            "--continue=true",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--file-allocation=none",
            "--max-connection-per-server=64",
            "--split=64",
            "--min-split-size=1M",
            # 与生产实现对齐：磁盘缓存与并发上限对速度影响最大
            "--disk-cache=32M",
            "--max-concurrent-downloads=2",
            "--check-certificate=true",
            "--follow-torrent=false",
            "--enable-dht=false",
            "--bt-enable-lpd=false",
            "--console-log-level=warn",
            "--log-level=warn",
            "--summary-interval=0",
            "--quiet=true",
            "--disable-ipv6",
            f"--log={log_path}",
        ]

    def _start(self) -> bool:
        """启动子进程，失败返回 False。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._process = subprocess.Popen(
                self._command(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:  # noqa: BLE001 - 启动失败交由上层回落
            logger.warning(f"aria2c 启动失败：{type(error).__name__}")
            self._process = None
            return False
        return True

    def ensure(self) -> bool:
        """确保 RPC 可用；已有实例直接复用，必要时启动并等待最多 10 秒。"""
        if self.rpc.version() is not None:
            return True
        process = self._process
        if process is None or process.poll() is not None:
            if not self._start():
                return False
        deadline = time.monotonic() + PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if self.rpc.version() is not None:
                return True
            time.sleep(PROBE_INTERVAL)
        logger.warning("aria2c 启动后 RPC 探测超时")
        return False

    def stop(self) -> None:
        """停止本插件启动的 aria2c；不触碰其他进程，且日志不输出密钥。"""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception as error:  # noqa: BLE001 - 停止失败不向上抛
            logger.warning(f"停止 aria2c 失败：{type(error).__name__}")
            try:
                process.kill()
            except Exception:  # noqa: BLE001 - 已退出或无法终止
                pass
