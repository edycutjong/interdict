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
	curl -sSL -o data/SDN.XML \
	  "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
	@shasum -a 256 data/SDN.XML

.PHONY: verify-book
verify-book:  ## Re-derive the sealed sentinel book and check its hash
	$(PY) scripts/seed_sentinels.py --sdn data/SDN.XML --out /tmp/sentinels-check.csv
	@shasum -a 256 /tmp/sentinels-check.csv data/sentinels.csv

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

.PHONY: verify-ledger
verify-ledger:  ## Verify the ledger hash chain end to end
	@$(PY) -c "from interdict.db import connect, verify_chain; \
	  c=connect().__enter__(); ok,n=verify_chain(c); \
	  print(f'ledger: {n} entries, chain {\"INTACT\" if ok else \"FORKED\"}'); \
	  raise SystemExit(0 if ok else 1)"

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test:  ## Run the test suite
	$(PY) -m pytest tests/ -q

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
