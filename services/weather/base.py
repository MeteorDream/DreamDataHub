"""weather provider 抽象基类 + 统一数据模型。

一个 ``WeatherProvider`` 子类代表一个天气数据源（高德 / 百度 / 心知天气 ...）。
所有 provider 的 ``location()`` / ``forecast()`` 返回**统一 Pydantic 模型**
（``LocationData`` / ``ForecastData``），由 provider 内部把原始响应翻译过来——
调用方无需关心是哪一家。

**新增 provider 的步骤**：

1. 建 ``services/weather/<name>.py``，实现 ``class <Name>Provider(WeatherProvider)``
2. 定义对应的 ``<Name>Config(WeatherProviderConfig)``
3. 在 ``services/weather/__init__.py:PROVIDERS`` 里注册
4. TOML 里 ``[module.weather.<name>]`` 段填 config

所有 provider 抛的错都是 ``WeatherProviderError`` 或其子类；调用方统一 catch。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field

__all__ = [
    "DailyForecast",
    "ForecastData",
    "LocationData",
    "WeatherProvider",
    "WeatherProviderConfig",
    "WeatherProviderError",
]


class WeatherProviderConfig(BaseModel):
    """所有 provider config 的基类。子类自定义字段。"""

    model_config = {"extra": "ignore"}


class LocationData(BaseModel):
    """逆地理编码结果。

    ``adcode`` 是最关键的字段 —— 后续 ``forecast(adcode)`` 用它作为查询主键。
    """

    adcode: str = Field(default="", description="城市/地区编码（用于后续 forecast 查询）")
    formatted_address: str = Field(default="", description="完整地址字符串")
    country: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    township: str = ""
    citycode: str = Field(default="", description="城市区号")
    raw: dict[str, Any] = Field(
        default_factory=dict, description="provider 原始响应，用于访问未纳入模型的字段"
    )


class DailyForecast(BaseModel):
    """单日预报。字段统一以字符串存放（不同 provider 的类型不一致，用 str 兜底）。"""

    date: str = Field(default="", description="ISO 日期，如 '2026-07-12'")
    week: str = Field(default="", description="星期数字符串 '1'..'7'")
    day_weather: str = Field(default="", description="白天天气，如 '多云'")
    night_weather: str = ""
    day_temp: str = Field(default="", description="白天温度（摄氏度字符串）")
    night_temp: str = ""
    day_wind: str = Field(default="", description="白天风向")
    night_wind: str = ""
    day_power: str = Field(default="", description="白天风力等级，如 '1-3'")
    night_power: str = ""


class ForecastData(BaseModel):
    """天气预报聚合。``casts[0]`` 通常是今天，后续为未来几天。"""

    city: str = Field(default="", description="城市名称（provider 返回，用于展示）")
    adcode: str = ""
    province: str = ""
    report_time: str = Field(default="", description="预报发布时间")
    casts: list[DailyForecast] = Field(default_factory=list)
    raw: dict[str, Any] = Field(
        default_factory=dict, description="provider 原始响应"
    )


class WeatherProviderError(Exception):
    """provider 请求 / 解析失败。所有 provider 内部把原始错误包装成这个。"""


class WeatherProvider(ABC):
    """天气 provider 抽象基类。

    子类必须实现 ``location()`` 和 ``forecast()``，并声明 ``name`` ClassVar
    （用于在 ``PROVIDERS`` registry 里做分发键）。
    """

    name: ClassVar[str]

    def __init__(self, config: WeatherProviderConfig) -> None:
        self._config = config

    @abstractmethod
    async def location(self, longitude: float, latitude: float) -> LocationData:
        """逆地理编码。失败抛 ``WeatherProviderError``。"""

    @abstractmethod
    async def forecast(self, adcode: str) -> ForecastData:
        """按 adcode 查询天气预报。失败抛 ``WeatherProviderError``。"""
