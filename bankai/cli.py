"""Operator CLI:  python -m bankai.cli <command>

  init                   create tables and (ENV=dev) seed the demo bank        [one-shot job in compose]
  replay <trace_id>      print the full span tree of a conversation turn      (debug view, PII-masked)
  traces [N]             list the N most recent traces
  audit-verify           verify the tamper-evident audit hash chain
  usage [agent|user|provider|session]   cost/token report
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select

from .bankcore.audit import AuditLog
from .bankcore.seed import seed
from .config import Settings
from .cost import CostTracker
from .db import app_spans, init_db, make_engine
from .obs.tracing import load_trace, render_trace
from .pii import PIIDetector, Redactor


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    s = Settings.from_env()
    engine = make_engine(s.resolved_database_url())
    cmd, args = argv[0], argv[1:]
    if cmd == "init":
        init_db(engine)
        print("seeded demo data" if s.env == "dev" and seed(engine) else "schema ready")
        return 0
    if cmd == "replay":
        spans = load_trace(engine, args[0])
        if not spans:
            print("trace not found")
            return 1
        print(render_trace(spans))
        return 0
    if cmd == "traces":
        n = int(args[0]) if args else 10
        with engine.connect() as conn:
            rows = conn.execute(select(app_spans.c.trace_id, func.min(app_spans.c.start_ts).label("t0"), func.count().label("spans"))
                                .group_by(app_spans.c.trace_id).order_by(func.min(app_spans.c.start_ts).desc()).limit(n)).all()
        for r in rows:
            print(f"{r.trace_id}  {r.spans:>3} spans")
        return 0
    if cmd == "audit-verify":
        ok, n, msg = AuditLog(engine, Redactor(PIIDetector())).verify_chain()
        print(f"audit chain {'OK' if ok else 'BROKEN'}: {n} rows ({msg})")
        return 0 if ok else 1
    if cmd == "usage":
        for row in CostTracker(engine, s).report(args[0] if args else "agent"):
            print(f"{str(row['key']):<24} calls={row['calls']:<5} tokens={row['tokens']:<8} cost=${row['cost_usd']:.5f}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
