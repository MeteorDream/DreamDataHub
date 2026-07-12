"""weather module —— hub 门面。

职责：
- 根据 config ``[module.weather] provider = "..."`` 选一个 :class:`WeatherProvider`
  子类实例化，挂到 ``ctx.state.provider``（生命周期由 Module 管理）
- 通过两个 Capability 向外暴露 ``location`` / ``forecast`` 能力
- 可选的 cron 周期推送任务

**具体的 API 实现在 ``services/weather/`` 子包里**（provider 抽象基类 + 各家实现）。
**展示层（emoji / markdown / html 格式化）在 ``services/weather/formatter.py``**。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import ClassVar

from croniter import croniter
from pydantic import BaseModel, Field

from hub import Capability, Context, Module
from services.weather import (
    PROVIDERS,
    ForecastData,
    LocationData,
    WeatherProviderError,
)

# ---------------------------------------------------------------------------
# Capability 契约
# ---------------------------------------------------------------------------


class WeatherLocationParams(BaseModel):
    """``WeatherLocationService`` 入参 —— 逆地理编码。"""

    longitude: float
    latitude: float


class WeatherLocationService(Capability):
    """逆地理编码能力契约。Result 直接用 services 层的统一 :class:`LocationData`。"""

    name: ClassVar[str] = "weather.location"
    Params: ClassVar[type[BaseModel]] = WeatherLocationParams
    Result: ClassVar[type[BaseModel]] = LocationData


class WeatherForecastParams(BaseModel):
    """``WeatherForecastService`` 入参。"""

    adcode: str = Field(min_length=1, description="城市/地区编码，如 '440305'")
    city: str | None = Field(
        default=None,
        description=(
            "调用方希望在展示时使用的城市名（可选）；不参与查询，仅用于日志 / "
            "调用方追踪。provider 返回的真实城市名在 ``ForecastData.city`` 里。"
        ),
    )


class WeatherForecastService(Capability):
    """天气预报能力契约。Result 直接用 services 层的统一 :class:`ForecastData`。"""

    name: ClassVar[str] = "weather.forecast"
    Params: ClassVar[type[BaseModel]] = WeatherForecastParams
    Result: ClassVar[type[BaseModel]] = ForecastData


mod = Module("weather")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@mod.on_startup
async def setup(ctx: Context) -> None:
    """按 ``provider`` 字段选一个 provider 实例化，挂到 ctx.state.provider。"""
    cfg = ctx.config
    provider_name = str(cfg.get("provider", "amap"))
    if provider_name not in PROVIDERS:
        raise RuntimeError(
            f"weather: unknown provider {provider_name!r}, "
            f"available: {sorted(PROVIDERS)}"
        )
    provider_cls, config_cls = PROVIDERS[provider_name]

    # provider 特定的 config 从 [module.weather.<provider>] 子表取
    provider_cfg_dict = cfg.get(provider_name) or {}
    provider_cfg = config_cls.model_validate(provider_cfg_dict)
    ctx.state.provider = provider_cls(provider_cfg)
    ctx.logger.info("weather module: provider=%s", provider_name)

    # cron 周期推送（可选）
    schedule = str(cfg.get("schedule", ""))
    ctx.state.schedule = schedule
    if schedule:
        ctx.spawn(_push(ctx), name="weather:push")
        ctx.logger.info("weather module: cron scheduler=%r", schedule)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("weather module teardown")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@mod.provides(WeatherLocationService)
async def location_capability(
    ctx: Context, params: WeatherLocationParams
) -> LocationData:
    """逆地理编码 —— 委托给 ctx.state.provider。"""
    try:
        return await ctx.state.provider.location(params.longitude, params.latitude)
    except WeatherProviderError as exc:
        raise RuntimeError(f"weather.location failed: {exc}") from exc


@mod.provides(WeatherForecastService)
async def forecast_capability(
    ctx: Context, params: WeatherForecastParams
) -> ForecastData:
    """天气预报 —— 委托给 ctx.state.provider。"""
    try:
        return await ctx.state.provider.forecast(params.adcode)
    except WeatherProviderError as exc:
        raise RuntimeError(f"weather.forecast failed: {exc}") from exc


# ---------------------------------------------------------------------------
# 周期推送（stub）
# ---------------------------------------------------------------------------


async def _push(ctx: Context) -> None:
    """按 cron 表达式周期推送天气 —— 目前只是骨架，未接推送目标。"""
    schedule = ctx.state.schedule
    if not croniter.is_valid(schedule):
        ctx.logger.warning("weather: invalid cron schedule: %r", schedule)
        return

    try:
        while not ctx.hub_event.is_set():
            try:
                now = datetime.now()  # noqa: DTZ005 — cron 期望本地时间
                cron = croniter(schedule, now)
                next_time = cron.get_next(datetime)
                await asyncio.wait_for(
                    ctx.hub_event.wait(),
                    timeout=(next_time - now).total_seconds(),
                )
            except TimeoutError:
                pass
            # TODO: 真正的推送逻辑（拉取 forecast → 格式化 → publish 到 IMReply 等）
            ctx.logger.info("weather push: schedule triggered (not implemented)")
    except asyncio.CancelledError:
        ctx.logger.debug("weather push cancelled")
        raise
