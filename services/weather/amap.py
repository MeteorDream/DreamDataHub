"""高德（AMap）天气 provider。

API 文档：
- 逆地理编码：https://lbs.amap.com/api/webservice/guide/api/georegeo
- 天气查询：https://lbs.amap.com/api/webservice/guide/api-advanced/weatherinfo
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from services.weather.base import (
    DailyForecast,
    ForecastData,
    LocationData,
    WeatherProvider,
    WeatherProviderConfig,
    WeatherProviderError,
)

__all__ = ["AMapConfig", "AMapProvider"]


class AMapConfig(WeatherProviderConfig):
    """高德 provider 配置。"""

    key: str = ""
    timeout: float = 15.0


class AMapProvider(WeatherProvider):
    """高德天气 provider —— 逆地理编码 + 天气预报。"""

    name: ClassVar[str] = "amap"

    _LOCATION_URL = "https://restapi.amap.com/v3/geocode/regeo"
    _WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
    _INFO_URL_HELP = "code 说明可参考: https://lbs.amap.com/api/webservice/guide/tools/info"

    def __init__(self, config: AMapConfig) -> None:
        if not config.key:
            raise WeatherProviderError("amap: key is required")
        super().__init__(config)
        self._cfg: AMapConfig = config

    async def location(self, longitude: float, latitude: float) -> LocationData:
        params = {
            "key": self._cfg.key,
            "location": f"{longitude:.4f},{latitude:.4f}",
            "output": "JSON",
        }
        data = await self._get(self._LOCATION_URL, params, action="location")
        regeocode = data.get("regeocode") or {}
        addr = regeocode.get("addressComponent") or {}
        # amap 有些字段可能返回空 dict 或空列表（如无城市时 city 是 []），统一转成 str
        return LocationData(
            adcode=_as_str(addr.get("adcode")),
            formatted_address=_as_str(regeocode.get("formatted_address")),
            country=_as_str(addr.get("country")),
            province=_as_str(addr.get("province")),
            city=_as_str(addr.get("city")),
            district=_as_str(addr.get("district")),
            township=_as_str(addr.get("township")),
            citycode=_as_str(addr.get("citycode")),
            raw=regeocode,
        )

    async def forecast(self, adcode: str) -> ForecastData:
        params = {
            "key": self._cfg.key,
            "city": adcode,
            "extensions": "all",
            "output": "JSON",
        }
        data = await self._get(self._WEATHER_URL, params, action="forecast")
        forecasts = data.get("forecasts") or []
        if not forecasts:
            raise WeatherProviderError(f"amap forecast: empty result for adcode={adcode}")
        fc = forecasts[0]
        casts = [
            DailyForecast(
                date=_as_str(c.get("date")),
                week=_as_str(c.get("week")),
                day_weather=_as_str(c.get("dayweather")),
                night_weather=_as_str(c.get("nightweather")),
                day_temp=_as_str(c.get("daytemp")),
                night_temp=_as_str(c.get("nighttemp")),
                day_wind=_as_str(c.get("daywind")),
                night_wind=_as_str(c.get("nightwind")),
                day_power=_as_str(c.get("daypower")),
                night_power=_as_str(c.get("nightpower")),
            )
            for c in (fc.get("casts") or [])
        ]
        return ForecastData(
            city=_as_str(fc.get("city")),
            adcode=_as_str(fc.get("adcode")),
            province=_as_str(fc.get("province")),
            report_time=_as_str(fc.get("reporttime")),
            casts=casts,
            raw=fc,
        )

    async def _get(self, url: str, params: dict, *, action: str) -> dict:
        """通用 GET，做 HTTP 状态码 + amap 业务状态校验，失败抛 WeatherProviderError。"""
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise WeatherProviderError(f"amap {action}: HTTP error: {exc}") from exc
        if resp.status_code != 200:
            raise WeatherProviderError(
                f"amap {action}: HTTP {resp.status_code}"
            )
        data = resp.json()
        if data.get("status") != "1":
            raise WeatherProviderError(
                f"amap {action} failed: status={data.get('status')} "
                f"code={data.get('infocode')} info={data.get('info')} "
                f"({self._INFO_URL_HELP})"
            )
        return data


def _as_str(v: object) -> str:
    """把 amap 返回值统一转成字符串。空 list / None / dict 都归一到空字符串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, dict)) and not v:
        return ""
    return str(v)
