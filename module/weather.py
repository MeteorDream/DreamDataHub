
from __future__ import annotations

from datetime import datetime
from croniter import croniter
from typing import ClassVar

import httpx

from hub import Context, Module

mod = Module("weather")

@mod.on_startup
async def setup(ctx: Context) -> None:
    cfg = ctx.config
    Weather.AMAP_KEY = cfg.get("amap_key", "")
    schedule = cfg.get("schedule", "")  # cron 表达式
    ctx.state.schedule = schedule
    if schedule:
        ctx.spawn(_push(ctx), name="weather:push")
        ctx.logger.info("Weather module setup scheuler")

@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("Weather module teardown")

async def _push(ctx: Context) -> None:
    """周期推送天气信息到指定的 topic"""

    schedule = ctx.state.schedule
    if not croniter.is_valid(schedule):
        ctx.logger.warning("Weather module: invalid cron schedule: %r", schedule)
        return 

    try:
        while not ctx.hub_event.is_set():
            # TODO: 待实现天气查询推送功能
            try:
                now = datetime.now()
                cron = croniter(schedule, now)
                next_time = cron.get_next(datetime)
                await asyncio.wait_for(ctx.hub_event.wait(), timeout=(next_time - now).total_seconds())
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        ctx.logger.debug("weather push cancelled")
        raise

class Weather:
    """各种平台天气 API 接口"""

    # 高德 API KEY
    AMAP_KEY: ClassVar[str] = ""

    @staticmethod
    async def amap_location(longitude: float, latitude: float) -> tuple[str, dict]:
        """逆地理编码, 根据经纬度返回位置信息, api 文档: https://lbs.amap.com/api/webservice/guide/api/georegeo
        
        Args:
            longitude: 经度
            latitude: 纬度
            
        Returns:
            msg, data: 1. 错误信息, 成功时为空字符串; 2. 位置信息, 成功时包含 'formatted_address' 等字段, 失败时为空字典,
        """
        async with httpx.AsyncClient() as client:
            url = "https://restapi.amap.com/v3/geocode/regeo"
            params = {"key": Weather.AMAP_KEY, "location": f"{longitude:.4f},{latitude:.4f}", "output": "JSON"}
            response = await client.get(url, params=params, timeout=5.0)
            
            # 检查响应状态码
            if response.status_code != 200:
                return f"请求失败, 状态码: {response.status_code}", {}
            data = response.json()
            if data.get("status", "0") != "1":
                return f"请求失败, status: {data.get('status')}, code: {data.get('infocode')}, info: {data.get('info')}, code 说明可参考: https://lbs.amap.com/api/webservice/guide/tools/info", {}
            return "", data.get("regeocode")

    @staticmethod
    async def amap_weather(city: str) -> tuple[str, dict]:
        """天气查询, 根据 city 返回天气信息, api 文档: https://lbs.amap.com/api/webservice/guide/api-advanced/weatherinfo
        
        Args:
            city: 城市编码, 输入城市的 adcode，adcode 信息可通过 amap_location 获取
        
        Returns:
            msg, data: 1. 错误信息, 成功时为空字符串; 2. 天气信息, 成功时包含 'lives' 等字段, 失败时为空字典,
        """
        async with httpx.AsyncClient() as client:
            url = "https://restapi.amap.com/v3/weather/weatherInfo"
            params = {"key": Weather.AMAP_KEY, "city": city, "extensions": "all", "output": "JSON"}
            response = await client.get(url, params=params, timeout=5.0)
            
            # 检查响应状态码
            if response.status_code != 200:
                return f"请求失败, 状态码: {response.status_code}", {}
            data = response.json()
            if data.get("status", "0") != "1":
                return f"请求失败, status: {data.get('status')}, code: {data.get('infocode')}, info: {data.get('info')}, code 说明可参考: https://lbs.amap.com/api/webservice/guide/tools/info", {}
            return "", data
        
if __name__ == "__main__":
    import asyncio, json
    Weather.AMAP_KEY = "8359fbed879bf89df1c8f9e644fca586"
    message, data = asyncio.run(Weather.amap_weather('440305'))
    print(message, json.dumps(data, ensure_ascii=False))