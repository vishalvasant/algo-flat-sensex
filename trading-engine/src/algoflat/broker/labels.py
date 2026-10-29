"""Provider-aware user-facing broker labels."""
from __future__ import annotations

from algoflat.config import get_config


def broker_provider(provider: str | None = None) -> str:
    if provider:
        return str(provider).lower()
    return str(get_config().broker.get("provider", "upstox")).lower()


def broker_display_name(provider: str | None = None) -> str:
    return "Upstox" if broker_provider(provider) == "upstox" else "Flattrade"


def broker_api_key_missing_title(provider: str | None = None) -> str:
    return f"{broker_display_name(provider)} API key missing"


def broker_api_key_missing_message(provider: str | None = None) -> str:
    label = broker_display_name(provider)
    if broker_provider(provider) == "upstox":
        return (
            "Open Settings → Broker and save your Upstox API key, secret, and access token."
        )
    return (
        f"Open Settings → Broker and save your API key and secret from {label} Wall → Pi."
    )


def broker_login_required_title(provider: str | None = None) -> str:
    return f"{broker_display_name(provider)} login required"


def broker_data_source_label(provider: str | None = None) -> str:
    return broker_display_name(provider)
