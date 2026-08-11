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
    live_trading: bool


def load_settings() -> Settings:
    return Settings(
        trading_host=os.environ.get("DIRECTA_TRADING_HOST", "127.0.0.1"),
        trading_port=int(os.environ.get("DIRECTA_TRADING_PORT", "10002")),
        historical_port=int(os.environ.get("DIRECTA_HISTORICAL_PORT", "10003")),
        live_trading=_bool_env("DIRECTA_LIVE_TRADING", False),
    )


settings = load_settings()
