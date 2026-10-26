# QA Forge

Enterprise UI automation framework for regulated environments (banking first), built on
**Playwright + pytest, pure Python, no AI components**, with **QA Forge Studio**: a web UI that
lets testers who don't write code build, record, run and review tests.

It gives every project the same accelerator: audited actions, tamper-evident evidence, Okta SSO,
synthetic test data that flows from the UI into Kafka / DB / other-app verification, JIRA
publishing with de-duplicated defects, and auditable fallback locators.

---

## 1. Quick start

### For testers (no programming)

1. Double-click **`Start QA Forge Studio.bat`** (macOS/Linux: `./start_studio.sh`).
   The first start installs everything into a private `.venv` folder (a few minutes, once).
2. Your browser opens at http://127.0.0.1:8700. The very first time, create the administrator.
3. Keep the black window open while you work; closing it stops Studio.

Needs Python 3.10+ on the computer. Behind a corporate proxy, IT sets `PIP_INDEX_URL`,
`HTTPS_PROXY` and `PLAYWRIGHT_DOWNLOAD_HOST` once; if the test browser can't be downloaded,
the launcher falls back to the installed Microsoft Edge automatically.

### For the automation team

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

pytest                                  # framework self-tests + coded + no-code tests
pytest -m smoke --env sit               # a suite on an environment
python -m qaforge studio --open         # the no-code UI
```

After each run open `evidence/<run_id>/index.html` (or Runs in Studio).

> `demo_app/` (a small fake bank used to prove the framework end to end) is git-ignored and
> optional. The example page objects in `apps/retail_banking`, `apps/backoffice` and the tests in
> `tests/retail`, `tests/nocode/specs` target that demo; keep them as references or delete them
> once your own applications are onboarded.

## 1a. QA Forge Studio - what testers do

| Screen | What it is for |
|---|---|
| Tests | Every test (Studio-built and coded), filter by name/tag/JIRA, tick and run on an environment and browser |
| New test | Build a test from steps chosen in lists: open app and sign in, type, click, check, remember values, check Kafka events, read table rows, reuse step groups, run business flows. Values like `${payee.account_number}` come from generated data or earlier steps |
| Record a test | Studio signs in through Okta, opens Playwright's recorder; your clicks become steps. Elements already in a page object are reused automatically; typed passwords are removed |
| Page objects | Every element and its **exact locator from the POM files** (read with Python's `ast`, never executed), fallbacks, which tests use it, and the source file |
| Capture locators | Scan a screen (or pick elements in a visible browser). Each locator is built in Playwright's recommended order and **executed against the live page**; only those matching exactly that one element are kept. Save them to a new or existing page object |
| Health check | Opens each page and counts every page-object locator (and each fallback) on the live app |
| Test data / Field checks | Edit data-driven tables and the "this field must equal that" lists used for event checks |
| Runs | Results with pass/fail stamps, per-test evidence reports, JIRA dry-run plan, one-click evidence integrity check |
| Environments | One tab per environment (+ Add environment): applications, roles, Okta, browser, Kafka, JIRA, secret store, safety, filled in a form and validated before saving. Admins may enter secret values for non-production environments: write-only, kept in git-ignored `config/secrets/<env>.env`, never shown or logged |
| Users and audit | Viewer / tester / admin roles; every sign-in and change is in `studio_data/audit.log.jsonl` |

Studio never runs code typed by a user: tests are YAML (`tests/nocode/specs`), page objects it
creates are generated Python validated before writing (`# qaforge:generated`), and runs are
ordinary pytest runs with the same evidence as coded tests. Hand-written POMs are read-only in Studio.

## 1b. Auditable fallback locators (rule-based, no AI)

```python
self.amount = self.chain(self.tid("tr-amount"),                                  # primary
                         self.page.get_by_label("Amount (INR)", exact=True),      # fallback 1
                         name="amount")
```

* The primary is used whenever it finds exactly one element. A fallback is tried only when the
  primary finds none or several, and only accepted if it finds exactly one.
* Every fallback adds a loud **LOCATOR FALLBACK USED** step with a screenshot, marks the test
  "locator fix needed" in its report, the run index, Studio and the JIRA comment, and is listed in
  `evidence/<run>/locator_fallbacks.json`.
