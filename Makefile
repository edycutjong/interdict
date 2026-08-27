SHELL := /bin/bash
PY    := .venv/bin/python
PIP   := .venv/bin/pip
DSN   ?= postgresql://interdict:interdict@localhost:5433/interdict

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.venv:
	python3 -m venv .venv && $(PIP) install -q --upgrade pip

.PHONY: install
install: .venv  ## Install Python dependencies
	$(PIP) install -q -r requirements.txt

.PHONY: install-dev
install-dev: .venv  ## Install dependencies plus the lint/type/security toolchain
	$(PIP) install -q -r requirements-dev.txt

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

.PHONY: lint
lint:  ## Ruff lint
	@echo "ruff check..."
	$(PY) -m ruff check .

.PHONY: lint-fix
lint-fix:  ## Ruff autofix + format
	$(PY) -m ruff check --fix . && $(PY) -m ruff format .

.PHONY: typecheck
typecheck:  ## mypy
	@echo "mypy..."
	$(PY) -m mypy interdict --ignore-missing-imports

.PHONY: test-coverage
test-coverage:  ## Tests with coverage
	$(PY) -m pytest --cov --cov-report=term-missing --cov-report=xml

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

.PHONY: audit
# `|| true` on both commands here used to mean the security leg of `make ci` could never
# fail: a real CVE or a real leaked secret printed and exited 0. Only a MISSING gitleaks is
# tolerated now, and it says so; a gitleaks that ran and found something fails the target.
audit:  ## Dependency CVEs + secrets in history
	@echo "=== pip-audit (dependency CVEs) ==="
	$(PY) -m pip_audit
	@echo "=== gitleaks (secrets in history) ==="
	@if command -v gitleaks >/dev/null; then gitleaks detect --no-banner --redact; \
	 else echo "gitleaks not installed locally; CI runs it (.github/workflows/gitleaks.yml)"; fi

.PHONY: ci
ci: lint typecheck test-coverage audit  ## Everything CI runs

.PHONY: up
up:  ## Start Postgres + Elasticsearch + yente
	docker compose -f ops/docker-compose.yml up -d
	@echo "waiting for elasticsearch..."
	@until curl -sf http://localhost:9200/_cluster/health >/dev/null; do sleep 2; done
	@echo "stack up. run 'make oracle-index' once to index the OFAC dataset."

.PHONY: down
down:  ## Stop the local stack
	docker compose -f ops/docker-compose.yml down

.PHONY: oracle-index
oracle-index:  ## Index us_ofac_sdn into yente (run once after `make up`)
	docker compose -f ops/docker-compose.yml exec -T yente yente reindex
	@curl -sf http://localhost:8000/readyz && echo " oracle ready"

.PHONY: schema
schema:  ## Apply the database schema
	docker compose -f ops/docker-compose.yml exec -T postgres \
	  psql -U interdict -d interdict -v ON_ERROR_STOP=1 < interdict/schema.sql

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

.PHONY: fetch-sdn
fetch-sdn:  ## Fetch the current OFAC SDN publication (27MB, follows the S3 redirect)
	curl -sSL --fail -o data/SDN.XML \
	  "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
	@bash scripts/check_sdn.sh data/SDN.XML

.PHONY: verify-book
verify-book:  ## Check the sealed sentinel book against its seal, and against the publication
	$(PY) scripts/verify_book.py --sdn data/SDN.XML

.PHONY: archive-status
archive-status:  ## Fail if the OFAC archiver has not captured anything recently
	$(PY) scripts/archive_status.py

# ---------------------------------------------------------------------------
# The dev loop
# ---------------------------------------------------------------------------

.PHONY: challenge
challenge:  ## Screen any name live: make challenge NAME="Ibrahim Al Rashid"
	@$(PY) scripts/challenge.py --name "$(NAME)" $(if $(DOB),--dob "$(DOB)",)

.PHONY: agreement
agreement:  ## Grade the book against yente, VERBATIM (near-tautological -- see below)
	$(PY) scripts/agreement.py --json-out data/agreement-verbatim.json

.PHONY: challenge-set
challenge-set:  ## Grade the book against yente on PERTURBED names -- the honest number
	$(PY) scripts/agreement.py --perturb --json-out data/g1-perturbed.json

