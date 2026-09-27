"""Generate local secret files (random, git-ignored). Compose mounts them as Docker secrets under /run/secrets/.
   python scripts/gen_secrets.py            # creates missing files only; never overwrites existing secrets"""
import pathlib
import secrets

ROOT = pathlib.Path(__file__).resolve().parent.parent / "secrets"
ROOT.mkdir(exist_ok=True)
for name in ("jwt_signing_key", "mcp_token_key", "db_password"):
    p = ROOT / name
    if not p.exists():
        p.write_text(secrets.token_urlsafe(48))
        print(f"created secrets/{name}")
key = ROOT / "anthropic_api_key"
if not key.exists():
    key.write_text("")  # empty => external LLM uses the mock; put a real key here to use the Anthropic API (still behind the PII guard)
    print("created secrets/anthropic_api_key (empty)")