* `locators.fallback_policy: fail` in an environment turns any fallback into a test failure
  (for certification runs).
* Capture locators saves up to two other verified locators as fallbacks automatically.

## 2. What a test looks like

```python
@pytest.mark.jira("BANK-101")
def test_neft_transfer(open_app, ctx, events, verify):
    retail = RetailBanking(open_app("retail", role="maker"))          # Okta SSO + cached session

    payee = ctx.save("payee", BeneficiaryFactory.build())            # synthetic, format-valid
    retail.payments.add_beneficiary(payee)
    ctx.mark_created("payee", entity="beneficiary")                  # -> run ledger for reuse

    transfer = ctx.save("transfer", TransferFactory.build(mode="NEFT"))
    receipt = retail.payments.transfer(payee, transfer)
    ctx.save("txn_ref", receipt.reference)                           # captured from the screen

    events.expect_and_verify("payments.initiated", "payment_initiated", key=ctx["txn_ref"])

    backoffice = Backoffice(open_app("backoffice", role="checker"))  # second app, second role
    row = backoffice.approvals.find_payment(ctx["txn_ref"])
    verify(row, [{"field": "Amount", "expected": "ctx.transfer.amount", "rule": "decimal"},
                 {"field": "Status", "expected": "PENDING_APPROVAL"}], source="ui:backoffice")
```

The mapping `config/mappings/payment_initiated.yaml` says which event field must equal which
entered value; the evidence report shows every field as expected / actual / rule / result.

## 3. Architecture

```
tests/                      pytest tests: markers, JIRA keys, data
apps/<app>/flows/           business journeys returning domain results (receipts, ids)
apps/<app>/pages/           page objects: locators + user-intent methods, no assertions
qaforge/components/         reusable widgets (DataGrid; add date pickers, dropdowns, OTP...)
qaforge/core/               browser manager, AppSession, audited Actions, BasePage
qaforge/auth/               Okta sign-in, RFC 6238 TOTP, session-state cache
qaforge/data/               models, generators, factories, ScenarioContext, RunLedger, YAML loader
qaforge/verifiers/          events (Kafka / HTTP), DB (read-only), field-mapping comparison
qaforge/compliance/         masker, JSON logging, evidence packs, manifest, HTML reports
qaforge/integrations/jira/  REST client (Cloud / DC), results publisher, defect de-duplication
qaforge/plugin.py           pytest fixtures + hooks
config/environments/        base.yaml + one file per env (local, sit, uat, ... prod is guarded)
config/mappings/            UI -> event/DB/API field mappings
testdata/                   data-driven cases (YAML / JSON / CSV)
demo_app/                   QA Demo Bank used for the local self-proving suite
```

Rule of thumb: tests call flows, flows call pages, pages call `self.act.*`. Nobody calls
`locator.click()` directly; that is what makes every step audited and screenshotted.

## 4. Compliance features

| Requirement | How |
|---|---|
| Step-level audit log | Every action logs a structured JSON line (UTC, run id, env, test, JIRA key, app:role, step) to `run.log.jsonl` and the test's own `test.log.jsonl` |
| No sensitive data in logs or reports | Central masker: every resolved secret is masked automatically; keys like password/otp/cvv/token blanked; card (Luhn), account, PAN, Aadhaar-format, bearer tokens, e-mails partially masked |
| No sensitive data in screenshots | Playwright `mask=` on `input[type=password]`, `[data-sensitive]` and each page's `sensitive_locators()` |
| Evidence per test | Steps with screenshots, verification tables, masked data used, events received, Playwright trace, video on failure, traceback |
| Tamper evidence | `manifest.json` with SHA-256 of every file plus a hash over the manifest; sealed zip; `qaforge verify-evidence <zip>` for auditors |
| Who / what / where | `run.json`: triggered by, host, CI build, git commit, framework / Playwright / browser versions, app URLs |
| Secrets | Config holds secret *names* only; values from env/CI or HashiCorp Vault; add CyberArk etc. by implementing `SecretProvider` |
| Production safety | Any prod-like env refused unless config allows it AND `QA_ALLOW_PRODUCTION=I_UNDERSTAND` |
| Read-only verification | DB verifier rejects anything but SELECT/WITH; Kafka verifier never produces or commits offsets |
| Reproducibility | Test-data seed recorded per test; `--data-seed N` regenerates identical data |
| Retries are visible | No silent retries in the framework; if you add `pytest-rerunfailures`, every attempt is its own evidence pack |

