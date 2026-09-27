# Windows equivalent of the Makefile:  .\run.ps1 up | down | test | canary | eval | eval-regression | demo | smoke
param([Parameter(Position=0)][string]$Target = "test", [string]$TraceId = "")
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$env:PYTHONPATH = "."
switch ($Target) {
  "secrets" { & $py scripts/gen_secrets.py }
  "up"      { & $py scripts/gen_secrets.py; docker compose up -d --build; "UI: http://localhost:$($env:API_PORT ?? 8080)" }
  "up-env"  { & $py scripts/gen_secrets.py
              $env:JWT_SIGNING_KEY = (Get-Content secrets/jwt_signing_key -Raw); $env:MCP_TOKEN_KEY = (Get-Content secrets/mcp_token_key -Raw)
              $env:DB_PASSWORD = (Get-Content secrets/db_password -Raw); $env:ANTHROPIC_API_KEY = " "
              docker compose -f docker-compose.yml -f docker-compose.env-secrets.yml up -d --build }
  "down"    { docker compose down -v }
  "test"    { & $py -m pytest tests -q }
  "canary"  { & $py -m pytest tests -m canary -q }
  "eval"    { & $py -m evals.runner }
  "eval-regression" { & $py -m evals.break_prompt --break transfers }
  "smoke"   { & $py scripts/smoke.py --base http://localhost:8080 --jaeger http://localhost:16686 }
  "demo"    { foreach ($d in "attack","authz","runaway","hitl","why_multi_agent","pii_egress") { & $py -m "demos.$d" } }
  "replay"  { docker compose exec api python -m bankai.cli replay $TraceId }
  default   { "unknown target '$Target'" }
}
