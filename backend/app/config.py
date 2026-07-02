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
    # Owner-role connection for schema work (create_all, migrations, RLS policies,
    # GRANTs). After the stage-3 RLS role flip, DATABASE_URL points at the
    # non-owner app_rls role (NOSUPERUSER — cannot run DDL/policies/GRANT), so
    # schema steps route through this owner URL instead. Unset = same as
    # DATABASE_URL (pre-cutover: both are the table owner, no behavior change).
    SCHEMA_DATABASE_URL: str | None = None
    # Runtime RLS role guard. Production should stay "strict": app startup fails
    # if DATABASE_URL connects as a PostgreSQL superuser or a BYPASSRLS role.
    # Local one-off tests may set "warn" or "off" explicitly.
    RLS_RUNTIME_ROLE_ENFORCEMENT: str = "strict"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # File Storage
    AGENT_DATA_DIR: str = _default_agent_data_dir()
    AGENT_TEMPLATE_DIR: str = "/app/agent_template"
    # Startup repair for legacy chat sessions that predate the canonical
    # chat_transcript_events + memory/t0 session ledger path. Runs inside the
    # backend container so Railway's /data/agents volume is available.
    T0_STARTUP_BACKFILL_ENABLED: bool = True
    T0_STARTUP_BACKFILL_RECENT_DAYS: int = 3650
    T0_STARTUP_BACKFILL_MAX_SESSIONS: int = 10000
    T0_STARTUP_BACKFILL_BATCH_SIZE: int = 100

    # OfficeCLI (agentic document editing core)
    OFFICECLI_BIN: str = "officecli"
    OFFICECLI_SHA256: str = ""
    OFFICECLI_TIMEOUT_SECONDS: int = 45

    # ONLYOFFICE DocumentServer
    ONLYOFFICE_DOCS_URL: str = ""
    ONLYOFFICE_INTERNAL_DOCS_URL: str = ""
    ONLYOFFICE_JWT_SECRET: str = ""
    ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS: int = 300

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    FEISHU_PLATFORM_REGION: str = "feishu_cn"
    FEISHU_OPEN_API_DOMAIN: str = ""
    FEISHU_OAUTH_AUTHORIZE_URL: str = ""
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
    # Platform-managed heartbeat cadence (heartbeat_policy overrides per-agent
    # rows). 2026-06-05 owner decision: 2h — T2 accumulation never kept up with
    # the old 45min digestion rhythm (most ticks idled), and in-conversation
    # memory rides the per-response extraction hook, not this loop.
    HEARTBEAT_DEFAULT_INTERVAL_MINUTES: int = 120
    # Subagent evolution loop (docs/subagent-evolution-loop.md §4.1): an
    # agent-level definition with this many ACTIVE memory entries (and no
    # pending proposal) gets an LLM-drafted definition-improvement proposal.
    SUBAGENT_EVOLUTION_THRESHOLD: int = 8
    # Candidate-driven tenant behavior eval publisher. This closes the
    # production self-evolution promotion gate for ordinary tenants without
    # turning every heartbeat into a benchmark run.
    BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED: bool = True
    BEHAVIOR_EVAL_REPORT_MAX_AGE_HOURS: int = 24

    # Capability mapping enforcement (P1-W2-8)
    # When True, any tool absent from CAPABILITY_MAP is denied at the
    # capability gate (fail-closed). Set False only for explicit local
    # compatibility windows; unmapped tools are always logged + counted.
    STRICT_CAPABILITY_MAPPING: bool = True

    # Workflow admission thresholds (§9 P2) — hard caps for run preflight;
    # admission rejects (never warns) past these. Env-overridable per deploy.
    WORKFLOW_RUNTIME_ENABLED: bool = True
    WORKFLOW_TRIGGER_ENABLED: bool = True
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
    # Production daemon polling interval for startup/time resumes and
    # persistent signal resumes.
    WORKFLOW_DAEMON_INTERVAL_SECONDS: int = 15

    # Consolidation-debt stall thresholds (C9-2, memory-system-spec §6.2.2):
    # a reviewed T2 package or an active explicit overlay entry older than
    # this without being consolidated marks the memory pipeline as stalled.
    MEMORY_DEBT_PENDING_AGE_ALERT_HOURS: float = 48.0
    MEMORY_DEBT_EXPLICIT_AGE_ALERT_HOURS: float = 72.0
    # T2 retention (C9-3, memory-system-spec §3.6/§6.2.3): unreferenced
    # packages older than this archive to memory/.archive/t2/** — moved,
    # never deleted; refs keep resolving through the reference index.
    MEMORY_RETENTION_ARCHIVE_AFTER_DAYS: float = 30.0
    # Resident profile-plane budget (read side, memory-system-spec §4.2):
    # self + profiles + explicit overlay load whole into the prompt; going
    # over this budget is a write-side convergence failure signal (工序 4),
    # alerted — never hard-trimmed.
    MEMORY_RESIDENT_BUDGET_CHARS: float = 12_000.0

    # Coordination backend (Phase 17 wiring)
    # "postgres" — durable PostgreSQL-backed coordination (default). This keeps
    # prompt-facing Team Context / Teammate Mailbox on the same truth source as
    # coordination writers across workers and process restarts.
    # "memory" — explicit dev/test override for in-process Lease/Signal/Checkpoint.
    COORDINATION_BACKEND: str = "postgres"
    OPENVIKING_API_KEY: str = ""

    # Tavily Search API
    TAVILY_API_KEY: str = ""

    # Exa Search API
    EXA_API_KEY: str = ""

    # Optional platform-hosted SearXNG instance for no-key basic web search.
    SEARXNG_URL: str = ""

    # AnySearch Search API. Supports comma- or newline-separated API key pools.
    ANYSEARCH_API_KEYS: str = ""
    ANYSEARCH_DEFAULT_ZONE: str = "intl"
    ANYSEARCH_DEFAULT_CONTENT_TYPES: str = "web"
    ANYSEARCH_TIMEOUT_SECONDS: int = 12

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
