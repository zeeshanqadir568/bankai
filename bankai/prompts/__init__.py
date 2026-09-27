"""Prompt registry. Prompts are files, versioned in git, hashed into every eval report.

`PROMPT_DIR` overrides the packaged directory — the eval harness uses it to prove that a broken
prompt is caught by the regression suite.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..common import sha256_hex

_DEFAULT = Path(__file__).parent


def prompt_dir() -> Path:
    return Path(os.environ.get("PROMPT_DIR") or _DEFAULT)


def load_prompt(name: str) -> str:
    return (prompt_dir() / f"{name}.md").read_text(encoding="utf-8").strip()


def load_agent_prompt(name: str) -> str:
    """Agent prompt + the shared security block."""
    return load_prompt(name) + "\n\n" + load_prompt("guard")


def prompts_version() -> str:
    h = "".join(sha256_hex(p.read_text(encoding="utf-8")) for p in sorted(prompt_dir().glob("*.md")))
    return sha256_hex(h)[:12]
