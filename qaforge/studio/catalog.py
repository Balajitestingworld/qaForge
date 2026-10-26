"""Read-side helpers for Studio: tests, runs, environments, data files, mappings."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from qaforge.config.secrets import build_secret_provider
from qaforge.config.settings import ProductionGuardError, load_settings

_cache: dict[str, Any] = {"at": 0.0, "stamp": None, "items": [], "error": None}
_cache_lock = threading.Lock()


def _tests_stamp(root: Path) -> float:
    paths = list((root / "tests").rglob("*.py")) + list((root / "tests").rglob("*.yaml")) + \
        list((root / "testdata").rglob("*.yaml"))
    return max((p.stat().st_mtime for p in paths), default=0.0)


def collect_tests(root: Path, force: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    """Ask pytest itself which tests exist (same rules as a real run). Cached until files change."""
    stamp = _tests_stamp(root)
    with _cache_lock:
        if not force and _cache["stamp"] == stamp and time.time() - _cache["at"] < 600:
            return _cache["items"], _cache["error"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "catalog.json"
            proc = subprocess.run(  # noqa: S603 - fixed arguments
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "", "-p", "no:cacheprovider",
                 f"--qaf-catalog={out}"], cwd=root, capture_output=True, text=True, timeout=180)
            items = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
            error = None if proc.returncode in (0, 5) else (proc.stdout + proc.stderr)[-3000:]
        items = [i for i in items if not i["nodeid"].startswith("tests/framework/")]
        _cache.update(at=time.time(), stamp=stamp, items=items, error=error)
        return items, error


def envs(root: Path) -> list[str]:
    return sorted(p.stem for p in (root / "config" / "environments").glob("*.yaml") if p.stem != "base")


def env_summary(root: Path, env: str) -> dict[str, Any]:
    out: dict[str, Any] = {"env": env, "blocked": None, "error": None, "apps": {}, "roles": [], "secrets": [],
                           "events": None, "jira": None, "production": False}
    try:
        s = load_settings(env=env, root_dir=root)
    except ProductionGuardError as exc:
        out.update(blocked=str(exc), production=True)
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    out["production"] = s.is_production
    out["apps"] = {k: {"base_url": v.base_url, "auth": v.auth, "description": v.description} for k, v in s.apps.items()}
    out["roles"] = sorted(s.users)
    try:
        sec = build_secret_provider(s)
        for role, u in s.users.items():
            for label, name in (("username", u.username_secret), ("password", u.password_secret),
                                ("one-time code seed", u.totp_secret)):
                if name:
                    out["secrets"].append({"role": role, "what": label, "name": name, "present": sec.has(name)})
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"Secret store not reachable: {exc}"
    out["events"] = s.events.source
    out["jira"] = {"enabled": s.jira.enabled, "dry_run": s.jira.dry_run, "mode": s.jira.mode,
                   "deployment": s.jira.deployment}
    out["browser"] = s.browser.channel or s.browser.name
    return out


def validate_env_yaml(root: Path, env: str, text: str) -> str | None:
    """Returns an error message, or None when the YAML is a valid environment config."""
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"Not valid YAML: {exc}"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "base.yaml").write_text((root / "config/environments/base.yaml").read_text(encoding="utf-8"),
                                     encoding="utf-8")
        (d / f"{env}.yaml").write_text(text, encoding="utf-8")
        try:
            load_settings(env=env, config_dir=d, root_dir=root)
        except ProductionGuardError:
            return None  # valid, just guarded
        except Exception as exc:  # noqa: BLE001
            return str(exc)
    return None


def runs(root: Path, evidence_dir: str = "evidence", limit: int = 60) -> list[dict[str, Any]]:
    base = root / evidence_dir
    out = []
    if not base.exists():
        return out
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)[:limit]:
        summary = d / "run_summary.json"
        if summary.exists():
            s = json.loads(summary.read_text(encoding="utf-8"))
            counts = s.get("counts", {})
            outcome = "failed" if counts.get("failed") or counts.get("error") else \
                ("passed" if counts.get("passed") else "skipped")
            out.append({"run_id": d.name, "env": s["run"].get("env"), "started": s["run"].get("started_at"),
                        "finished": s["run"].get("finished_at"), "by": s["run"].get("triggered_by"),
                        "counts": counts, "total": s.get("total", 0), "outcome": outcome,
                        "results": s.get("results", []), "has_jira": (d / "jira_publish_result.json").exists()})
        elif (d / "tests").exists() or (d / "run.log.jsonl").exists():
            out.append({"run_id": d.name, "env": None, "started": None, "finished": None, "by": None,
                        "counts": {}, "total": 0, "outcome": "running", "results": [], "has_jira": False})
    return out


DATA_EXT = {".yaml", ".yml", ".json", ".csv"}


def data_files(root: Path) -> list[str]:
    base = root / "testdata"
    return sorted(str(p.relative_to(root)) for p in base.rglob("*") if p.suffix in DATA_EXT and "files" not in p.parts)


def mapping_names(root: Path) -> list[str]:
    return sorted(p.stem for p in (root / "config" / "mappings").glob("*.yaml"))


# --------------------------------------------------------------------------- environment form
ENV_NAME_RE = r"^[a-z][a-z0-9_-]{1,20}$"
SECRET_NAME_RE = r"^[A-Z][A-Z0-9_]{1,60}$"

ENV_FORM: list[dict[str, Any]] = [
    {"title": "Applications", "help": "Every web application tests open in this environment.",
     "list": "apps", "item": "application", "key_label": "Name used in tests", "fields": [
        {"key": "base_url", "label": "Address", "placeholder": "https://retail-sit.bank.internal", "required": True},
        {"key": "auth", "label": "Sign-in", "kind": "select", "choices": ["okta", "none"]},
        {"key": "landing_path", "label": "Page after sign-in", "placeholder": "/dashboard"},
        {"key": "ready_selector", "label": "Element shown only when signed in", "placeholder": '[data-testid="app-header"]'},
        {"key": "description", "label": "Description"}]},
    {"title": "Test users (roles)", "help": "Names of the secrets that hold each role's sign-in. Values live in the secret store, never here.",
     "list": "users", "item": "role", "key_label": "Role", "fields": [
        {"key": "username_secret", "label": "Username secret name", "placeholder": "SIT_MAKER_USER", "required": True, "secret": True},
        {"key": "password_secret", "label": "Password secret name", "placeholder": "SIT_MAKER_PASSWORD", "required": True, "secret": True},
        {"key": "totp_secret", "label": "One-time code seed secret name", "placeholder": "SIT_MAKER_TOTP_SEED", "secret": True},
        {"key": "description", "label": "Description"}]},
    {"title": "Okta sign-in", "fields": [
        {"key": "okta.domain", "label": "Okta address", "placeholder": "https://yourbank-nonprod.okta.com"},
        {"key": "okta.mfa", "label": "Second factor", "kind": "select", "choices": ["totp", "none", "manual"],
         "help": "totp: codes generated from the stored seed. none: MFA-exempt automation users. manual: someone types it (visible browser only)."},
        {"key": "okta.session_ttl_minutes", "label": "Reuse a sign-in for (minutes)", "kind": "number"}]},
    {"title": "Browser", "fields": [
        {"key": "browser.name", "label": "Browser engine", "kind": "select", "choices": ["chromium", "firefox", "webkit"]},
        {"key": "browser.channel", "label": "Installed browser to use", "kind": "select", "choices": ["", "msedge", "chrome"],
         "help": "Leave empty to use Playwright's own browser."},
        {"key": "browser.headless", "label": "Run without a visible window", "kind": "bool"},
        {"key": "browser.proxy_server", "label": "Proxy", "placeholder": "http://proxy.bank.internal:8080"},
        {"key": "browser.ignore_https_errors", "label": "Ignore certificate errors (never in production)", "kind": "bool"}]},
    {"title": "Events (Kafka)", "fields": [
        {"key": "events.source", "label": "Where events are read from", "kind": "select", "choices": ["none", "kafka", "http"]},
        {"key": "events.default_timeout_s", "label": "Wait for an event up to (seconds)", "kind": "number"},
        {"key": "events.kafka.bootstrap_servers", "label": "Kafka brokers", "placeholder": "kafka-1:9093,kafka-2:9093"},
        {"key": "events.kafka.security_protocol", "label": "Security protocol", "kind": "select",
         "choices": ["SASL_SSL", "SSL", "SASL_PLAINTEXT", "PLAINTEXT"]},
        {"key": "events.kafka.sasl_mechanism", "label": "SASL mechanism", "kind": "select",
         "choices": ["SCRAM-SHA-512", "SCRAM-SHA-256", "PLAIN", "GSSAPI", "OAUTHBEARER", ""]},
        {"key": "events.kafka.username_secret", "label": "Kafka username secret name", "secret": True},
        {"key": "events.kafka.password_secret", "label": "Kafka password secret name", "secret": True},
        {"key": "events.kafka.ssl_ca_location", "label": "CA certificate file", "placeholder": "C:/certs/bank-ca.pem"},
        {"key": "events.kafka.value_format", "label": "Message format", "kind": "select", "choices": ["json", "avro", "string"]},
        {"key": "events.kafka.schema_registry_url", "label": "Schema Registry address (Avro)"},
        {"key": "events.http_base_url", "label": "HTTP event endpoint (only for source http)"}]},
    {"title": "JIRA", "fields": [
        {"key": "jira.enabled", "label": "Publish results to JIRA", "kind": "bool"},
        {"key": "jira.dry_run", "label": "Dry run: prepare but don't send", "kind": "bool"},
        {"key": "jira.deployment", "label": "JIRA type", "kind": "select", "choices": ["cloud", "datacenter"]},
        {"key": "jira.base_url", "label": "JIRA address", "placeholder": "https://jira.bank.internal"},
        {"key": "jira.project_key", "label": "Project key", "placeholder": "BANK"},
        {"key": "jira.mode", "label": "Test management", "kind": "select", "choices": ["issues", "xray", "zephyr"]},
        {"key": "jira.user_secret", "label": "Account e-mail secret name (Cloud)", "secret": True},
        {"key": "jira.token_secret", "label": "API token / personal access token secret name", "secret": True}]},
    {"title": "Secret store", "fields": [
        {"key": "secrets.provider", "label": "Secrets come from", "kind": "select", "choices": ["env", "vault"],
         "help": "env: environment variables set by IT, or values entered below. vault: HashiCorp Vault."},
        {"key": "secrets.vault_addr", "label": "Vault address"},
        {"key": "secrets.vault_mount", "label": "Vault mount"},
        {"key": "secrets.vault_path", "label": "Vault path", "placeholder": "qa/sit/automation"}]},
    {"title": "Safety", "fields": [
        {"key": "locators.fallback_policy", "label": "When a fallback locator is needed", "kind": "select",
         "choices": ["warn", "fail"], "help": "warn: continue and flag the test. fail: stop the test."},
        {"key": "is_production", "label": "This is a production environment", "kind": "bool"},
        {"key": "allow_production", "label": "Allow automation here (production only)", "kind": "bool"}]},
]


def env_file(root: Path, env: str) -> Path:
    import re

    if not re.match(ENV_NAME_RE, env) or env == "base":
        raise ValueError("Environment name: lowercase letters, digits, - or _, 2-21 characters")
    return root / "config" / "environments" / f"{env}.yaml"


def env_raw(root: Path, env: str) -> dict[str, Any]:
    f = env_file(root, env)
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}


def base_raw(root: Path) -> dict[str, Any]:
    return yaml.safe_load((root / "config/environments/base.yaml").read_text(encoding="utf-8")) or {}


def secret_names(raw: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for u in (raw.get("users") or {}).values():
        names += [u.get(k) for k in ("username_secret", "password_secret", "totp_secret")]
    k = ((raw.get("events") or {}).get("kafka") or {})
    names += [k.get("username_secret"), k.get("password_secret")]
    j = raw.get("jira") or {}
    if j.get("enabled"):
        names += [j.get("user_secret"), j.get("token_secret")]
    return list(dict.fromkeys(n for n in names if n))


def local_secret_file(root: Path, env: str) -> Path:
    return root / "config" / "secrets" / f"{env}.env"


def set_local_secret(root: Path, env: str, name: str, value: str) -> None:
    """Write-only secret entry for teams without a vault. File is git-ignored and owner-readable only."""
    import os
    import re

    if not re.match(SECRET_NAME_RE, name):
        raise ValueError("Secret names: capital letters, digits and underscores")
    if "\n" in value or "\r" in value:
        raise ValueError("Secret values can't contain line breaks")
    f = local_secret_file(root, env)
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else [
        f"# Secrets for '{env}' entered in QA Forge Studio. Git-ignored. Prefer your vault for shared environments."]
    lines = [ln for ln in lines if not ln.startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
    tmp = f.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(f)
