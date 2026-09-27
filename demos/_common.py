"""Tiny presentation helpers shared by the demo scripts (ASCII only: safe on any console)."""
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def h1(t: str) -> None:
    print("\n" + "=" * 100 + f"\n  {t}\n" + "=" * 100)


def h2(t: str) -> None:
    print(f"\n--- {t}")


def say(who: str, msg: str) -> None:
    print(f"\n  [{who}] {msg}")


def bot(res) -> None:
    for line in res.text.splitlines():
        print(f"  [assistant] {line}")
    l = res.log
    bits = [f"routes={l.routes}", f"tools={[t['tool'] + ('!' + t['code'] if t['code'] else '') for t in l.tools]}"]
    if l.guard_blocks:
        bits.append(f"guard_blocks={[b['code'] for b in l.guard_blocks]}")
    if l.flags:
        bits.append(f"flags={l.flags}")
    if l.approvals:
        bits.append(f"approvals={l.approvals}")
    if l.budget_stopped:
        bits.append(f"BUDGET_STOPPED={l.budget_stopped}")
    print("              (" + "  ".join(bits) + f"  trace={res.trace_id[:8]})")


def money(c: int) -> str:
    return f"${c / 100:,.2f}"


def run(coro) -> None:
    asyncio.run(coro)
