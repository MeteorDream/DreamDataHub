"""weather provider registry —— 名字 → (Provider 类, Config 类) 的映射。

**新增 provider 步骤**：

1. 建 ``services/weather/<name>.py``，实现 ``class <Name>Provider(WeatherProvider)`` +
   ``class <Name>Config(WeatherProviderConfig)``
2. 在下面 ``PROVIDERS`` 里加一行 ``"<name>": (<Name>Provider, <Name>Config)``
3. TOML 里 ``[module.weather] provider = "<name>"`` + ``[module.weather.<name>]`` 段填 config

Module wrapper（``module/weather.py``）根据 ``[module.weather] provider = ...`` 从
这个 registry 里取 provider 类，实例化后挂到 ``ctx.state.provider``。
"""

from __future__ import annotations

from services.weather.amap import AMapConfig, AMapProvider
from services.weather.base import (
    DailyForecast,
    ForecastData,
    LocationData,
    WeatherProvider,
    WeatherProviderConfig,
    WeatherProviderError,
)

__all__ = [
    "PROVIDERS",
    "AMapConfig",
    "AMapProvider",
    "DailyForecast",
    "ForecastData",
    "LocationData",
    "WeatherProvider",
    "WeatherProviderConfig",
    "WeatherProviderError",
]


# provider 名字 → (Provider 类, Config 类)。加新 provider 时在这里注册。
PROVIDERS: dict[str, tuple[type[WeatherProvider], type[WeatherProviderConfig]]] = {
    "amap": (AMapProvider, AMapConfig),
    # 未来：
    # "baidumap": (BaiduMapProvider, BaiduMapConfig),
    # "xinzhi": (XinzhiProvider, XinzhiConfig),
}
