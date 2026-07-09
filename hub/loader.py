"""Loader — 从 config 字典还原模块清单，并按依赖关系拓扑排序。

约定：
- config 顶层 ``[module.<name>]`` 列出每个模块的配置；``enabled = false`` 跳过
- 模块文件路径固定为 ``module/<name>.py``
- 每个模块文件顶层声明**有且只有一个** ``Module`` 实例，loader 扫 globals 拿到

依赖处理（严格模式）：
- 若模块声明了 ``requires=[SomeCap]``，则必须有另一个已启用模块 ``@mod.provides(SomeCap)``
- 缺失依赖 → RuntimeError，拒绝启动
- 依赖构成 DAG，loader 做 Kahn 拓扑排序：提供者先启动，依赖者后启动
- 检测到循环依赖 → RuntimeError
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from hub.capabilities import Capability
from hub.module import Module

logger = logging.getLogger("hub.loader")


def load_modules(config: dict[str, Any]) -> list[Module]:
    """根据 config 加载所有启用的模块，并按依赖拓扑排序返回。

    抛出:
        RuntimeError: 模块声明的 ``requires`` 没有对应的 ``provides``，或存在
                     循环依赖，或同一 Capability 被多个模块 provides。
        TypeError: config 结构错误。
    """
    section = config.get("module") or {}
    if not isinstance(section, dict):
        raise TypeError("config.module must be a table")

    loaded: list[Module] = []
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
        loaded.append(m)
        logger.info("module %s: loaded (%s)", name, m)

    if not loaded:
        return []

    return _resolve_dependencies(loaded)


def _resolve_dependencies(modules: list[Module]) -> list[Module]:
    """严格校验依赖并做 Kahn 拓扑排序。

    步骤：
    1. 构造 provided: Capability → Module；重复 provide 直接抛
    2. 检查每个模块的 requires 是否都被覆盖；缺失即抛
    3. 构造有向图 provider → requirer；Kahn 排序
    4. 拓扑序断裂 → 存在环 → 抛
    """
    # 1. 反向索引：capability → 提供它的模块
    provided: dict[type[Capability], Module] = {}
    for m in modules:
        for cap in m.capabilities:
            if cap in provided:
                raise RuntimeError(
                    f"capability {cap.__name__} provided by both "
                    f"{provided[cap].name!r} and {m.name!r}"
                )
            provided[cap] = m

    # 2. 严格依赖校验
    for m in modules:
        for cap in m.requires:
            if cap not in provided:
                enabled_names = ", ".join(mod.name for mod in modules)
                raise RuntimeError(
                    f"module {m.name!r} requires capability {cap.__name__} "
                    f"but no enabled module provides it "
                    f"(enabled modules: {enabled_names})"
                )

    # 3. Kahn 拓扑排序
    # 边：provider_module_name → set of requirer_module_name
    edges: dict[str, set[str]] = {m.name: set() for m in modules}
    in_degree: dict[str, int] = dict.fromkeys((m.name for m in modules), 0)
    by_name: dict[str, Module] = {m.name: m for m in modules}

    for m in modules:
        for cap in m.requires:
            provider = provided[cap]
            if provider is m:
                # 模块 requires 自己 provides 的能力：等价于无依赖
                continue
            if m.name not in edges[provider.name]:
                edges[provider.name].add(m.name)
                in_degree[m.name] += 1

    # 保持相对稳定：初始 config 中出现顺序作为排序 tie-break
    queue: list[str] = [m.name for m in modules if in_degree[m.name] == 0]
    ordered: list[Module] = []
    while queue:
        name = queue.pop(0)
        ordered.append(by_name[name])
        # 稳定顺序：按原始 config 序遍历后继
        successors = [n for n in (m.name for m in modules) if n in edges[name]]
        for succ in successors:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    if len(ordered) != len(modules):
        remaining = [m.name for m in modules if m.name not in {o.name for o in ordered}]
        raise RuntimeError(f"cyclic dependency detected among modules: {remaining}")

    if [m.name for m in ordered] != [m.name for m in modules]:
        logger.info(
            "load order (topological): %s",
            " -> ".join(m.name for m in ordered),
        )
    return ordered
