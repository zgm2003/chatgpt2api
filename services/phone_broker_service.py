from __future__ import annotations

from typing import Any

from services.hero_sms_service import OPENAI_SERVICE_CODE, HeroSmsActivation, HeroSmsClient


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def reserve_phone(config: dict, *, session: Any | None = None) -> HeroSmsActivation:
    """Reserve one phone for Codex add_phone.

    Good taste version: one entrypoint, two cases.
    - reuse_activation_id + reuse_phone: use what the user already bought.
    - otherwise buy only when auto_buy is explicitly enabled, with maxPrice.
    """
    activation_id = str(config.get("reuse_activation_id") or "").strip()
    phone = str(config.get("reuse_phone") or "").strip()
    if activation_id and phone:
        return HeroSmsActivation(activation_id=activation_id, phone=phone, raw="REUSE_ACTIVATION")

    if not bool(config.get("auto_buy")):
        raise RuntimeError("HeroSMS auto_buy 未启用，未填写复用号码，不会自动买号")

    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("HeroSMS auto_buy 已启用，但 api_key 为空")

    client = HeroSmsClient(
        api_key,
        session=session,
        poll_interval=_positive_float(config.get("poll_interval"), 5.0),
    )
    try:
        return client.get_number(
            service=str(config.get("service") or OPENAI_SERVICE_CODE).strip() or OPENAI_SERVICE_CODE,
            country=int(config.get("country") or 16),
            operator=str(config.get("operator") or "any").strip() or "any",
            max_price=_positive_float(config.get("max_price_usd"), 0.03),
        )
    finally:
        client.close()