## 5. Okta SSO

* Identity Engine (identifier-first) and Classic widgets supported; selectors are overridable per env.
* MFA `totp`: enrol a TOTP authenticator for each automation user in **non-prod**, store the base32
  seed in the vault, reference it with `totp_secret`. Codes are generated in-process (RFC 6238).
* Or ask the Okta team for an MFA-exempt policy for the automation group in non-prod (`mfa: none`).
* Never automate push or SMS factors.
* After the first login, the authenticated storage state is cached per env/app/role in `.auth/`
  (0600, git-ignored, never attached to evidence) for `session_ttl_minutes`. Expired or rejected
  sessions trigger a fresh login automatically.
* Agree a `ready_selector` (element visible only when signed in) with each app team.

## 6. Kafka verification

`events.source: kafka` in the env file plus `pip install "qaforge[kafka]"` (or `[avro]`).
For each expectation a fresh consumer (unique group, no commits) seeks every partition to the test
start time and scans forward, matching on message key, a header, or payload fields. Supports
SASL_SSL (SCRAM / PLAIN / GSSAPI / OAUTHBEARER), mTLS, JSON / Avro with Schema Registry / string.
Things to confirm with the platform team: auth mechanism, read ACLs for the test principal on the
topics, CA certificate, payload format, and which field correlates to the UI reference.

`events.expect_none(...)` proves a rejected transaction did NOT publish anything.

## 7. JIRA

| Setting | Values |
|---|---|
| `jira.deployment` | `cloud` (e-mail + API token) or `datacenter` (Personal Access Token) |
| `jira.mode` | `issues` (comment + attach evidence), `xray` (one Test Execution per run), `zephyr` (Zephyr Scale Cloud executions) |
| `jira.dry_run` | `true` (default): writes every request to `jira_publish_plan.json` instead of sending |
| `jira.defect.*` | auto-defects with signature label so repeats comment on the open bug instead of duplicating |

CLI override per run: `pytest --jira off|dry-run|on`. Review one dry-run plan with your JIRA
admin before switching `dry_run: false`.

## 8. Day-to-day commands

```bash
pytest --env sit -m smoke                       # pick env + suite
pytest --env sit --qa-browser firefox
pytest --data-seed 123456789                    # reproduce data of a previous failure
qaforge doctor --env sit                        # config + secret presence check (never prints values)
qaforge new-page --app crm --name CustomerSearch --path /crm/search
qaforge new-flow --app crm --name Onboarding
qaforge new-test --app crm --role maker --name customer_search --jira BANK-201
qaforge verify-evidence evidence/<run>/tests/<test>.zip
qaforge studio --host 0.0.0.0 --port 8700       # shared team server (put it behind HTTPS / SSO)
qaforge studio-user ravi --role tester           # add a Studio user from the command line
playwright show-trace evidence/<run>/tests/<test>/trace-retail-maker.zip
```

(`qaforge` is available after `pip install -e .`; otherwise use `python -m qaforge`.)

## 9. Bank infrastructure notes

* **Air-gapped install:** mirror the wheels to your internal PyPI; for browsers set
  `PLAYWRIGHT_DOWNLOAD_HOST` to your Artifactory, or use `browser.channel: msedge/chrome` to drive
  the already-approved browser.
* **Proxy / certificates:** `browser.proxy_server`; set `REQUESTS_CA_BUNDLE` for JIRA / Vault calls;
  `ignore_https_errors` is refused for production-like envs.
* **CI later:** the suite is CI-agnostic. Pass `--env`, inject secrets as env vars, archive
  `evidence/<run_id>/`. A Dockerfile is included for containerised agents.

## 10. Onboarding a real application (checklist)

1. Copy `config/environments/sit.yaml`, set URLs, users (secret names), Okta domain, Kafka, JIRA.
2. Ask devs for `data-testid` on interactive elements and `data-sensitive` on PII / balances.
3. `qaforge doctor --env sit` until every secret is present.
4. Scaffold pages / flows; record locators with `playwright codegen <url>`.
5. Write the field mapping YAML for each event / table you must verify.
6. Run with `--jira dry-run`, review the plan, then enable publishing.
