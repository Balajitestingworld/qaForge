# QA Forge Framework Context

This file captures the essential architecture, workflows, commands, and conventions of the QA Forge project so future Copilot chats can answer questions with project-specific context.

## 1. Project purpose

QA Forge is an enterprise UI automation framework for regulated environments, especially banking and finance. It combines:

- Playwright-based browser automation
- pytest-based test execution
- evidence-pack generation and auditing
- no-code Studio for non-programmers
- secret management and environment configuration
- Kafka / DB / event verification
- JIRA dry-run and publishing support

## 2. Core project structure

- `qaforge/` - framework source code
  - `core/` - browser/session/action abstractions
  - `auth/` - Okta, TOTP, session utilities
  - `data/` - models, generators, factories, context, ledger
  - `compliance/` - evidence, logging, masking, HTML reports
  - `studio/` - no-code web UI and admin flows
  - `nocode/` - no-code spec execution and runner
  - `config/` - settings and secrets handling
  - `verifiers/` - DB/event verification
- `apps/` - application-specific page objects and flows
  - `retail_banking/`
  - `backoffice/`
- `demo_app/` - local sample banking app used for proving the framework end-to-end
- `tests/` - pytest tests, framework tests, retailer tests, no-code specs
- `config/environments/` - environment config files
- `config/mappings/` - UI-to-event field mappings
- `testdata/` - YAML/JSON data-driven inputs
- `studio_data/` - local Studio users and audit logs
- `evidence/` - generated evidence reports for test runs

## 3. Runtime and environment requirements

- Requires Python 3.10+
- Project venv is typically `.venv`
- Playwright browsers must be installed with:
  - `.venv/bin/python -m playwright install chromium webkit`
- The startup script is `start_studio.sh`
- QA Forge Studio runs on `http://127.0.0.1:8700`

## 4. Common commands

### Start Studio

```bash
cd "/Users/balajimakam/Downloads/qaforge 2"
./start_studio.sh
```

If the venv was created with an older Python version, the script recreates it automatically.

### Run a smoke browser test

```bash
cd "/Users/balajimakam/Downloads/qaforge 2"
.venv/bin/python -m pytest tests/retail/test_payments_e2e.py -q -m smoke
```

### Run a no-code spec test

```bash
cd "/Users/balajimakam/Downloads/qaforge 2"
.venv/bin/python -m pytest tests/nocode/test_nocode_specs.py -k neft_maker_checker -q
```

### Start demo app locally

```bash
cd "/Users/balajimakam/Downloads/qaforge 2"
.venv/bin/python -m demo_app.server --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

### Install Playwright browsers

```bash
cd "/Users/balajimakam/Downloads/qaforge 2"
.venv/bin/python -m playwright install chromium webkit
```

## 5. Important login credentials

### QA Forge Studio

- Username: `Admin`
- Password: `Admin@12345`

### Demo bank app

- Maker: `maker.demo@example.test` / `Maker#Pass1`
- Checker: `checker.demo@example.test` / `Checker#Pass1`

## 6. Testing and automation model

The framework encourages a layered architecture:

- `tests` call flows and business scenarios
- flows call page objects
- page objects call audited actions
- audited actions call Playwright locators
- every step is logged and evidence is captured

This is the key pattern:

- `tests` are not supposed to directly call Playwright
- `page object` methods should not directly call `locator.click()`
- all browser actions are expected to flow through `qaforge.core.actions.Actions`

## 7. Audited action layer

The core browser wrapper is in:

- `qaforge/core/actions.py`

This class provides wrapped methods such as:

- `goto`
- `click`
- `fill`
- `select`
- `check`
- `press`
- `hover`
- `double_click`
- `right_click`
- `drag_and_drop`
- `clear`
- `focus`
- `blur`
- `read_text`
- `read_value`
- `expect_visible`
- `expect_hidden`
- `expect_text`
- `expect_value`
- `expect_url`
- `accept_dialog`
- `dismiss_dialog`
- `evaluate`
- `wait_for_load_state`

Each action is designed to:

- log a step
- capture a screenshot when appropriate
- mask sensitive values
- report fallback usage when locator chains are used
- surface failures with evidence capture

## 8. Fallback locator architecture

The project supports locator chains and fallback locators via:

- `qaforge/core/locator_chain.py`

The workflow is:

- primary locator is tried first
- if it fails, a fallback is used only when valid
- fallback usage is logged loudly and recorded in evidence
- fallback policy can be `warn` or `fail`

## 9. Browser session model

The session and app abstraction live in:

- `qaforge/core/session.py`
- `qaforge/core/browser.py`

A session handles:

- browser start/stop
- app login / session reuse
- Playwright context creation
- evidence pack wiring
- app-specific state and role handling

## 10. Studio / no-code model

The no-code web UI is in:

- `qaforge/studio/`

Studio handles:

- test authoring
- environment management
- user roles and audit trail
- generated test specs and page objects
- run orchestration

Studio users are stored in `studio_data/users.json` and audit records are in `studio_data/audit.log.jsonl`.

## 11. Evidence and compliance model

Core evidence and masking logic lives in:

- `qaforge/compliance/evidence.py`
- `qaforge/compliance/logger.py`
- `qaforge/compliance/masker.py`

Every run automatically produces:

- HTML report
- masked logs
- screenshots
- trace / browser artifacts when applicable
- JIRA dry-run plan

## 12. Verification model

There are framework verifiers for:

- event verification (`qaforge/verifiers/events.py`)
- DB verification (`qaforge/verifiers/db.py`)
- field and data comparison (`qaforge/verifiers/compare.py`)

These are used to validate actual application output against expected app behavior.

## 13. Demo app notes

The demo app is intentionally small and used to prove the framework end-to-end.

- `demo_app/server.py`
- runs at `http://127.0.0.1:8765`
- contains sample retail and back-office flows
- includes a demo identity provider and event backbone simulation

## 14. Known gotchas

- Python 3.9 fails because the project uses 3.10+ syntax (e.g. union types)
- Playwright browser engines must be installed
- Some local examples depend on the demo app being running
- The Studio UI requires the app to be launched with the right Python venv
- Studio users are local persisted state in `studio_data/`

## 15. Recommended workflow for future questions

When answering coding questions, assume the project architecture is:

1. Browser actions go through `Actions`
2. Page objects wrap UI flows
3. Tests reflect business journeys
4. Evidence and masking are part of every execution
5. The framework enforces a governed, auditable automation pattern rather than raw Playwright scripting

## 16. Good examples in the repo

- `apps/retail_banking/flows/payments.py`
- `apps/retail_banking/pages/transfer_pages.py`
- `apps/backoffice/pages/payments_queue_page.py`
- `qaforge/core/actions.py`
- `qaforge/studio/security.py`
- `tests/retail/test_payments_e2e.py`
- `tests/nocode/test_nocode_specs.py`

These are the best references for understanding the project’s real usage patterns.

## 17. Short answer for future Copilot chats

This project is an audited Playwright + pytest framework with a no-code Studio, environment config, secret management, event verification, JIRA dry-run publishing, and evidence capture. It prefers wrapper-based browser actions over raw Playwright calls and is meant to produce tamper-evident, compliance-friendly automation rather than ad hoc scripts.
