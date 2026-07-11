"""Notification transports and error-notification throttling."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping

from .config_safety import resolve_secret


class NotificationService:
    def __init__(
        self,
        app_dir: str | os.PathLike[str],
        timezone: tzinfo,
        config_provider: Callable[[], Mapping[str, Any]],
        formatter: Callable[[str, str], str],
        status_callback: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._app_dir = Path(app_dir)
        self._timezone = timezone
        self._config_provider = config_provider
        self._formatter = formatter
        self._status_callback = status_callback
        self._last_error_notify: dict[str, float] = {}

    @property
    def config(self) -> Mapping[str, Any]:
        return self._config_provider()

    def _write_fallback_log(self, message: str) -> None:
        path = self._app_dir / "logs" / "trade_log.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now(self._timezone):%H:%M}] {message}\n")

    def send_feishu(self, message: str) -> bool:
        try:
            import requests

            env_path = Path.home() / ".hermes" / ".env"
            app_id = app_secret = None
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("FEISHU_APP_ID"):
                        app_id = line.split("=", 1)[1].strip()
                    elif line.strip().startswith("FEISHU_APP_SECRET"):
                        app_secret = line.split("=", 1)[1].strip()
            if not app_id or not app_secret:
                print(f"  ⚠️ 飞书凭据未配置，写日志: {message}")
                self._write_fallback_log(message)
                return False

            token_response = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            token_data = token_response.json()
            if token_data.get("code") != 0:
                print(f"  ⚠️ 飞书token获取失败: {token_data}")
                return False

            open_id = self.config.get("feishu", {}).get("open_id", "")
            response = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers={
                    "Authorization": f"Bearer {token_data['tenant_access_token']}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": f"[QQQ Trader]\n{message}"}),
                },
                timeout=10,
            )
            result = response.json()
            if result.get("code") == 0:
                print("  ✅ 飞书推送成功")
                return True
            print(f"  ⚠️ 飞书推送失败: {result}")
            return False
        except Exception as error:
            import traceback

            print(f"  ⚠️ 飞书通知异常: {error}")
            traceback.print_exc()
            self._write_fallback_log(message)
            return False

    def send_telegram(self, message: str, msg_type: str = "info", **kwargs: Any) -> bool:
        try:
            import requests

            telegram = self.config.get("telegram", {})
            bot_token = resolve_secret(self.config, "telegram", "bot_token", "TELEGRAM_BOT_TOKEN")
            chat_id = resolve_secret(self.config, "telegram", "chat_id", "TELEGRAM_CHAT_ID")
            if not bot_token or not chat_id:
                print(f"  ⚠️ Telegram凭据未配置，写日志: {message}")
                self._write_fallback_log(message)
                return False

            text = self._formatter(message, msg_type, **kwargs)
            full_text = text + "\n───────────\n<code>QQQ 0DTE v7</code>"
            proxy_url = telegram.get("proxy", "")
            proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else {}
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            def post(payload: dict[str, Any], label: str):
                last_error = None
                for attempt in range(3):
                    try:
                        return requests.post(api_url, json=payload, timeout=15, proxies=proxies)
                    except requests.RequestException as error:
                        last_error = error
                        safe_error = str(error).replace(bot_token, "***TOKEN***")
                        print(f"  ⚠️ Telegram{label}网络异常({attempt + 1}/3): {safe_error[:220]}")
                        if attempt < 2:
                            time.sleep(2 * (attempt + 1))
                raise last_error

            response = post({"chat_id": chat_id, "text": full_text, "parse_mode": "HTML"}, "推送")
            result = response.json()
            if result.get("ok"):
                print("  ✅ Telegram推送成功")
                return True
            print(f"  ⚠️ Telegram推送失败: {result}")
            if "can't parse" in str(result):
                fallback_response = post(
                    {"chat_id": chat_id, "text": f"[QQQ Trader]\n{message}"},
                    "纯文本回退",
                )
                try:
                    fallback = fallback_response.json()
                    if fallback.get("ok"):
                        print("  ✅ Telegram纯文本回退成功")
                        return True
                    print(f"  ⚠️ Telegram纯文本回退失败: {fallback}")
                except Exception:
                    print(f"  ⚠️ Telegram纯文本回退响应异常: {fallback_response.text[:200]}")
            return False
        except Exception as error:
            bot_token = self.config.get("telegram", {}).get("bot_token", "")
            safe_error = str(error).replace(bot_token, "***TOKEN***") if bot_token else str(error)
            print(f"  ⚠️ Telegram通知失败，已记录到本地，后续会重试: {safe_error[:260]}")
            self._write_fallback_log(message)
            return False

    def notify(self, message: str, msg_type: str = "info", **kwargs: Any) -> bool:
        telegram = self.config.get("telegram", {})
        print(f"  [Notify] Telegram: enabled={telegram.get('enabled')}, type={msg_type}")
        results: list[bool] = []
        if self.config.get("feishu", {}).get("enabled", True):
            results.append(self.send_feishu(message))
        if telegram.get("enabled", False):
            results.append(self.send_telegram(message, msg_type=msg_type, **kwargs))
        result = any(results)
        if self._status_callback and results:
            self._status_callback(msg_type, result)
        return result

    def handle_error(
        self,
        error: Exception,
        context: str = "",
        notify_type: str | None = None,
        retry_count: int = 0,
    ) -> bool:
        error_str = str(error).lower()
        now = time.time()
        error_key = f"{context}_{type(error).__name__}"
        if now - self._last_error_notify.get(error_key, 0) < 300:
            return False

        network_keywords = ("connection", "timeout", "network", "socket", "http", "ssl", "dns")
        rate_limit_keywords = ("429", "rate limit", "too many", "throttle", "limit")
        if any(keyword in error_str for keyword in network_keywords):
            self.notify(
                "🌐 网络异常",
                "network",
                error_msg=str(error)[:100],
                retry_count=retry_count,
            )
        elif any(keyword in error_str for keyword in rate_limit_keywords):
            match = re.search(r"(\d+)\s*(?:second|sec|s)", error_str)
            self.notify(
                "⏱️ API限流",
                "rate_limit",
                api_name=context or "Longbridge API",
                wait_seconds=int(match.group(1)) if match else 60,
            )
        elif notify_type:
            self.notify(str(error)[:200], notify_type)
        else:
            return False
        self._last_error_notify[error_key] = now
        return True
