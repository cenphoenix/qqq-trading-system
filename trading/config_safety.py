"""Configuration validation, redaction, and secret resolution."""

from __future__ import annotations

import copy
import os
from datetime import datetime
from typing import Any, Mapping


SENSITIVE_TOKENS = ("token", "secret", "password", "api_key", "private_key")


def redact_config(value: Any, key: str = "") -> Any:
    """Return a deep copy with credential-like values removed."""
    if isinstance(value, Mapping):
        return {str(k): redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(item, key) for item in value]
    if any(token in key.lower() for token in SENSITIVE_TOKENS):
        return "***REDACTED***" if value else ""
    return copy.deepcopy(value)


def resolve_secret(config: Mapping[str, Any], group: str, key: str, env_name: str) -> str:
    """Prefer process environment secrets while retaining local-config compatibility."""
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    group_value = config.get(group, {})
    return str(group_value.get(key, "") if isinstance(group_value, Mapping) else "").strip()


def validate_config(config: Mapping[str, Any]) -> list[str]:
    """Validate dangerous cross-field combinations not covered by scalar schemas."""
    errors: list[str] = []
    risk = config.get("risk", {}) if isinstance(config.get("risk", {}), Mapping) else {}
    trading = config.get("trading", {}) if isinstance(config.get("trading", {}), Mapping) else {}

    start = str(trading.get("start_time", "09:30"))
    end = str(trading.get("end_time", "16:00"))
    try:
        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
        if start_time >= end_time:
            errors.append("trading.start_time must be earlier than trading.end_time")
    except ValueError:
        errors.append("trading start/end time must use HH:MM")

    order_pct = float(risk.get("order_pct", 0) or 0)
    put_pct = float(risk.get("put_order_pct", order_pct) or 0)
    if not 0 < order_pct <= 60:
        errors.append("risk.order_pct must be between 0 and 60")
    if not 0 < put_pct <= 40:
        errors.append("risk.put_order_pct must be between 0 and 40")
    if float(risk.get("daily_limit", 0) or 0) <= 0:
        errors.append("risk.daily_limit must be positive")
    if int(risk.get("max_contracts_per_trade", 1) or 0) <= 0:
        errors.append("risk.max_contracts_per_trade must be positive")

    stage1 = int(risk.get("timeout_stage1_bars", 0) or 0)
    stage2 = int(risk.get("timeout_stage2_bars", 0) or 0)
    stage3 = int(risk.get("timeout_stage3_bars", 0) or 0)
    if stage1 and stage2 and stage2 < stage1:
        errors.append("risk.timeout_stage2_bars cannot be earlier than stage1")
    if stage2 and stage3 and stage3 < stage2:
        errors.append("risk.timeout_stage3_bars cannot be earlier than stage2")

    tiers = risk.get("profit_take_tiers", [])
    if tiers:
        profits = [float(item.get("profit_pct", 0)) for item in tiers if isinstance(item, Mapping)]
        if profits != sorted(profits) or any(value <= 0 for value in profits):
            errors.append("risk.profit_take_tiers must have increasing positive profit_pct values")
        close_total = sum(float(item.get("close_pct", 0) or 0) for item in tiers if isinstance(item, Mapping))
        if close_total > 1.0 + 1e-9:
            errors.append("risk.profit_take_tiers close_pct total cannot exceed 1.0")
    return errors