.PHONY: bench
# Promised by name in README.md, DEMO.md, JUDGE.md, the landing page, the judge page and
# the Devpost testing instructions -- and missing from this file until 2026-08-27, so a
# judge following the README hit "No rule to make target 'bench'". The script was always
# there; only the entry point was not.
bench:  ## p50/p95/p99 over the deterministic screening plane, 400 counterparties
	$(PY) scripts/bench.py --runs 3 --json-out data/bench.json

.PHONY: verify-ledger
verify-ledger:  ## Verify the ledger hash chain end to end
	@$(PY) scripts/verify_ledger.py

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test:  ## Run the test suite (WARNING: destroys the demo book -- see demo-state)
	@echo "note: the test fixtures TRUNCATE counterparties, disbursements, holds and"
	@echo "      adjudications. If you were holding a loaded book for a demo or a"
	@echo "      screenshot, run 'make demo-state' afterwards to rebuild it."
	$(PY) -m pytest tests/ -q

.PHONY: demo-state
demo-state:  ## Rebuild the labelled demo book and re-screen it (restores the console)
	@# The test fixtures truncate the payment book, so a full 'make test' leaves the
	@# evidence console showing test rows instead of the demo. This puts it back. The
	@# book is deterministic -- same strata, same amounts, same $1,181,434.51 held.
	$(PY) scripts/load_book.py --truncate --sentinels 30 --variants 30 --lookalikes 30
	$(PY) scripts/run_rescreen.py --batch-size 20 --progress

.PHONY: demo-ids
demo-ids:  ## Print the live Firestore document ids the demo video's cloud shots need
	@# These move on every re-screen, so never write them down -- read them before filming.
	$(PY) scripts/demo_ids.py

.PHONY: test-v
test-v:  ## Run the test suite, verbose
	$(PY) -m pytest tests/ -v

.PHONY: reproduce
reproduce: install up  ## Clean-machine path: stack, index, schema, tests, the real number
	$(MAKE) oracle-index
	$(MAKE) schema
	$(MAKE) test
	$(MAKE) challenge-set
	@echo
	@echo "Reproduced. The number that matters is top-1 in PERTURBED mode:"
	@echo "screening names that are NOT on the list character-for-character."

# ─── the published surfaces in site/ ────────────────────────────────────────────
# The product is Python and none of this ships; it exists so that /judge -- the one
# page written for a single reader, and the link that goes in the submission entry --
# cannot quietly break on submission day. Playwright serves site/ with python's own
# http.server, which resolves /judge/ the same way GitHub Pages does.

.PHONY: e2e-install
e2e-install:  ## Install the browser toolchain for the site/ gates (once)
	npm install
	npx playwright install --with-deps chromium

.PHONY: e2e
e2e:  ## Playwright: /judge reachable unauthenticated, no dead links, no overflow
	npx playwright test

.PHONY: e2e-ui
e2e-ui:  ## Playwright, interactive
	npx playwright test --ui

.PHONY: lighthouse
lighthouse:  ## Lighthouse CI over the landing page, /judge and the pitch deck
	npx lhci autorun

.PHONY: security-scan
security-scan:  ## Everything that must be clean before a private -> public flip
	@echo "=== pip-audit (dependency CVEs) ==="
	$(PY) -m pip_audit --requirement requirements.txt --strict
	@echo "=== gitleaks (secrets over FULL history) ==="
	@command -v gitleaks >/dev/null 2>&1 \
	  && gitleaks detect --source . --redact --log-opts="--all" \
	  || echo "gitleaks NOT INSTALLED -- history was not scanned. brew install gitleaks"
	@echo "=== kitchen leak scan (private folders and home paths must never appear) ==="
	@# Each alternative wraps one letter in a character class so this recipe does not
	@# match itself -- the scanner was its own first false positive. The class still
	@# matches the real string everywhere else, including elsewhere in this Makefile.
	@! git grep -nIE '/Us[e]rs/|~/Hack[a]thon|_id[e]as/|_sp[e]cs/|proj[e]ct\.json' -- \
	     ':!*.lock' ':!package-lock.json' \
	  || (echo "LEAK: a kitchen path is present in a tracked file" && exit 1)
	@echo "clean."
