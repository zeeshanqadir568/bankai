# One command to run everything:  make up      (then open http://localhost:8080 ; staff console /staff ; Jaeger :16686)
# Windows without make: use  .\run.ps1 <target>
PY      ?= python
COMPOSE ?= docker compose
API_PORT ?= 8080

.PHONY: up up-env down logs ps secrets test canary eval eval-regression eval-real demo demo-attack demo-authz demo-runaway demo-hitl demo-multi demo-pii smoke replay clean

secrets:                 ## generate random local secrets (never overwrites existing ones)
	$(PY) scripts/gen_secrets.py

up: secrets              ## build + start the whole stack (postgres, redis, jaeger, 4 MCP servers, api, worker)
	API_PORT=$(API_PORT) $(COMPOSE) up -d --build
	@echo "UI: http://localhost:$(API_PORT)   staff: /staff   traces: http://localhost:16686"

up-env: secrets          ## same, but secrets are passed as env-sourced Docker secrets (hosts that can't bind-mount ./secrets)
	JWT_SIGNING_KEY="$$(cat secrets/jwt_signing_key)" MCP_TOKEN_KEY="$$(cat secrets/mcp_token_key)" DB_PASSWORD="$$(cat secrets/db_password)" ANTHROPIC_API_KEY=" " \
	API_PORT=$(API_PORT) $(COMPOSE) -f docker-compose.yml -f docker-compose.env-secrets.yml up -d --build

down:                    ## stop and remove containers + volumes
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

smoke:                   ## end-to-end test against the running stack (incl. degradation + self-heal)
	$(PY) scripts/smoke.py --base http://localhost:$(API_PORT) --jaeger http://localhost:16686 \
	  --stop-cmd "$(COMPOSE) stop payments-mcp" --start-cmd "$(COMPOSE) start payments-mcp"

test:                    ## unit + integration + eval-meta tests (no Docker needed)
	$(PY) -m pytest tests -q

canary:                  ## prompt-injection canaries only - run on EVERY change
	$(PY) -m pytest tests -m canary -q

eval:                    ## golden conversations -> evals/reports/report.{md,json}; exit 1 on regression
	$(PY) -m evals.runner

eval-regression:         ## prove the suite catches regressions: break a prompt, expect red
	$(PY) -m evals.break_prompt --break transfers

eval-real:               ## same evals against a real in-bank model served by Ollama
	$(PY) -m evals.runner --local ollama --model $${MODEL:-qwen2.5:14b-instruct} --no-report

demo: demo-attack demo-authz demo-runaway demo-hitl demo-multi demo-pii
demo-attack:  ; $(PY) -m demos.attack
demo-authz:   ; $(PY) -m demos.authz
demo-runaway: ; $(PY) -m demos.runaway
demo-hitl:    ; $(PY) -m demos.hitl
demo-multi:   ; $(PY) -m demos.why_multi_agent
demo-pii:     ; $(PY) -m demos.pii_egress

replay:                  ## make replay TRACE=<32-hex trace id>
	$(COMPOSE) exec api python -m bankai.cli replay $(TRACE)

clean:
	$(PY) -c "import shutil,glob;[shutil.rmtree(p,ignore_errors=True) for p in glob.glob('**/__pycache__',recursive=True)+['.pytest_cache','evals/reports']]"
