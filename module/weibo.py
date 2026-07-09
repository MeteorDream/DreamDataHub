"""Weibo — 微博 Web API 异步客户端。

参考 https://github.com/jackwener/weibo-cli 的同步实现，改成基于 ``httpx.AsyncClient``
的异步版本。当前实现的功能：

- ``get_hot_search()``       热搜 sidebar（约 50 条）
- ``get_hot_band()``         完整热搜榜
- ``get_weibo_detail(mid)``  单条微博详情
- ``get_profile(uid)``       用户资料
- ``get_user_weibos(uid)``   用户微博列表
- ``qr_login_start()``       扫码登录第一步：申请二维码
- ``qr_login_check(session)`` 扫码登录第二步：查询扫码状态

QR 登录拆成两个方法而不是一个阻塞循环，是为了适配本项目的 Workflow 场景：
调用方（workflow / IM 消息处理器）可以在 ``qr_login_start()`` 后把二维码 URL 发给
用户，然后按自己的节奏（例如每 2s）调 ``qr_login_check()`` 直到成功或超时。

反检测策略（比参考实现保守）：
- 请求间高斯抖动 (mean=1.0s, σ=0.3)，5% 概率追加 2~5s 长停顿模拟阅读
- 429 / 5xx 指数退避重试，最多 ``max_retries`` 次
- 保留响应 Set-Cookie 到会话 jar
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WeiboApiError(Exception):
    """微博 API 调用失败的基类。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        response: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.response = response


class WeiboSessionExpiredError(WeiboApiError):
    """会话过期（``ok == -100`` 或返回文案含"请登录"等关键字）。"""

    def __init__(self) -> None:
        super().__init__("weibo: 会话已过期，请重新登录", code="session_expired")


class WeiboAuthRequiredError(WeiboApiError):
    """访问需登录接口但未提供 cookie。"""

    def __init__(self, action: str) -> None:
        super().__init__(f"weibo: {action} 需要登录（缺少 SUB/SUBP cookie）")


class WeiboQRExpiredError(WeiboApiError):
    """扫码登录二维码已过期。"""

    def __init__(self) -> None:
        super().__init__("weibo: 二维码已过期，请重新申请", code="qr_expired")


# ---------------------------------------------------------------------------
# QR 登录数据结构
# ---------------------------------------------------------------------------


QRLoginStatus = Literal["waiting", "scanned", "success", "expired", "unknown"]


@dataclass
class QRSession:
    """扫码登录第一步 (`qr_login_start`) 返回的会话信息。

    这是一个纯数据对象，可以序列化到 workflow 的 state 里跨调用往返。
    调用方后续用 ``qr_login_check(session)`` 轮询扫码状态。

    :param qrid: passport 生成的二维码 ID，唯一标识本次登录会话
    :param image_url: 二维码图片直链（已经是 https，可直接给 IM 发图）
    :param scan_url: 二维码里编码的扫描地址（备用，通常直接展示 image_url）
    :param passport_cookies: 从 passport 域拿到的 cookie（含 X-CSRF-TOKEN），后续
                             ``qr_login_check`` 必须带上，否则接口会当无效会话
    """

    qrid: str
    image_url: str
    scan_url: str
    passport_cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class QRLoginResult:
    """扫码登录轮询 (`qr_login_check`) 的一次结果。

    :param status: ``waiting`` — 未扫码；``scanned`` — 已扫码待手机确认；
                   ``success`` — 成功（``cookies`` 可用）；
                   ``expired`` — 二维码过期；``unknown`` — 未知 retcode（附 message 便于排查）
    :param cookies: 仅 ``status == "success"`` 时非空；可直接构造 ``Weibo(cookies=...)``
    :param retcode: 原始 retcode
    :param message: 原始 msg 文案
    """

    status: QRLoginStatus
    cookies: dict[str, str] = field(default_factory=dict)
    retcode: int | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


class _Endpoints:
    BASE_URL = "https://weibo.com"
    MOBILE_BASE_URL = "https://m.weibo.cn"
    PASSPORT_URL = "https://passport.weibo.com"

    # 热搜 / 榜单
    HOT_SEARCH = "/ajax/side/hotSearch"
    HOT_BAND = "/ajax/statuses/hot_band"

    # 用户
    PROFILE_INFO = "/ajax/profile/info"
    MY_MBLOG = "/ajax/statuses/mymblog"

    # 微博详情
    STATUSES_SHOW = "/ajax/statuses/show"

    # QR 登录（passport.weibo.com）
    SSO_SIGNIN = "/sso/signin"
    QR_IMAGE = "/sso/v2/qrcode/image"
    QR_CHECK = "/sso/v2/qrcode/check"


