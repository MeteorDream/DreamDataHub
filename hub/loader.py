"""Loader — 从 config 字典还原模块清单。

约定：
- config 顶层 ``[module.<name>]`` 列出每个模块的配置；``enabled = false`` 跳过
- 模块文件路径固定为 ``module/<name>.py``
- 每个模块文件顶层声明**有且只有一个** ``Module`` 实例，loader 扫 globals 拿到
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from hub.module import Module

logger = logging.getLogger("hub.loader")


def load_modules(config: dict[str, Any]) -> list[Module]:
    """根据 config 加载所有启用的模块，并把每段配置绑到对应 Module。"""
    section = config.get("module") or {}
    if not isinstance(section, dict):
        raise TypeError("config.module must be a table")

    out: list[Module] = []
    for name, params in section.items():
        if not isinstance(params, dict):
            raise TypeError(f"config.module.{name} must be a table")
        if not params.get("enabled", False):
            logger.info("module %s: disabled, skip", name)
            continue
        # 动态加载导入模块并查找 Module 实例
        py_mod = importlib.import_module(f"module.{name}")
        candidates = [v for v in vars(py_mod).values() if isinstance(v, Module)]
        if len(candidates) != 1:
            raise RuntimeError(
                f"module/{name}.py must declare exactly one Module instance, got {len(candidates)}"
            )
        m = candidates[0]
        if m.name != name:
            logger.warning(
                "module/%s.py declares Module(%r); using config name %r as canonical",
                name,
                m.name,
                name,
            )
        m.bind_config(params)
        out.append(m)
        logger.info("module %s: loaded (%s)", name, m)
    return out
