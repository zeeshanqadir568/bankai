"""Configuration: environment variables for settings, a secrets store for secrets.

Rule: nothing sensitive has a default outside ENV=dev. In any other environment a missing
secret is a startup failure, not a silent fallback to a well-known key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

DEV_ONLY_SECRETS = {
    "jwt_signing_key": "dev-only-jwt-key-not-for-production-0123456789",
    "mcp_token_key": "dev-only-mcp-token-key-not-for-production-0123456789",
    "anthropic_api_key": "",
}


class SecretStore:
    """Resolution order: Docker/K8s secret file -> `<NAME>_FILE` env -> env var (dev only) -> dev default (dev only)."""

    def __init__(self, env: str, secrets_dir: str = "/run/secrets", overrides: dict[str, str] | None = None):
        self.env = env
        self.secrets_dir = Path(secrets_dir)
        self.overrides = overrides or {}

    def get(self, name: str, *, required: bool = True) -> str:
        if name in self.overrides:
            return self.overrides[name]
        file_env = os.environ.get(f"{name.upper()}_FILE")
        candidates = [Path(file_env)] if file_env else []
        candidates.append(self.secrets_dir / name)
        for p in candidates:
            if p.is_file():
                return p.read_text().strip()
        if self.env == "dev":
            val = os.environ.get(name.upper())
            if val is not None:
                return val
            if name in DEV_ONLY_SECRETS:
                return DEV_ONLY_SECRETS[name]
        if required:
            raise RuntimeError(f"secret '{name}' is not configured (ENV={self.env}); refusing to start with a default")
        return ""


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    service_name: str = "bankai-api"
    database_url: str = "sqlite:///./data/bank.db"
    redis_url: str = ""  # empty -> in-process queue (tests, single-process demos)

    jwt_issuer: str = "https://idp.bank.internal"
    jwt_audience: str = "bank-ai-assistant"
    jwt_algorithm: str = "HS256"

    # MCP endpoints. Empty -> servers run in-process (tests/demos); set -> streamable-HTTP.
    mcp_accounts_url: str = ""
    mcp_payments_url: str = ""
    mcp_crm_url: str = ""
    mcp_knowledge_url: str = ""

    # LLMs
    local_llm_provider: str = "mock"  # mock | ollama
    ollama_url: str = "http://localhost:11434"
    local_model: str = "qwen2.5:14b-instruct"
    external_llm_provider: str = "mock"  # mock | anthropic
    external_model: str = "claude-sonnet-5"
    external_llm_enabled: bool = True  # kill switch: false => everything stays in-bank

    # Loop / cost protection
    max_iterations: int = 6
    max_request_tokens: int = 24_000
    max_request_cost_usd: float = 0.25
    max_delegation_depth: int = 3
    max_identical_calls: int = 3
    max_llm_calls_per_request: int = 24
    user_daily_cost_usd: float = 5.00
    session_ttl_seconds: int = 1800
    memory_window_messages: int = 8

    # Transfer policy (enforced in payments-mcp, not in the agent)
    approval_threshold_cents: int = 100_000  # $1,000
    hard_limit_cents: int = 5_000_000  # $50,000 per transfer
    daily_limit_cents: int = 10_000_000
    approval_timeout_seconds: int = 900

    # Resilience
    llm_timeout_seconds: float = 30.0
    mcp_timeout_seconds: float = 10.0
    retry_attempts: int = 3
    retry_base_delay: float = 0.2
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 20.0

    otlp_endpoint: str = ""
    secrets_dir: str = "/run/secrets"
    secret_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = _env("ENV", "dev")
        s = cls(
            env=env,
            service_name=_env("SERVICE_NAME", "bankai-api"),
            database_url=_env("DATABASE_URL", cls.database_url),
            redis_url=_env("REDIS_URL", ""),
            mcp_accounts_url=_env("MCP_ACCOUNTS_URL", ""),
            mcp_payments_url=_env("MCP_PAYMENTS_URL", ""),
            mcp_crm_url=_env("MCP_CRM_URL", ""),
            mcp_knowledge_url=_env("MCP_KNOWLEDGE_URL", ""),
            local_llm_provider=_env("LOCAL_LLM_PROVIDER", "mock"),
            ollama_url=_env("OLLAMA_URL", cls.ollama_url),
            local_model=_env("LOCAL_MODEL", cls.local_model),
            external_llm_provider=_env("EXTERNAL_LLM_PROVIDER", "mock"),
            external_model=_env("EXTERNAL_MODEL", cls.external_model),
            external_llm_enabled=_env("EXTERNAL_LLM_ENABLED", "true").lower() == "true",
            max_request_tokens=int(_env("MAX_REQUEST_TOKENS", "24000")),
            max_request_cost_usd=float(_env("MAX_REQUEST_COST_USD", "0.25")),
            user_daily_cost_usd=float(_env("USER_DAILY_COST_USD", "5.0")),
            session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", "1800")),
            approval_threshold_cents=int(_env("APPROVAL_THRESHOLD_CENTS", "100000")),
            approval_timeout_seconds=int(_env("APPROVAL_TIMEOUT_SECONDS", "900")),
            llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "30")),
            max_iterations=int(_env("MAX_ITERATIONS", "6")),
            otlp_endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            secrets_dir=_env("SECRETS_DIR", "/run/secrets"),
        )
        return replace(s, **overrides) if overrides else s

    def secrets(self) -> SecretStore:
        return SecretStore(self.env, self.secrets_dir, dict(self.secret_overrides))

    def resolved_database_url(self) -> str:
        """Postgres passwords never live in DATABASE_URL: the URL carries no password and the `db_password` secret is injected here."""
        if not self.database_url.startswith(("postgresql", "postgres")):
            return self.database_url
        from sqlalchemy.engine import make_url
        url = make_url(self.database_url.replace("postgres://", "postgresql://", 1))
        if url.password is None:
            pw = self.secrets().get("db_password", required=False)
            if pw:
                url = url.set(password=pw)
        return url.render_as_string(hide_password=False)
