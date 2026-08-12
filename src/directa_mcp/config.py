import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    trading_host: str
    trading_port: int
    historical_port: int
    #: Whether the order tools may send anything to Darwin at all. There is no
    #: simulated alternative — Directa provides no test account for API
    #: development and the dAPI has no simulation command — so this is a safety
    #: catch, not a mode switch: off, orders are impossible; on, they are real.
    #: It lives in the server's environment specifically so that the model
    #: driving the tools cannot change it.
    orders_enabled: bool


def load_settings() -> Settings:
    return Settings(
        trading_host=os.environ.get("DIRECTA_TRADING_HOST", "127.0.0.1"),
        trading_port=int(os.environ.get("DIRECTA_TRADING_PORT", "10002")),
        historical_port=int(os.environ.get("DIRECTA_HISTORICAL_PORT", "10003")),
        orders_enabled=_bool_env("DIRECTA_ENABLE_ORDERS", False),
    )


settings = load_settings()
