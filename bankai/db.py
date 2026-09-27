"""Database schema (SQLAlchemy Core) and engine factory.

`core_*` tables simulate the bank's systems of record (in real life: separate databases owned by
other teams, reached only through MCP servers). `app_*` tables are this assistant's own state.
Money is always integer cents; timestamps are epoch seconds (portable, clock-injectable).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

# ---------------------------------------------------------------- core banking (mock systems of record)
core_customers = Table(
    "core_customers", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("email", String(200)),
    Column("phone", String(40)),
)
core_accounts = Table(
    "core_accounts", metadata,
    Column("number", String(20), primary_key=True),
    Column("customer_id", String(40), ForeignKey("core_customers.id"), nullable=False, index=True),
    Column("type", String(20), nullable=False),
    Column("currency", String(3), nullable=False, default="USD"),
    Column("balance_cents", BigInteger, nullable=False),
    Column("status", String(20), nullable=False, default="active"),
)
core_transactions = Table(
    "core_transactions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_number", String(20), ForeignKey("core_accounts.number"), nullable=False),
    Column("ts", Float, nullable=False),
    Column("amount_cents", BigInteger, nullable=False),  # signed: debits negative
    Column("description", Text, nullable=False),  # third-party authored => UNTRUSTED
    Column("counterparty", String(120)),
    Column("kind", String(20), nullable=False),
    Index("ix_tx_acct_ts", "account_number", "ts"),
)
core_payees = Table(
    "core_payees", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", String(40), nullable=False),
    Column("name", String(120), nullable=False),
    Column("account_number", String(20), nullable=False),
    Column("added_ts", Float, nullable=False),
    UniqueConstraint("customer_id", "account_number", name="uq_payee"),
)
core_transfers = Table(
    "core_transfers", metadata,
    Column("id", String(40), primary_key=True),
    Column("initiator_id", String(40), nullable=False, index=True),
    Column("customer_id", String(40), nullable=False),
    Column("from_account", String(20), nullable=False),
    Column("to_account", String(20), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("memo", String(200)),
    Column("status", String(24), nullable=False),  # completed | pending_approval | denied | expired | rejected
    Column("risk_json", Text, nullable=False, default="[]"),
    Column("approval_id", String(40)),
    Column("created_ts", Float, nullable=False),
    Column("updated_ts", Float, nullable=False),
)
core_approvals = Table(
    "core_approvals", metadata,
    Column("id", String(40), primary_key=True),
    Column("transfer_id", String(40), ForeignKey("core_transfers.id"), nullable=False),
    Column("requested_by", String(40), nullable=False),
    Column("session_id", String(40)),
    Column("status", String(16), nullable=False),  # pending | approved | denied | expired
    Column("reasons_json", Text, nullable=False),
    Column("created_ts", Float, nullable=False),
    Column("expires_ts", Float, nullable=False),
    Column("decided_by", String(40)),
    Column("decided_ts", Float),
    Column("note", Text),
)
core_idempotency = Table(
    "core_idempotency", metadata,
    Column("scope", String(80), primary_key=True),  # "<tool>:<user_id>"
    Column("key", String(120), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response_json", Text, nullable=False),
    Column("ts", Float, nullable=False),
)
core_tickets = Table(
    "core_tickets", metadata,
    Column("id", String(40), primary_key=True),
    Column("customer_id", String(40), nullable=False, index=True),
    Column("subject", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("category", String(30), nullable=False),
    Column("priority", String(10), nullable=False),
    Column("status", String(16), nullable=False),  # queued | open | closed
    Column("created_by", String(40), nullable=False),
    Column("created_ts", Float, nullable=False),
    Column("updated_ts", Float, nullable=False),
)
core_kb = Table(
    "core_kb", metadata,
    Column("id", String(40), primary_key=True),
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("source", String(60), nullable=False),  # who authored it => trust level
)
core_audit = Table(
    "core_audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("trace_id", String(40)),
    Column("actor", String(40), nullable=False),
    Column("agent", String(40)),
    Column("tool", String(80), nullable=False),
    Column("decision", String(12), nullable=False),  # allow | deny | error
    Column("reason", Text),
    Column("args_json", Text),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)

# ---------------------------------------------------------------- assistant state
app_sessions = Table(
    "app_sessions", metadata,
    Column("id", String(40), primary_key=True),
    Column("user_id", String(40), nullable=False, index=True),
    Column("created_ts", Float, nullable=False),
    Column("last_seen_ts", Float, nullable=False),
    Column("expires_ts", Float, nullable=False),
    Column("blackboard_json", Text, nullable=False),
    Column("turn_count", Integer, nullable=False, default=0),
)
app_messages = Table(
    "app_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(40), nullable=False, index=True),
    Column("role", String(12), nullable=False),
    Column("content", Text, nullable=False),
    Column("ts", Float, nullable=False),
)
app_summaries = Table(
    "app_summaries", metadata,
    Column("session_id", String(40), primary_key=True),
    Column("summary", Text, nullable=False),
    Column("upto_message_id", Integer, nullable=False),
)
app_user_memory = Table(
    "app_user_memory", metadata,
    Column("user_id", String(40), primary_key=True),
    Column("key", String(60), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_ts", Float, nullable=False),
)
app_usage = Table(
    "app_usage", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("user_id", String(40), nullable=False, index=True),
    Column("session_id", String(40), index=True),
    Column("agent", String(40)),
    Column("provider", String(20)),
    Column("model", String(60)),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("trace_id", String(40)),
)
app_paused_runs = Table(
    "app_paused_runs", metadata,
    Column("approval_id", String(40), primary_key=True),
    Column("session_id", String(40), nullable=False),
    Column("user_id", String(40), nullable=False),
    Column("agent", String(40), nullable=False),
    Column("state_json", Text, nullable=False),
    Column("created_ts", Float, nullable=False),
)
app_notifications = Table(
    "app_notifications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(40), nullable=False, index=True),
    Column("session_id", String(40)),
    Column("body", Text, nullable=False),
    Column("ts", Float, nullable=False),
    Column("delivered", Boolean, nullable=False, default=False),
)
app_spans = Table(
    "app_spans", metadata,
    Column("span_id", String(32), primary_key=True),
    Column("trace_id", String(32), nullable=False, index=True),
    Column("parent_id", String(32)),
    Column("name", String(120), nullable=False),
    Column("service", String(40)),
    Column("start_ts", Float, nullable=False),
    Column("end_ts", Float, nullable=False),
    Column("attrs_json", Text, nullable=False),
    Column("status", String(10), nullable=False),
)
app_egress_log = Table(
    "app_egress_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("trace_id", String(40)),
    Column("provider", String(20), nullable=False),
    Column("model", String(60)),
    Column("redactions", Integer, nullable=False),
    Column("verified_clean", Boolean, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("payload_preview", Text, nullable=False),  # already-redacted text, truncated
    Column("blocked", Boolean, nullable=False, default=False),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB across threads
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url and url not in ("sqlite://", "sqlite:///"):
                cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
