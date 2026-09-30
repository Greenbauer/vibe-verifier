"""Read-only local CI dashboard companion for Vibe Verifier."""

from .config import Config, ConfigError, load_config
from .service import DashboardService

__all__ = ["Config", "ConfigError", "DashboardService", "load_config"]
