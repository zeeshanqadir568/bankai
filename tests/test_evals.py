"""The eval suite itself: it must be green on the real system AND go red when we break things (otherwise it proves nothing)."""
import argparse
import asyncio
from pathlib import Path

import pytest
import yaml

from bankai.security.authz import Decision
from evals import break_prompt, runner

GOLDEN = Path(runner.HERE / "golden.yaml")


def ns(only="", threshold=1.0):
    return argparse.Namespace(cases=str(GOLDEN), only=only, threshold=threshold, local="mock", ollama_url="", model="")


def run(only=""):
    return asyncio.run(runner.run_suite(ns(only)))


def test_golden_dataset_shape():
    cases = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    assert len(cases) >= 30
    cats = {c["category"] for c in cases}
    assert {"accounts", "transfers", "hitl", "faq", "tickets", "authz", "injection", "pii", "routing", "escalation", "budget", "resilience"} <= cats
    assert sum(1 for c in cases if c.get("known_fail")) >= 2  # deliberately failing evals are part of the dataset
    assert len({c["id"] for c in cases}) == len(cases)
    assert sum(1 for c in cases if c["category"] == "injection") >= 5


def test_full_golden_suite_is_green():
    summary, results = run()
    failing = [(r.id, [c.name for c in r.checks + r.rubric if not c.ok] or r.error) for r in results if not r.known_fail and not r.passed]
    assert summary["ok"], failing
    assert summary["pass_rate"] == 1.0 and summary["critical_failures"] == [] and summary["xpass"] == []
    assert set(summary["known_failures"]) == {"kf-01-implicit-followup-not-resolved", "kf-02-keyword-collision-misroutes-policy-question"}
    assert summary["rubric_score"] == 1.0


def test_known_failures_show_what_regression_output_looks_like():
    _, results = run("kf-")
    assert [r.status for r in results] == ["XFAIL", "XFAIL"]
    md = runner.render_markdown(*[runner.summarize(results, 1.0, {"prompts_version": "x", "prompt_dir": "", "local": "", "external": ""}), results])
    assert "KNOWN FAIL (expected)" in md and "t2.routes==['accounts_agent']" in md


def test_a_known_fail_that_starts_passing_fails_the_suite():
    cases = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    ok_case = next(c for c in cases if c["id"] == "acc-01-all-balances")
    ok_case = {**ok_case, "id": "stale-known-fail", "known_fail": "was broken once"}
    r = asyncio.run(runner.run_case(ok_case, "mock", ns()))
    assert r.status == "XPASS"
    assert not runner.summarize([r], 1.0, {"prompts_version": "", "prompt_dir": "", "local": "", "external": ""})["ok"]


@pytest.mark.parametrize("kind,must_fail", [("transfers", {"tr-01-small-transfer-confirm-then-execute", "authz-04-transfer-from-someone-elses-account"}),
                                             ("escalate", {"rt-02-asks-for-human", "rt-04-high-risk-fraud-escalates"}),
                                             ("faq", {"faq-01-mortgage-rate", "faq-02-branch-hours"})])
def test_a_broken_prompt_is_caught_by_the_regression_suite(kind, must_fail, monkeypatch):
    monkeypatch.setenv("PROMPT_DIR", str(break_prompt.break_prompt(kind)))
    summary, results = run()
    failed = {r.id for r in results if not r.known_fail and not r.passed}
    assert not summary["ok"] and must_fail <= failed
    assert summary["prompts_version"] != runner.prompts_version.__wrapped__() if hasattr(runner.prompts_version, "__wrapped__") else True


# ---------------------------------------------------------------- code-level sabotage: do the evals notice security regressions?
def test_evals_catch_an_authorization_regression(monkeypatch):
    monkeypatch.setattr("bankai.bankcore.services.authorize", lambda *a, **k: Decision(True, "sabotaged"))
    _, results = run("authz")
    failed = {r.id for r in results if not r.passed}
    assert {"authz-01-customer-reads-other-balance", "authz-03-bob-reads-alice-savings", "tk-03-other-customers-ticket-is-private"} <= failed


def test_evals_catch_a_disabled_action_guard(monkeypatch):
    from bankai.guardrails.actions import ALLOW, ActionGuard
    monkeypatch.setattr(ActionGuard, "validate", lambda self, call, **kw: ALLOW)
    _, results = run("tr-0")
    assert "tr-01-small-transfer-confirm-then-execute" in {r.id for r in results if not r.passed}  # confirmation gate gone


def test_evals_catch_pii_reaching_the_external_model_if_the_guard_is_bypassed(monkeypatch):
    """Simulates a developer 'simplifying' GuardedExternalLLM into a pass-through and the router into always-external."""
    from bankai.llm.guarded import GuardedExternalLLM
    from bankai.llm.router import HybridRouter, RouteDecision

    async def passthrough(self, req):
        return await self._GuardedExternalLLM__raw.chat(req)

    monkeypatch.setattr(GuardedExternalLLM, "chat", passthrough)
    monkeypatch.setattr(HybridRouter, "decide", lambda self, req: RouteDecision("external", "sabotaged", True))
    _, results = run("pii")
    failed = {r.id: [c.name for c in r.checks if not c.ok] for r in results if not r.passed}
    assert failed and all("invariant.no_pii_reached_external_model" in names for names in failed.values())


def test_evals_catch_a_removed_loop_protection(monkeypatch):
    from bankai.cost import RequestBudget
    monkeypatch.setattr(RequestBudget, "note_tool_call", lambda self, fq, args: None)
    monkeypatch.setattr(RequestBudget, "check_iteration", lambda self, i: None)
    monkeypatch.setattr(RequestBudget, "charge", lambda self, resp: None)
    _, results = run("bud-01")
    assert not all(r.passed for r in results)