# QR 登录用到的常量
_QR_ENTRY = "miniblog"
_QR_SOURCE = "miniblog"
_QR_REDIRECT_URL = "https://weibo.com/"
_QR_VERSION = "20250520"

# passport 返回的 retcode
_RETCODE_SUCCESS = 20000000
_RETCODE_QR_NOT_SCANNED = 50114001
_RETCODE_QR_SCANNED = 50114002
_RETCODE_QR_EXPIRED = 50114004


_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{_Endpoints.BASE_URL}/",
}


_PASSPORT_HEADERS: dict[str, str] = {
    **_HEADERS,
    "x-requested-with": "XMLHttpRequest",
    "Referer": (
        f"{_Endpoints.PASSPORT_URL}/sso/signin"
        f"?entry={_QR_ENTRY}&source={_QR_SOURCE}&url={_QR_REDIRECT_URL}"
    ),
}


_SESSION_EXPIRED_KEYWORDS: tuple[str, ...] = (
    "请先登录",
    "请登录后使用",
    "请登录",
    "用户未登录",
)

_REQUIRED_AUTH_COOKIES: frozenset[str] = frozenset({"SUB", "SUBP"})


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class Weibo:
    """微博 Web API 异步客户端。

    Anti-detection：请求间高斯抖动 + 5% 长停顿 + 429/5xx 指数退避重试。

    :param cookies: 已登录的 cookie 字典，至少包含 ``SUB`` / ``SUBP`` 才能访问需鉴权接口。
                    仅访问热搜等公开接口时可传空。
    :param timeout: HTTP 请求超时（秒）。
    :param request_delay: 相邻请求最小间隔（秒），设为 ``0`` 关闭节流。
    :param max_retries: 429 / 5xx / 网络错误最大重试次数。
    """

    HOT_SEARCH_ACTION: ClassVar[str] = "热搜"
    HOT_BAND_ACTION: ClassVar[str] = "热搜榜"
    PROFILE_ACTION: ClassVar[str] = "用户资料"
    USER_WEIBOS_ACTION: ClassVar[str] = "用户微博"
    WEIBO_DETAIL_ACTION: ClassVar[str] = "微博详情"
    QR_LOGIN_ACTION: ClassVar[str] = "扫码登录"

    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        *,
        timeout: float = 30.0,
        request_delay: float = 1.0,
        max_retries: int = 3,
    ) -> None:
        self._cookies = dict(cookies or {})
        self._timeout = timeout
        self._request_delay = request_delay
        self._max_retries = max_retries

        self._client: httpx.AsyncClient | None = None
        self._mobile_client: httpx.AsyncClient | None = None

        self._last_request_time = 0.0
        self._request_count = 0
        # 保护相邻请求节流的锁 —— 并发调多个方法时避免同一时刻突刺
        self._rate_lock = asyncio.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def __aenter__(self) -> Weibo:
        await self.aopen()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aopen(self) -> None:
        """显式初始化 —— 想脱离 async with 使用时调。重复调用是幂等的。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_Endpoints.BASE_URL,
                headers=dict(_HEADERS),
                cookies=self._cookies,
                follow_redirects=True,
                timeout=httpx.Timeout(self._timeout),
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._mobile_client is not None:
            await self._mobile_client.aclose()
            self._mobile_client = None

    # ── 认证辅助 ────────────────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        """cookie 里是否包含所有必需字段（SUB / SUBP）。"""
        return _REQUIRED_AUTH_COOKIES.issubset(self._cookies.keys())

    def _require_auth(self, action: str) -> None:
        if not self.is_authenticated:
            raise WeiboAuthRequiredError(action)

    # ── 节流 & 重试 ─────────────────────────────────────────────────────

    async def _rate_limit_delay(self) -> None:
        if self._request_delay <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            elapsed = loop.time() - self._last_request_time
            if elapsed < self._request_delay:
                jitter = max(0.0, random.gauss(0.3, 0.15))
                if random.random() < 0.05:
                    jitter += random.uniform(2.0, 5.0)
                sleep_time = self._request_delay - elapsed + jitter
                logger.debug("weibo: rate-limit delay %.2fs", sleep_time)
                await asyncio.sleep(sleep_time)
            self._last_request_time = loop.time()
            self._request_count += 1

    def _client_for(self, mobile: bool) -> httpx.AsyncClient:
        if mobile:
            if self._mobile_client is None:
                mobile_headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                        "Mobile/15E148 Safari/604.1"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{_Endpoints.MOBILE_BASE_URL}/",
                    "X-Requested-With": "XMLHttpRequest",
                }
                self._mobile_client = httpx.AsyncClient(
                    base_url=_Endpoints.MOBILE_BASE_URL,
                    headers=mobile_headers,
                    cookies=self._cookies,
                    follow_redirects=True,
                    timeout=httpx.Timeout(self._timeout),
                )
            return self._mobile_client
        if self._client is None:
            raise RuntimeError("weibo: client not initialized; use `async with Weibo(...)`")
        return self._client

    def _merge_response_cookies(self, resp: httpx.Response) -> None:
        for name, value in resp.cookies.items():
            if not value:
                continue
            self._cookies[name] = value
            if self._client is not None:
                self._client.cookies.set(name, value)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        mobile: bool = False,
    ) -> dict[str, Any]:
        await self._rate_limit_delay()
        http = self._client_for(mobile)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await http.request(method, url, params=params)
                if not mobile:
                    self._merge_response_cookies(resp)
                logger.info(
                    "weibo: [#%d] %s %s -> %d",
                    self._request_count,
                    method,
                    url[:60],
                    resp.status_code,
                )

                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "weibo: HTTP %d, retry in %.1fs (%d/%d)",
                        resp.status_code,
                        wait,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                text = resp.text
                if text.startswith("<"):
                    raise WeiboApiError(f"weibo: HTML instead of JSON from {url}")
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                wait = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "weibo: network error %s, retry in %.1fs (%d/%d)",
                    type(exc).__name__,
                    wait,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(wait)
            except httpx.HTTPStatusError as exc:
                # 非 429/5xx 的 4xx，不重试
                raise WeiboApiError(
                    f"weibo: HTTP {exc.response.status_code} from {url}",
                    code=exc.response.status_code,
                ) from exc

        if last_exc is not None:
            raise WeiboApiError(
                f"weibo: request failed after {self._max_retries} retries: {last_exc}"
            ) from last_exc
        raise WeiboApiError(f"weibo: request failed after {self._max_retries} retries")

    # ── 响应校验 ────────────────────────────────────────────────────────

    @staticmethod
    def _handle_response(
        data: dict[str, Any], action: str, *, unwrap: bool = True
    ) -> dict[str, Any]:
        """微博接口大多返回 ``{"ok": 1, "data": {...}}``；这里做统一校验 + 解包。

        - ``ok == -100`` / 消息含登录关键字 → 抛 ``WeiboSessionExpiredError``
        - ``ok == 0`` → 抛 ``WeiboApiError``
        - ``ok`` 真值：``unwrap=True`` 时返回 ``data["data"]`` 或整个 dict
        """
        ok = data.get("ok")
        message = data.get("msg", data.get("message", "Unknown error"))

        if ok == -100:
            raise WeiboSessionExpiredError()

        if ok == 0:
            msg_str = str(message)
            if any(kw in msg_str for kw in _SESSION_EXPIRED_KEYWORDS):
                raise WeiboSessionExpiredError()
            raise WeiboApiError(f"weibo: {action}: {message} (ok={ok})", code=ok, response=data)

        if ok:
            return data.get("data", data) if unwrap else data

        raise WeiboApiError(f"weibo: {action}: {message} (ok={ok})", code=ok, response=data)

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        action: str,
        unwrap: bool = True,
        mobile: bool = False,
    ) -> dict[str, Any]:
        data = await self._request("GET", url, params=params, mobile=mobile)
        return self._handle_response(data, action, unwrap=unwrap)

    # ── 公开 API ────────────────────────────────────────────────────────

    async def get_hot_search(self) -> dict[str, Any]:
        """获取微博热搜（sidebar 热搜列表，约 50 条）。

        Returns:
            解包后的 ``data`` 字段，通常包含 ``hotgov`` / ``realtime`` 等键。
            ``realtime`` 是主榜单，元素形如::

                {
                    "word": "热搜词",
                    "raw_hot": 100000,
                    "num": 100000,
                    "rank": 1,
                    "label_name": "热" / "新" / "沸" / ...
                }
        """
        return await self._get(_Endpoints.HOT_SEARCH, action=self.HOT_SEARCH_ACTION)

    async def get_hot_band(self) -> dict[str, Any]:
        """获取完整热搜榜（`/ajax/statuses/hot_band`）。

        比 ``get_hot_search`` 返回更详细的字段（分类标签、来源等）。公开接口，
        不需要登录。

        Returns:
            解包后的 ``data``，主要字段：``band_list`` (完整榜单，含分类) /
            ``ads_info`` / ``hotgov`` / ``fun_word_info`` / 更新时间等。
            ``band_list`` 元素常见字段：``word`` / ``raw_hot`` / ``rank`` /
            ``category`` / ``label_name`` / ``mid`` / ``onboard_time``。
        """
        return await self._get(_Endpoints.HOT_BAND, action=self.HOT_BAND_ACTION)

    async def get_weibo_detail(self, mblogid: str) -> dict[str, Any]:
        """获取单条微博详情。

        Args:
            mblogid: 微博 ID，短 ID（如 ``"Qw06Kd98p"``）或长数字 ID 均可。

        Returns:
            该接口不遵循 ``{ok:1, data:{}}`` 包装（``unwrap=False``），直接返回微博对象，
            常见字段：``text_raw`` / ``created_at`` / ``pic_ids`` / ``pic_infos`` /
            ``user`` / ``retweeted_status`` / ``reposts_count`` / ``comments_count`` /
            ``attitudes_count`` 等。
        """
        self._require_auth(self.WEIBO_DETAIL_ACTION)
        if not mblogid:
            raise ValueError("weibo: mblogid is required")
        return await self._get(
            _Endpoints.STATUSES_SHOW,
            params={"id": mblogid},
            action=self.WEIBO_DETAIL_ACTION,
            unwrap=False,
        )

    async def get_profile(self, uid: str | int) -> dict[str, Any]:
        """获取指定用户的 profile。

        Args:
            uid: 用户 ID（数字），可传 str/int。

        Returns:
            解包后的 ``data``，主要字段：``user`` (含 ``screen_name`` / ``description`` /
            ``followers_count`` / ``friends_count`` / ``statuses_count`` / ``avatar_hd``
            等) 和 ``more`` / ``block_app`` 等辅助字段。
        """
        self._require_auth(self.PROFILE_ACTION)
        if not str(uid):
            raise ValueError("weibo: uid is required")
        return await self._get(
            _Endpoints.PROFILE_INFO,
            params={"uid": str(uid)},
            action=self.PROFILE_ACTION,
        )

    async def get_user_weibos(
        self,
        uid: str | int,
        *,
        page: int = 1,
        feature: int = 0,
    ) -> dict[str, Any]:
        """获取指定用户的微博列表（分页）。

        Args:
            uid: 用户 ID。
            page: 页码，从 1 开始。
            feature: ``0`` 全部微博，``1`` 原创，``2`` 图片，``3`` 视频，``4`` 音乐，``5`` 文章。

        Returns:
            解包后的 ``data``，主要字段：``list`` (微博数组) / ``total`` / ``since_id`` 等。
            列表元素结构与 ``get_weibo_detail`` 返回值同源。
        """
        self._require_auth(self.USER_WEIBOS_ACTION)
        if not str(uid):
            raise ValueError("weibo: uid is required")
        if page < 1:
            raise ValueError("weibo: page must be >= 1")
        return await self._get(
            _Endpoints.MY_MBLOG,
            params={"uid": str(uid), "page": str(page), "feature": str(feature)},
            action=self.USER_WEIBOS_ACTION,
        )

    # ── 扫码登录 ────────────────────────────────────────────────────────
    #
    # 拆成两个独立步骤，让 workflow 场景可以：
    #   1) 收到"登录"指令 → qr_login_start() → 把 image_url 发给用户
    #   2) 用户扫码 → workflow 每 2s 调 qr_login_check()，直到 success/expired
    # 二维码 URL 是公网直链，可以直接以 IM 图片段的形式发给用户。

    @staticmethod
    async def qr_login_start(*, http_timeout: float = 30.0) -> QRSession:
        """扫码登录第一步 —— 申请二维码。

        流程（reverse-engineered from passport.weibo.com）:

        1. GET ``/sso/signin`` 拿到 ``X-CSRF-TOKEN`` cookie
        2. GET ``/sso/v2/qrcode/image`` 返回 ``qrid`` + ``image`` URL

        Returns:
            :class:`QRSession` — 包含 ``qrid`` / ``image_url`` (可直接发给用户看) /
            ``passport_cookies`` (后续 ``qr_login_check`` 必须带上)。

        Raises:
            WeiboApiError: passport 拒绝、缺 CSRF token 或返回 retcode != success。

        注意：这是一个 ``@staticmethod``，因为登录发生前还没有会话 cookie，也不需要
        走主客户端的节流。调用方直接用 ``Weibo.qr_login_start()`` 即可。
        """
        async with httpx.AsyncClient(
            base_url=_Endpoints.PASSPORT_URL,
            headers=dict(_PASSPORT_HEADERS),
            follow_redirects=True,
            timeout=httpx.Timeout(http_timeout),
        ) as client:
            # Step 1: 拿 CSRF token
            resp = await client.get(
                _Endpoints.SSO_SIGNIN,
                params={
                    "entry": _QR_ENTRY,
                    "source": _QR_SOURCE,
                    "url": _QR_REDIRECT_URL,
                },
            )
            resp.raise_for_status()
            csrf_token = client.cookies.get("X-CSRF-TOKEN")
            if not csrf_token:
                raise WeiboApiError("weibo: 未能从 passport 获取 X-CSRF-TOKEN")
            client.headers["x-csrf-token"] = csrf_token

            # Step 2: 请求二维码
            resp = await client.get(
                _Endpoints.QR_IMAGE,
                params={"entry": _QR_ENTRY, "size": "180"},
            )
            resp.raise_for_status()
            qr_data = resp.json()
            if qr_data.get("retcode") != _RETCODE_SUCCESS:
                raise WeiboApiError(
                    f"weibo: 申请二维码失败: {qr_data.get('msg', 'Unknown')}",
                    code=qr_data.get("retcode"),
                    response=qr_data,
                )

            data = qr_data.get("data") or {}
            qrid = data.get("qrid") or ""
            image_url = data.get("image") or ""
            if not qrid or not image_url:
                raise WeiboApiError("weibo: 二维码响应缺少 qrid 或 image 字段", response=qr_data)

            # 图片 URL 里 data= 参数编码了扫码后要访问的 URL；备用给调用方做兜底渲染
            parsed = urlparse(image_url)
            qs = parse_qs(parsed.query)
            scan_url = qs.get(
                "data",
                [f"https://passport.weibo.cn/signin/qrcode/scan?qr={qrid}"],
            )[0]

            passport_cookies = {name: value for name, value in client.cookies.items() if value}
            logger.info("weibo: qr_login_start ok qrid=%s", qrid[:20])
            return QRSession(
                qrid=qrid,
                image_url=image_url,
                scan_url=scan_url,
                passport_cookies=passport_cookies,
            )

    @staticmethod
    async def qr_login_check(session: QRSession, *, http_timeout: float = 15.0) -> QRLoginResult:
        """扫码登录第二步 —— 查询扫码状态。

        单次查询，不做内部循环。调用方按自己的节奏（推荐 2s）反复调用即可。
        期望的用法::

            session = await Weibo.qr_login_start()
            # 把 session.image_url 发给用户
            while True:
                result = await Weibo.qr_login_check(session)
                if result.status == "success":
                    wb = Weibo(cookies=result.cookies)  # 完成登录
                    break
                if result.status == "expired":
                    raise WeiboQRExpiredError()
                await asyncio.sleep(2)

        Returns:
            :class:`QRLoginResult` — ``status`` 取值见 :data:`QRLoginStatus`；
            ``success`` 时 ``cookies`` 已合并 passport + crossdomain + alt 三处。

        Raises:
            WeiboApiError: 网络错误或响应结构异常。
        """
        async with httpx.AsyncClient(
            base_url=_Endpoints.PASSPORT_URL,
            headers=dict(_PASSPORT_HEADERS),
            cookies=session.passport_cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(http_timeout),
        ) as client:
            csrf_token = session.passport_cookies.get("X-CSRF-TOKEN", "")
            if csrf_token:
                client.headers["x-csrf-token"] = csrf_token

            resp = await client.get(
                _Endpoints.QR_CHECK,
                params={
                    "entry": _QR_ENTRY,
                    "source": _QR_SOURCE,
                    "url": _QR_REDIRECT_URL,
                    "qrid": session.qrid,
                    "rid": "",
                    "ver": _QR_VERSION,
                },
            )
            resp.raise_for_status()
            check_data = resp.json()
            retcode = check_data.get("retcode")
            message = str(check_data.get("msg") or "")

            if retcode == _RETCODE_QR_NOT_SCANNED:
                return QRLoginResult(status="waiting", retcode=retcode, message=message)

            if retcode == _RETCODE_QR_SCANNED:
                return QRLoginResult(status="scanned", retcode=retcode, message=message)

            if retcode == _RETCODE_QR_EXPIRED:
                return QRLoginResult(status="expired", retcode=retcode, message=message)

            if retcode == _RETCODE_SUCCESS:
                cookies = await _finalize_qr_login(check_data, session, client)
                if not cookies:
                    raise WeiboApiError("weibo: 扫码成功但未获取到 session cookies")
                logger.info("weibo: qr_login success (%d cookies)", len(cookies))
                return QRLoginResult(
                    status="success",
                    cookies=cookies,
                    retcode=retcode,
                    message=message,
                )

            # 兜底：观察日志再决定是否要归入 waiting/expired
            # 有些文案里带"扫描"/"已扫"关键字但 retcode 未定义
            if "扫" in message:
                return QRLoginResult(status="scanned", retcode=retcode, message=message)
            if "过期" in message or "expired" in message.lower():
                return QRLoginResult(status="expired", retcode=retcode, message=message)
            logger.warning("weibo: qr_check unknown retcode=%s msg=%s", retcode, message)
            return QRLoginResult(status="unknown", retcode=retcode, message=message)


async def _finalize_qr_login(
    check_data: dict[str, Any],
    session: QRSession,
    passport_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """扫码成功后合并 passport + crossdomain + alt 三处 cookie。

    Weibo 的 SSO 会先返回一个 ``crossdomain`` URL，跳过去才能拿到 ``.weibo.com``
    域下的 SUB / SUBP。有时还会附带一个 ``alt`` token，需要走
    ``login.sina.com.cn/sso/login.php`` 换。参考 weibo-cli 实现，两个都尝试。
    """
    cookies: dict[str, Any] = dict(session.passport_cookies)
    cookies.update(passport_client.cookies.items())

    data = check_data.get("data") or {}
    cross_url = data.get("url") or ""
    alt = data.get("alt") or ""

    ua = _PASSPORT_HEADERS["User-Agent"]

    if cross_url:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30),
                headers={"User-Agent": ua},
            ) as cross:
                cross_resp = await cross.get(cross_url)
                cookies.update(cross_resp.cookies.items())
                cookies.update(cross.cookies.items())
        except Exception as exc:
            logger.warning("weibo: crossdomain follow failed: %s", exc)

    if alt:
        try:
            alt_url = (
                "https://login.sina.com.cn/sso/login.php"
                f"?entry={_QR_ENTRY}&alt={alt}&returntype=TEXT"
            )
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30),
                headers={"User-Agent": ua},
            ) as alt_client:
                alt_resp = await alt_client.get(alt_url)
                cookies.update(alt_resp.cookies.items())
                cookies.update(alt_client.cookies.items())
        except Exception as exc:
            logger.warning("weibo: alt token exchange failed: %s", exc)

    return cookies

if __name__ == "__main__":
    # 简单接口冒烟测试 —— 直接运行本文件，需登录接口用下面的硬编码 cookies。
    # 后续接入 hub 后就删掉这一段。

    import json


    # TODO: 填自己的 cookies，或先跑 QR 登录段拿到再填回来
    COOKIES: dict[str, str] = {
        "SUB": "",
        "SUBP": "",
    }
    # 微博 mblogid（短 ID）和 uid，随便找一条公开微博 / 用户即可
    TEST_MBLOGID = "Qw06Kd98p"
    TEST_UID = "1871802012"  # 央视新闻

    async def _smoke() -> None:
        # 1. 热搜（公开，不需登录）
        async with Weibo() as wb:
            hot = await wb.get_hot_search()
        print("\n[hot_search]: ")
        print(json.dumps(hot, ensure_ascii=False, indent=2))
        realtime = hot.get("realtime") or []
        print(f"\n[hot_search] realtime top5 of {len(realtime)}:")
        for i, item in enumerate(realtime[:5], 1):
            print(f"  {i}. {item.get('word', '?'):<30} num={item.get('num', 0)}")

        # 2. 完整热搜榜（公开）
        async with Weibo() as wb:
            band = await wb.get_hot_band()
        print(f"\n[hot_band]: ")
        print(json.dumps(band, ensure_ascii=False, indent=2))
        band_list = band.get("band_list") or []
        print(f"\n[hot_band] band_list top5 of {len(band_list)}:")
        for i, item in enumerate(band_list[:5], 1):
            label = item.get("label_name") or ""
            print(f"  {i}. [{label:<3}] {item.get('word', '?')}")

        # 3. 需登录的接口
        if not COOKIES.get("SUB") or not COOKIES.get("SUBP"):
            print("\n[skip] COOKIES 未填，跳过 detail/profile/weibos")
            return

        async with Weibo(cookies=COOKIES) as wb:
            detail = await wb.get_weibo_detail(TEST_MBLOGID)
            print(f"\n[detail]:")
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            print(f"\n[detail] {TEST_MBLOGID}:")
            print(f"  user     : {(detail.get('user') or {}).get('screen_name')}")
            print(f"  reposts  : {detail.get('reposts_count', 0)}")
            print(f"  comments : {detail.get('comments_count', 0)}")
            text = (detail.get("text_raw") or "").strip()
            print(f"  text     : {text[:120]}{'...' if len(text) > 120 else ''}")

            profile = await wb.get_profile(TEST_UID)
            user = profile.get("user") or {}
            print(f"\n[profile]:")
            print(json.dumps(profile, ensure_ascii=False, indent=2))
            print(f"\n[profile] uid={TEST_UID}:")
            print(f"  screen_name : {user.get('screen_name')}")
            print(f"  followers   : {user.get('followers_count', 0)}")
            print(f"  statuses    : {user.get('statuses_count', 0)}")

            weibos = await wb.get_user_weibos(TEST_UID, page=1)
            print(f"\n[weibos]:")
            print(json.dumps(weibos, ensure_ascii=False, indent=2))
            items = weibos.get("list") or []
            print(f"\n[weibos] uid={TEST_UID} page=1 count={len(items)}")
            for i, item in enumerate(items[:3], 1):
                t = (item.get("text_raw") or "").strip().replace("\n", " ")
                print(f"  {i}. [{item.get('created_at', '?')}] {t[:80]}")

    async def _smoke_qr() -> None:
        # 单独跑扫码登录冒烟：拿到二维码 URL → 每 2s 轮询直到 success/expired。
        # 想跑就把下面的 asyncio.run(_smoke_qr()) 打开、_smoke() 那行注释掉。
        session = await Weibo.qr_login_start()
        print(f"[qr] qrid    = {session.qrid[:24]}...")
        print(f"[qr] image   = {session.image_url}")
        print(f"[qr] scan    = {session.scan_url}")
        print("[qr] 请用微博 APP 扫码，最多轮询 4 分钟...")

        for i in range(120):
            result = await Weibo.qr_login_check(session)
            print(f"[qr #{i + 1:>3}] status={result.status} retcode={result.retcode} msg={result.message!r}")
            if result.status == "success":
                print(f"[qr] ✅ 成功！cookies keys: {sorted(result.cookies)}")
                # cookies keys: ['ALC', 'ALF', 'SCF', 'SUB', 'SUBP', 'X-CSRF-TOKEN']
                print(f"[qr] cookies (完整) = {result.cookies}")
                # {'X-CSRF-TOKEN': '', 'SCF': '', 'SUB': '', 'SUBP': '', 'ALF': '', 'ALC': ''}
                return
            if result.status == "expired":
                print("[qr] ❌ 已过期")
                return
            await asyncio.sleep(2)
        print("[qr] ⏰ 超时未完成")

    asyncio.run(_smoke())
    # asyncio.run(_smoke_qr())
    