"""Hub — 总线、生命周期、后台任务持有者。

职责：
- 维护 ``topic → [Subscriber]`` 路由表
- ``publish``：把投递转换成独立 task，慢/崩 handler 不影响其他订阅者
- 启停顺序：startup 顺序、shutdown LIFO
- SIGINT/SIGTERM：取消所有后台任务，运行 shutdown 钩子，干净退出
- 异常隔离：handler 异常**不**冒泡，转为 ``system.error`` 事件 + 日志

实现说明：故意**不**使用 ``asyncio.TaskGroup`` —— 在 TaskGroup 中任何一个
任务异常都会取消整组，与「单个 handler 崩了不能拖死 hub」相冲突。我们用
显式的 ``set[Task]`` 自管，在 shutdown 时统一取消并 ``gather(...,
return_exceptions=True)`` 收尾。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Coroutine

from hub.context import Context
from hub.module import Handler, Module
from hub.topics import SYSTEM_ERROR, SYSTEM_READY

logger = logging.getLogger("hub")


@dataclass
class _Subscriber:
    """运行期订阅者条目 — 把 handler 与它所属模块的 Context 绑在一起。"""

    module_name: str
    topic: str
    fn: Handler
    ctx: Context

    async def invoke(self, payload: Any, hub: Hub) -> None:
        """安全调用 handler — 异常被捕获、上报 system.error，绝不外泄。"""
        try:
            await self.fn(payload, self.ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.exception("handler %r on %r failed", self.fn.__name__, self.topic)
            # 二次发布 system.error 时再失败就不要无限套娃了
            if self.topic != SYSTEM_ERROR:
                try:
                    await hub.publish(
                        SYSTEM_ERROR,
                        {"module": self.module_name, "topic": self.topic, "exc": repr(exc)},
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to publish system.error")


@dataclass
class _BoundModule:
    """运行期模块条目 — Module 声明 + 它的 Context。"""

    module: Module
    ctx: Context
    started: bool = False
    subs: list[_Subscriber] = field(default_factory=list)


class Hub:
    """数据交换中心主对象。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscriber]] = {}
        self._bound: list[_BoundModule] = []  # 按加载顺序绑定模块
        self._tasks: set[asyncio.Task[Any]] = set()
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._stopping = False

    # ---- 公开 API（给 Context / 主程序）---------------------------------

    async def publish(self, topic: str, payload: Any) -> None:
        """投递事件到 topic。立即返回；每个订阅者各起一个 task。

        无订阅者：DEBUG 日志（如 ``llm.exchange`` 这类可选事件常无订阅者）。
        关停中或已停：丢弃，DEBUG 日志。
        """
        if not self._running or self._stopping:
            logger.debug("publish %s: hub not running, drop", topic)
            return
        subs = self._subs.get(topic)
        if not subs:
            logger.debug("publish %s: no subscribers", topic)
            return
        for sub in subs:
            self._track(asyncio.create_task(sub.invoke(payload, self), name=f"{sub.module_name}:{topic}"))

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """注册长任务（被 Context.spawn 包装调用）。"""
        if not self._running:
            raise RuntimeError("Hub.spawn called before run()")
        task = asyncio.create_task(coro, name=name)
        self._track(task)
        return task

    # ---- 注册模块 -------------------------------------------------------

    def register(self, module: Module) -> None:
        """登记一个模块（已 bind_config）。在 run() 之前调用。"""
        if any(b.module.name == module.name for b in self._bound):
            raise ValueError(f"duplicate module: {module.name}")
        ctx = Context(self, module.name, module.config, self._shutdown_event)
        self._bound.append(_BoundModule(module=module, ctx=ctx))

    # ---- 主循环 ---------------------------------------------------------

    async def run(self) -> None:
        """跑起来 — startup → 等 shutdown 信号 → 取消任务 → shutdown 钩子。"""
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
                installed_signals.append(sig)
            except NotImplementedError:
                # Windows 上不支持，退化到 KeyboardInterrupt
                pass

        self._running = True
        try:
            self._build_subs_index()
            await self._run_startup()
            if not self._stopping:
                # 通知所有人 ready —— 这本身也会起若干 task，被纳入 _tasks
                await self.publish(SYSTEM_READY, {})
                # 主循环：等 shutdown 信号
                try:
                    await self._shutdown_event.wait()
                except asyncio.CancelledError:
                    pass
            logger.info("hub: shutdown signaled, cancelling tasks...")
        finally:
            for sig in installed_signals:
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(sig)
            await self._cancel_all_tasks()
            self._running = False
            await self._run_shutdown()

    def stop(self) -> None:
        """编程式停机（测试 / REPL 用）。"""
        self._request_stop()

    # ---- 内部 -----------------------------------------------------------

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _request_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._shutdown_event.set()

    async def _cancel_all_tasks(self) -> None:
        if not self._tasks:
            return
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _build_subs_index(self) -> None:
        for bm in self._bound:
            for topic, fns in bm.module.handlers.items():
                for fn in fns:
                    sub = _Subscriber(module_name=bm.module.name, topic=topic, fn=fn, ctx=bm.ctx)
                    bm.subs.append(sub)
                    self._subs.setdefault(topic, []).append(sub)
        if logger.isEnabledFor(logging.INFO):
            summary = ", ".join(f"{t}({len(s)})" for t, s in self._subs.items()) or "(none)"
            logger.info("hub: subscriptions = %s", summary)

    async def _run_startup(self) -> None:
        for bm in self._bound:
            for hook in bm.module.startup_hooks:
                if self._stopping:
                    return
                logger.info("startup: %s.%s", bm.module.name, hook.__name__)
                try:
                    await hook(bm.ctx)
                except Exception:
                    logger.exception("startup failed: %s", bm.module.name)
                    self._request_stop()
                    return
            bm.started = True

    async def _run_shutdown(self) -> None:
        # LIFO：最后启动的最先关
        for bm in reversed(self._bound):
            if not bm.started:
                continue
            for hook in reversed(bm.module.shutdown_hooks):
                logger.info("shutdown: %s.%s", bm.module.name, hook.__name__)
                try:
                    await hook(bm.ctx)
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown hook failed: %s", bm.module.name)
