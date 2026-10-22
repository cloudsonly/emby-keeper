from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession, RequestsError

from embykeeper.runinfo import RunStatus
from embykeeper.turnstile import TurnstileService
from embykeeper.utils import get_proxy_str, show_exception, truncate_str

from ._base import BaseBotCheckin

__ignore__ = True

DEFAULT_BASE_URL = "https://mambo-hachimi.biliblili.uk"
ACCESS_BLOCKED_MESSAGE = "站点安全验证拦截"


def normalize_base_url(url: str | None) -> str:
    base = (url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return base


def build_login_payload(
    username: str, password: str, verification_token: str | None = None
) -> dict[str, str]:
    payload = {
        "userName": username.strip(),
        "password": password,
        "recaptchaToken": "",
        "verificationToken": verification_token or "",
    }
    return payload


def extract_message(payload: Any, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return fallback
    for key in ("message", "msg", "detail", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def verification_required(settings: Any, action: str) -> bool:
    if not isinstance(settings, dict):
        return False
    action_settings = settings.get(action)
    if not isinstance(action_settings, dict):
        return False
    verification_type = action_settings.get("type")
    return bool(action_settings.get("enabled") and verification_type != "none")


def verification_type(settings: Any, action: str) -> str:
    """取出该动作使用的验证类型 (turnstile / recaptcha / hcaptcha / none)."""
    if not isinstance(settings, dict):
        return ""
    action_settings = settings.get(action)
    if not isinstance(action_settings, dict):
        return ""
    return str(action_settings.get("type") or "").strip().lower()


def verification_site_key(settings: Any, action: str) -> str:
    """取出该动作对应的 Turnstile siteKey, 缺失时返回空串.

    不同版本的結束バンド面板字段名不一致, 这里兼容 siteKey / site_key / sitekey,
    以及嵌套在 turnstile 子对象里的写法.
    """
    if not isinstance(settings, dict):
        return ""
    action_settings = settings.get(action)
    if not isinstance(action_settings, dict):
        return ""
    for key in ("siteKey", "site_key", "sitekey"):
        value = action_settings.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = action_settings.get("turnstile")
    if isinstance(nested, dict):
        for key in ("siteKey", "site_key", "sitekey"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def classify_status_response(status_code: int, payload: Any) -> tuple[RunStatus, str]:
    if status_code == 401:
        return RunStatus.FAIL, "登录已失效"
    if status_code == 403:
        return RunStatus.FAIL, ACCESS_BLOCKED_MESSAGE
    if not 200 <= status_code < 300 or not isinstance(payload, dict):
        return RunStatus.FAIL, extract_message(payload, "获取签到状态失败")
    if payload.get("success") is False:
        return RunStatus.FAIL, extract_message(payload, "获取签到状态失败")
    checked_in = payload.get("hasCheckedInToday")
    if checked_in is True:
        return RunStatus.NONEED, "签到前查询确认今日已签到, 跳过提交"
    if checked_in is False:
        return RunStatus.RUNNING, "尚未签到"
    return RunStatus.FAIL, extract_message(payload, "签到状态响应异常")


def classify_checkin_response(status_code: int, payload: Any) -> tuple[RunStatus, str]:
    if status_code == 401:
        return RunStatus.FAIL, "登录已失效"
    if status_code == 403:
        return RunStatus.FAIL, ACCESS_BLOCKED_MESSAGE
    if not isinstance(payload, dict):
        return RunStatus.FAIL, extract_message(payload, "签到失败")

    success = payload.get("success") is True
    if 200 <= status_code < 300 and success:
        amount = payload.get("amount")
        currency = payload.get("currencyUnit")
        if amount is not None and currency:
            return RunStatus.SUCCESS, f"+{amount} {currency}"
        return RunStatus.SUCCESS, extract_message(payload, "签到成功")

    if (
        (200 <= status_code < 300 or status_code in {400, 409})
        and not success
        and payload.get("hasCheckedIn") is True
    ):
        return RunStatus.NONEED, "提交时站点确认今日已签到"
    return RunStatus.FAIL, extract_message(payload, "签到失败")


def summarize_results(results: list[tuple[str, RunStatus, str]]) -> tuple[RunStatus, str]:
    """聚合多站点签到结果.

    任一站点失败即整体失败 (否则部分站点静默失败不会被通知暴露);
    全部无需签到时为 NONEED; 否则只要有站点成功即为 SUCCESS.
    """
    if len(results) == 1:
        _, status, message = results[0]
        return status, message

    total = len(results)
    failed = [(label, message) for label, status, message in results if status == RunStatus.FAIL]
    succeeded = [(label, message) for label, status, message in results if status == RunStatus.SUCCESS]
    if failed:
        detail = "; ".join(f"{label}{message}" for label, message in failed)
        return RunStatus.FAIL, f"{total - len(failed)}/{total} 成功, 失败: {detail}"
    if succeeded:
        detail = "; ".join(f"{label}{message}" for label, message in succeeded)
        return RunStatus.SUCCESS, f"{len(succeeded)}/{total} 签到成功: {detail}"
    return RunStatus.NONEED, f"{total} 个站点今日均已签到"


class PlusembyCheckin(BaseBotCheckin):
    name = "結束バンド"

    @property
    def entry(self) -> dict:
        """当前正在处理的站点配置.

        单站点时即 self.config; 多站点时由 start() 逐项切换为 accounts 中的元素.
        """
        entry = getattr(self, "_entry", None)
        return entry if isinstance(entry, dict) else self.config

    def entries(self) -> list[dict]:
        """站点配置列表.

        支持在一份配置内跑多个 plusemby 站点:
            [checkiner.plusemby]
            [[checkiner.plusemby.accounts]]
            url = "https://a.example.com"
            username = "user_a"
            password = "pass_a"

        未配置 accounts (或其中没有合法条目) 时退化为单站点, 沿用顶层字段.
        """
        accounts = self.config.get("accounts")
        if isinstance(accounts, list):
            valid = [account for account in accounts if isinstance(account, dict)]
            if valid:
                return valid
        return [self.config]

    @property
    def base_url(self) -> str:
        return normalize_base_url(self.entry.get("url"))

    @property
    def username(self) -> str:
        return str(self.entry.get("username") or "").strip()

    @property
    def password(self) -> str:
        return str(self.entry.get("password") or "")

    @property
    def access_token(self) -> str:
        return str(self.entry.get("access_token") or "").strip()

    @property
    def use_proxy(self) -> bool:
        return self.entry.get("use_proxy", False) is True

    def api_url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def client_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": self.api_url("/login"),
        }
        if self.entry.get("useragent"):
            headers["User-Agent"] = str(self.entry["useragent"])
        return headers

    async def request_json(self, session: AsyncSession, method: str, path: str, **kwargs) -> tuple[int, Any]:
        response = await session.request(method, self.api_url(path), **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": truncate_str(response.text, 200) or "服务返回非 JSON 响应"}
        return response.status_code, payload

    async def get_verification_settings(self, session: AsyncSession) -> Any:
        status_code, payload = await self.request_json(session, "GET", "/api/settings/verification/public")
        if status_code == 403:
            raise RuntimeError(ACCESS_BLOCKED_MESSAGE)
        if status_code >= 400 or not isinstance(payload, dict):
            raise RuntimeError("无法读取验证配置")
        return payload

    def configured_verification_token(self, action: str) -> str:
        return str(self.entry.get(f"{action}_verification_token") or "").strip()

    def _turnstile_service(self) -> TurnstileService:
        # TurnstileService 自身会按 构造参数 > 环境变量 > config.toml [turnstile] 解析,
        # 因此不显式传参, 以便同时利用 solver_url 多地址轮换与 YesCaptcha.
        return TurnstileService()

    async def resolve_verification_token(self, settings: Any, action: str, *, fresh: bool = False) -> str:
        """取得该动作所需的 Turnstile token.

        优先使用配置中的静态 token (便于临时排障); 站点要求验证时改为实时求解,
        因为 Turnstile token 一次性且很快过期, 写死在配置里对定时任务无意义。
        """
        configured = self.configured_verification_token(action)
        if configured and not fresh:
            return configured
        if not verification_required(settings, action):
            return ""
        vtype = verification_type(settings, action)
        if vtype and vtype != "turnstile":
            self.log.warning(f"站点要求 {action} 使用 {vtype} 验证, 当前仅支持 Turnstile, 无法求解.")
            return ""
        site_key = verification_site_key(settings, action)
        if not site_key:
            self.log.warning(f"站点要求 {action} 验证但未提供 siteKey, 无法求解.")
            self.log.debug(f"站点验证配置 (便于排查字段名): {settings}")
            return ""
        try:
            service = self._turnstile_service()
            token = await service.solve_turnstile_async(
                self.api_url("/login"), site_key, timeout=float(self.timeout)
            )
        except Exception as e:
            self.log.warning(f"Turnstile 求解失败 ({action}): {e.__class__.__name__}: {e}")
            return ""
        return str(token or "").strip()

    async def login(
        self, session: AsyncSession, settings: Any, *, fresh_verification: bool = False
    ) -> tuple[RunStatus, str, str]:
        if self.access_token:
            return RunStatus.RUNNING, "已使用访问令牌", self.access_token
        if not self.username or not self.password:
            return RunStatus.FAIL, "缺少账号配置", ""

        verification_token = await self.resolve_verification_token(
            settings, "login", fresh=fresh_verification
        )
        if verification_required(settings, "login") and not verification_token:
            return RunStatus.FAIL, "登录需要 Turnstile 验证", ""

        status_code, payload = await self.request_json(
            session,
            "POST",
            "/api/auth/login",
            json=build_login_payload(self.username, self.password, verification_token),
        )
        if status_code == 403:
            return RunStatus.FAIL, ACCESS_BLOCKED_MESSAGE, ""
        token = payload.get("token") if isinstance(payload, dict) else None
        if status_code >= 400 or not isinstance(token, str) or not token.strip():
            return RunStatus.FAIL, extract_message(payload, "登录失败"), ""
        return RunStatus.RUNNING, "登录成功", token.strip()

    def _access_token_cache_key(self) -> str:
        identity = json.dumps([self.base_url, self.username], ensure_ascii=False)
        return f"plusemby.access_token.{sha256(identity.encode()).hexdigest()}"

    def cached_access_token(self) -> str:
        """读取上次登录缓存的 JWT, 复用它可省下一次 Turnstile 求解.

        缓存只是优化手段, 任何读写失败都退化为"没有缓存", 不影响正常登录签到。
        """
        if not self.username:
            return ""
        try:
            from embykeeper.cache import cache

            token = cache.get(self._access_token_cache_key())
            return token.strip() if isinstance(token, str) else ""
        except Exception:
            return ""

    def store_access_token(self, token: str) -> None:
        if not self.username or not isinstance(token, str) or not token.strip():
            return
        try:
            from embykeeper.cache import cache

            cache.set(self._access_token_cache_key(), token.strip())
        except Exception as e:
            self.log.debug(f"访问令牌缓存写入失败: {e.__class__.__name__}")

    def clear_access_token(self) -> None:
        if not self.username:
            return
        try:
            from embykeeper.cache import cache

            cache.delete(self._access_token_cache_key())
        except Exception as e:
            self.log.debug(f"访问令牌缓存清理失败: {e.__class__.__name__}")

    async def login_and_cache(
        self, session: AsyncSession, *, fresh_verification: bool = False
    ) -> tuple[RunStatus, str, str, Any]:
        """执行一次真实登录 (会消耗一次 Turnstile 求解) 并缓存所得 JWT."""
        settings = await self.get_verification_settings(session)
        status, message, token = await self.login(session, settings, fresh_verification=fresh_verification)
        if status != RunStatus.FAIL:
            self.store_access_token(token)
        return status, message, token, settings

    async def run_api_flow(self, session: AsyncSession) -> tuple[RunStatus, str]:
        if not self.access_token and (not self.username or not self.password):
            return RunStatus.FAIL, "缺少账号配置"

        settings = None
        # 配置中的静态令牌优先; 其次复用缓存的 JWT, 都没有才登录
        token = self.access_token or self.cached_access_token()
        for attempt in range(2):
            if not token:
                status, message, token, settings = await self.login_and_cache(
                    session, fresh_verification=bool(attempt)
                )
                if status == RunStatus.FAIL:
                    return status, message

            headers = {"Authorization": f"Bearer {token}"}
            status_code, payload = await self.request_json(
                session, "GET", "/api/checkin/status", headers=headers
            )
            if status_code != 401:
                status, message = classify_status_response(status_code, payload)
                if status != RunStatus.RUNNING:
                    return status, message
                if settings is None:
                    settings = await self.get_verification_settings(session)

                verification_token = await self.resolve_verification_token(
                    settings, "checkin", fresh=bool(attempt)
                )
                if verification_required(settings, "checkin") and not verification_token:
                    return RunStatus.FAIL, "签到需要 Turnstile 验证"

                body = {"verificationToken": verification_token} if verification_token else {}
                status_code, payload = await self.request_json(
                    session, "POST", "/api/checkin", headers=headers, json=body
                )
                if status_code != 401:
                    return classify_checkin_response(status_code, payload)

            if not self.access_token:
                self.clear_access_token()
            if self.access_token or attempt:
                return RunStatus.FAIL, "登录已失效"
            self.log.debug("访问令牌已失效, 重新登录后复查签到状态.")
            token, settings = "", None

    def _entry_label(self, index: int, total: int) -> str:
        """多站点时为日志加站点前缀, 单站点时保持原输出不变."""
        if total == 1:
            return ""
        return f"[{index}/{total} {self.base_url}] "

    def _log_result(self, label: str, status: RunStatus, message: str) -> None:
        if status == RunStatus.SUCCESS:
            self.log.info(f"{label}[yellow]签到成功[/]: {truncate_str(message, 80)}")
        elif status == RunStatus.NONEED:
            self.log.info(f"{label}{message}.")
        else:
            self.log.warning(f"{label}签到失败: {truncate_str(message, 80)}")

    def _log_summary(self, status: RunStatus, message: str) -> None:
        text = truncate_str(message, 160)
        if status == RunStatus.SUCCESS:
            self.log.info(f"[yellow]多站点签到汇总[/]: {text}")
        elif status == RunStatus.NONEED:
            self.log.info(f"多站点签到汇总: {text}.")
        else:
            self.log.warning(f"[red]多站点签到汇总[/]: {text}")

    async def _run_single(self, global_config, label: str) -> tuple[RunStatus, str]:
        """对当前 self.entry 执行一次完整签到流程."""
        api_proxy = get_proxy_str(global_config.proxy, curl=True) if self.use_proxy else None
        try:
            async with (
                asyncio.timeout(float(self.timeout)),
                AsyncSession(
                    proxy=api_proxy,
                    impersonate="edge",
                    allow_redirects=True,
                    timeout=float(self.timeout),
                    headers=self.client_headers(),
                ) as session,
            ):
                return await self.run_api_flow(session)
        except TimeoutError:
            # TimeoutError 在 3.11+ 是 OSError 子类, 必须排在 OSError 之前.
            self.log.warning(f"{label}签到超过总超时时间, 已停止当前请求和验证码求解.")
            return RunStatus.FAIL, "签到超时"
        except (RequestsError, OSError) as error:
            self.log.warning(f"{label}无法连接到結束バンド页面或接口 ({error.__class__.__name__}).")
            return RunStatus.FAIL, "网络错误"
        except RuntimeError as error:
            # get_verification_settings 以异常表达 "安全验证拦截/无法读取配置", 转为具体的失败原因
            self.log.warning(f"{label}签到失败: {truncate_str(str(error), 80)}")
            return RunStatus.FAIL, str(error)
        except Exception as error:
            # 单站点异常不应中断其余站点, 否则多站点配置下会全部跳过.
            self.log.warning(f"{label}签到异常, 已跳过该站点.")
            show_exception(error, regular=False)
            return RunStatus.FAIL, "异常错误"

    async def start(self):
        from embykeeper.config import config as global_config

        self.ctx.start(RunStatus.INITIALIZING)
        self.ctx.status = RunStatus.RUNNING

        entries = self.entries()
        total = len(entries)
        results: list[tuple[str, RunStatus, str]] = []
        for index, entry in enumerate(entries, 1):
            self._entry = entry
            label = self._entry_label(index, total)
            status, message = await self._run_single(global_config, label)
            self._log_result(label, status, message)
            results.append((label, status, message))

        status, message = summarize_results(results)
        if total > 1:
            self._log_summary(status, message)
        return self.ctx.finish(status, message)
