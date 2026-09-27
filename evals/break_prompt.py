"""Prove the regression suite catches regressions: deliberately break ONE prompt, run the evals, show what fails.

    python -m evals.break_prompt                 # removes transfers_agent from the coordinator's routing table
    python -m evals.break_prompt --break escalate

Exit 0 means "the suite noticed" (it FAILED on the broken prompt, as it should). Exit 1 means the regression slipped through.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from bankai.prompts import _DEFAULT
from evals import runner

BREAKS = {
    "transfers": ("coordinator.md", "- transfers_agent:", "remove the transfers_agent routing line (someone 'cleaned up' the routing table)"),
    "escalate": ("coordinator.md", "- escalate:", "remove the escalate line (fraud / 'human' no longer hands off)"),
    "faq": ("coordinator.md", "- faq_agent:", "remove the faq_agent routing line"),
}


def break_prompt(kind: str) -> Path:
    fname, prefix, _ = BREAKS[kind]
    tmp = Path(tempfile.mkdtemp(prefix="prompts_broken_"))
    for p in _DEFAULT.glob("*.md"):
        shutil.copy(p, tmp / p.name)
    lines = (tmp / fname).read_text(encoding="utf-8").splitlines()
    (tmp / fname).write_text("\n".join(l for l in lines if not l.startswith(prefix)) + "\n", encoding="utf-8")
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--break", dest="kind", choices=BREAKS, default="transfers")
    ap.add_argument("--out", default=str(runner.HERE / "reports" / "regression_demo"))
    a = ap.parse_args()
    broken = break_prompt(a.kind)
    fname = BREAKS[a.kind][0]
    print(f"Breaking prompt: {BREAKS[a.kind][2]}\n")
    diff = difflib.unified_diff((_DEFAULT / fname).read_text(encoding="utf-8").splitlines(), (broken / fname).read_text(encoding="utf-8").splitlines(),
                                f"a/{fname}", f"b/{fname}", lineterm="", n=0)
    print("\n".join(diff), "\n")
    os.environ["PROMPT_DIR"] = str(broken)
    ns = argparse.Namespace(cases=str(runner.HERE / "golden.yaml"), only="", threshold=1.0, local="mock", ollama_url="", model="")
    summary, results = asyncio.run(runner.run_suite(ns))
    runner.console(results, summary)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(runner.render_markdown(summary, results), encoding="utf-8")
    failed = [r.id for r in results if not r.known_fail and not r.passed]
    print(f"\n{len(failed)} case(s) regressed with prompts version {summary['prompts_version']}: {failed}")
    if summary["ok"]:
        print("!! the suite did NOT catch the regression")
        return 1
    print("Regression detected: the suite is red, as it should be. Report: " + runner._rel(out / "report.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
