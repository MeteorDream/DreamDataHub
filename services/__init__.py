"""services — 外部服务客户端层。

放置对第三方 API / SDK 的封装（异步客户端），特点：
- **不依赖 hub** —— 每个 client 可脱离 hub 独立使用
- **通过 Pydantic Config 传递配置** —— 由使用它的 Module 从 ``ctx.config`` 翻译得到
- **可被多个 Module 共享**  —— 生命周期由使用它的 Module setup / teardown 管理

架构层次：``services/`` 位于 ``module/`` 之下 —— Module 使用 services，反之不成立。

当前包含：
- ``weibo``            微博 Web API 客户端 + QR 登录
- ``weather``          天气 provider 抽象基类 + AMap/BaiduMap/... 具体实现
"""
