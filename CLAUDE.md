# bankai - project notes for Claude

**Start every session by reading `HANDOFF.md`** (what this is, current state, how to run, lessons learned, next steps).

- Owner writes mostly Roman Urdu + English; reply in Roman Urdu, concise. They are often near their usage limit.
- Run things with the project venv: `.\.venv\Scripts\python.exe` (no `make` on this PC; use `.\run.ps1`).
- Baseline check: `.\.venv\Scripts\python.exe -m pytest tests -q` (expect 188 passed).
- UI/prompts are baked into the Docker image: rebuild AND `--force-recreate` after editing them.
- Security invariants to never weaken: tool layer enforces authz/policy; confirmation text is built by code; PII never reaches the external model
  (every eval case asserts it); tests in `tests/test_injection.py` (`-m canary`) must stay green.

**Standing rule from the owner:** after EVERY meaningful step/fix/deliverable, immediately add an entry to the *Progress log* in `HANDOFF.md` (what changed, why, verification, what is pending) and keep its *Current state* table accurate. Do not wait until the end of the session - the context window can be cut at any time.
