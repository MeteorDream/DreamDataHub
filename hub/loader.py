"""Loader — 从 config 字典还原模块和 workflow 清单。

约定：
- config 顶层 ``[module.<name>]`` 列出每个模块的配置；``enabled = false`` 跳过
- 模块文件路径固定为 ``module/<name>.py``
- 每个模块文件顶层声明**有且只有一个** ``Module`` 实例，loader 扫 globals 拿到
- config 顶层 ``[workflow.<name>]`` 列出每个 workflow 的配置；``enabled = false`` 跳过
- workflow 文件路径固定为 ``workflow/<name>.py``
- 每个 workflow 文件导出 ``workflow`` 变量（``Workflow`` 实例）
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from hub.module import Module
from hub.workflow import Workflow
from hub.core import Hub

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


def load_workflows(config: dict[str, Any], hub: Hub) -> list[Workflow]:
    """根据 config 加载所有启用的 workflow，并注册到 hub。"""
    section = config.get("workflow") or {}
    if not isinstance(section, dict):
        raise TypeError("config.workflow must be a table")


    out: list[Workflow] = []
    for name, params in section.items():
        if not isinstance(params, dict):
            raise TypeError(f"config.workflow.{name} must be a table")
        if not params.get("enabled", False):
            logger.info("workflow %s: disabled, skip", name)
            continue
        # 动态加载导入 workflow 模块
        try:
            py_mod = importlib.import_module(f"workflow.{name}")
        except ModuleNotFoundError:
            logger.error("workflow %s: workflow/%s.py not found", name, name)
            continue
        candidates = [v for v in vars(py_mod).values() if isinstance(v, Workflow)]
        if len(candidates) != 1:
            raise RuntimeError(
                f"workflow/{name}.py must declare exactly one Workflow instance, "
                f"got {len(candidates)}"
            )
        wf = candidates[0]
        if wf.name != name:
            logger.warning(
                "workflow/%s.py: workflow name is %r; using config name %r",
                name,
                wf.name,
                name,
            )
            wf.name = name
        # config 覆盖 workflow 字段
        wf.bind_config(params)
        hub.register_workflow(wf)
        out.append(wf)
        logger.info("workflow %s: loaded (subscribe=%s)", name, wf.subscribe)
    return out
