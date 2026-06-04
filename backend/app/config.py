"""Application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


def _running_in_container() -> bool:
    """Best-effort container runtime detection."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False

    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return any(token in content for token in ("docker", "containerd", "kubepods", "podman"))


def _default_agent_data_dir() -> str:
    """Use Docker path in containers, user-writable path on local hosts."""
    if _running_in_container():
        return "/data/agents"
    return str(Path.home() / ".hive" / "data" / "agents")


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path(__file__).resolve().parent.parent.parent / "VERSION",
        Path("/app/VERSION"),
        Path("/VERSION"),
    ]:
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    APP_NAME: str = "Hive"
    APP_VERSION: str = _read_version()
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://hive:hive@localhost:5432/hive"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # File Storage
    AGENT_DATA_DIR: str = _default_agent_data_dir()
    AGENT_TEMPLATE_DIR: str = "/app/agent_template"

    # OfficeCLI (agentic document editing core)
    OFFICECLI_BIN: str = "officecli"
    OFFICECLI_SHA256: str = ""
    OFFICECLI_TIMEOUT_SECONDS: int = 45

    # ONLYOFFICE DocumentServer
    ONLYOFFICE_DOCS_URL: str = ""
    ONLYOFFICE_INTERNAL_DOCS_URL: str = ""
    ONLYOFFICE_JWT_SECRET: str = ""
    ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS: int = 300

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "hive_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    FEISHU_CLI_ENABLED: bool = False
    FEISHU_CLI_BIN: str = "lark-cli"
    FEISHU_CLI_TIMEOUT_SECONDS: int = 30
    FEISHU_CLI_IDENTITY: str = "bot"

    # Desktop Auth Bridge
    DESKTOP_DEEP_LINK_SCHEME: str = "copaw"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    BASE_URL: str = ""
    PUBLIC_BASE_URL: str = ""

    # Secrets encryption (set a strong random string in production)
    SECRETS_MASTER_KEY: str = ""

    # OpenViking knowledge backbone (optional — set URL to enable)
    OPENVIKING_URL: str = ""

    # Self-evolution cadence (P1-W2-5)
    # Tick frequency for the evolution daemon (heartbeat dispatcher).
    # Production: 60s. Dev/staging may set 5-15s to exercise the path.
    HEARTBEAT_TICK_SECONDS: int = 60
    # Default per-agent heartbeat interval when an Agent row has no override.
    # Production: 45 minutes (matches the legacy mapped_column default).
    HEARTBEAT_DEFAULT_INTERVAL_MINUTES: int = 45

    # Capability mapping enforcement (P1-W2-8)
    # When True, any tool absent from CAPABILITY_MAP is denied at the
    # capability gate (fail-closed). Set False only for explicit local
    # compatibility windows; unmapped tools are always logged + counted.
    STRICT_CAPABILITY_MAPPING: bool = True

    # Workflow admission thresholds (§9 P2) — hard caps for run preflight;
    # admission rejects (never warns) past these. Env-overridable per deploy.
    WORKFLOW_MAX_RUN_BUDGET_TOKENS: int = 2_000_000
    WORKFLOW_MAX_FANOUT_ITEMS: int = 16
    WORKFLOW_MAX_CONCURRENCY: int = 8
    WORKFLOW_MAX_LEAF_CALLS: int = 64
    WORKFLOW_MAX_WALL_CLOCK_SECONDS: int = 86_400

    # Workflow risk-grading thresholds (§9 P4, §10 decision 3) — past any of
    # these a launch is HIGH risk and must carry a confirmed plan.
    WORKFLOW_HIGH_RISK_BUDGET_TOKENS: int = 500_000
    WORKFLOW_HIGH_RISK_FANOUT_ITEMS: int = 8
    WORKFLOW_HIGH_RISK_WAIT_SECONDS: int = 3_600
    # Per-leaf token pre-reservation for the run quota envelope (§9 P5):
    # reserved before each spawn under an advisory lock, settled with actual.
    WORKFLOW_LEAF_TOKEN_ESTIMATE: int = 20_000
    # Repeated-ephemeral evidence (§9 P13 / §4): suggest 保存为模板 once the
    # same definition_hash completes this many ephemeral runs.
    WORKFLOW_PROMOTE_SUGGESTION_THRESHOLD: int = 3

    # Coordination backend (Phase 17 wiring)
    # "memory" — in-process Lease/Signal/Checkpoint (default, fine for
    # single-process Hive deployments).
    # "postgres" — durable PostgreSQL-backed coordination; requires the
    # caller to provide an AsyncSession + tenant_id when picking a
    # gateway (see `app/agents/coordination_wiring.py:pick_gateway`).
    COORDINATION_BACKEND: str = "memory"
    OPENVIKING_API_KEY: str = ""

    # Hindsight memory backend (read-side accelerator for T3 MD; optional)
    # Phase A: single instance + global API key; isolation via bank_id naming
    # (hive-t{tenant}-a{agent}). When upstream PR #855 merges, migrate to
    # Phase B (per-tenant key + PG schema isolation).
    HINDSIGHT_URL: str = ""
    HINDSIGHT_API_KEY: str = ""
    HINDSIGHT_ENABLED: bool = False
    HINDSIGHT_TIMEOUT_SECONDS: float = 10.0

    # Tavily Search API
    TAVILY_API_KEY: str = ""

    # Exa Search API
    EXA_API_KEY: str = ""

    # Firecrawl / XCrawl scraping APIs
    FIRECRAWL_API_KEY: str = ""
    XCRAWL_API_KEY: str = ""

    model_config = {
        "env_file": [".env", "../.env"],
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
