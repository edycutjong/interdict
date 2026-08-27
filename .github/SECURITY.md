# Security policy

## Reporting a vulnerability

Open a [private security advisory](../../security/advisories/new). Please do not file a
public issue for anything exploitable.

## What this project handles

Interdict reads public Treasury sanctions data and writes decisions about a **synthetic**
payment book. It holds no personal data of real payees, no credentials in the tree, and
no production money movement. Credentials live in the environment
(`GEMINI_API_KEY`, `INTERDICT_DSN`) and are never committed.

## Security properties this project actually claims

These are enforced and tested, not aspirational — if you can break one, that is a
vulnerability report worth filing:

| Claim | Enforced by | Test |
|---|---|---|
| the audit ledger cannot be rewritten or deleted | append-only triggers | `test_ledger_is_append_only_*` |
| the audit ledger cannot fork under concurrency | advisory-lock hash chain, `seq` assigned under the same lock | `test_ledger_chain_does_not_fork_under_concurrent_writers` |
| screened money cannot skip states | illegal-transition trigger | `test_illegal_transitions_are_rejected` |
| a re-screen cannot double-freeze a counterparty | `UNIQUE ... NULLS NOT DISTINCT` | `test_double_hold_on_same_pair_is_rejected` |
| a crash cannot silently leave counterparties unscreened | batch checkpointing + coverage-based completion | `test_no_counterparty_is_skipped_across_a_crash` |
| a model verdict cannot move money unchecked | oracle guard at the routing boundary, ≤2 round-trip cap | `test_unsupported_clear_is_quarantined_not_obeyed` |
| a model failure cannot become a silent clear | quarantine on exception | `test_adjudicator_failure_never_becomes_a_silent_clear` |

## Scanning

- **CodeQL** (`security-extended`) on push, PR, and weekly.
- **gitleaks** with `fetch-depth: 0` — scans full history, not just the working tree.
- **pip-audit** against `requirements.txt` in strict mode.
- **Dependabot** for pip, GitHub Actions, and the Docker images in `ops/`.

## Not a compliance product

Interdict drafts OFAC blocking reports. It does not file them, and nothing it produces
is legal advice. Transmission to OFAC is a human step, by design.
