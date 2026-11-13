"""pytest 入口：安装宿主模块桩。"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for item in (str(ROOT / 'plugins.v3'), str(HERE)):
    if item not in sys.path:
        sys.path.insert(0, item)

import _stubs  # noqa: E402

_stubs._install_stubs()
