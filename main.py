"""DataHub 启动入口。

``python main.py`` 会：
1. 读取 ``config.toml``（可用 ``DATAHUB_CONFIG`` 环境变量覆盖路径）
2. 初始化日志
3. 按 config 加载启用的模块和 workflow
4. 启动 Hub 主循环；Ctrl+C 时优雅关停
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tomllib
from pathlib import Path

from hub import Hub, load_modules
from utils.logger import init_logging

logger = logging.getLogger("main")
# httpx 设置为 warning 避免日志有大量的请求日志
logging.getLogger("httpx").setLevel(logging.WARNING)


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


async def amain() -> None:
    config_path = Path(os.environ.get("DATAHUB_CONFIG", "config.toml"))
    config = _load_config(config_path)

    hub_cfg = config.get("hub", {})
    init_logging(
        level=getattr(logging, hub_cfg.get("log_level", "INFO").upper(), logging.INFO),
        log_dir=hub_cfg.get("log_dir", "log"),
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )
    logger.info("DataHub starting (config=%s)", config_path.resolve())

    hub = Hub()
    for module in load_modules(config):
        hub.register(module)

    await hub.run()
    logger.info("DataHub stopped cleanly")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        # Windows 上 SIGINT 走这里；POSIX 上由信号处理器接走
        sys.exit(0)


if __name__ == "__main__":
    main()
