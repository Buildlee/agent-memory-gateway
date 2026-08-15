"""让直接执行的运维脚本优先使用当前仓库源码。"""

from __future__ import annotations

import sys
from pathlib import Path


def prefer_checkout_source(script_file: str | Path) -> None:
    """将仓库 ``src`` 置于导入路径首位，避免误用环境中的旧安装包。

    注意不能只检查 ``source not in sys.path``：editable 安装会把
    ``src`` 经 .pth 追加到 sys.path 尾部，排在 PYTHONPATH 之后，
    此时已存在但位置错误，必须移除后重新插到最前。
    """

    source_root = Path(script_file).resolve().parents[1] / "src"
    if not source_root.is_dir():
        return
    source = str(source_root)
    if source in sys.path:
        sys.path.remove(source)
    sys.path.insert(0, source)
