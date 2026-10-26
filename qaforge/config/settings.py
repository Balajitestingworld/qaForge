"""Typed, layered configuration.

Resolution order (later wins):
    1. config/environments/base.yaml
    2. config/environments/<env>.yaml
    3. Environment variables:  QAF__browser__headless=false  (double underscore = nesting)
    4. pytest command line options (--env, --headed, --browser ...)

Every value is validated by pydantic, so a typo in YAML fails fast instead of mid-run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

PRODUCTION_NAMES = {"prod", "production", "prd", "live"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(_Strict):
    base_url: str
    auth: Literal["okta", "none"] = "okta"
    landing_path: str = "/"
    #: CSS selector visible only when signed in - lets login detection avoid fixed sleeps
    ready_selector: str | None = None
    description: str = ""


class BrowserConfig(_Strict):
    name: Literal["chromium", "firefox", "webkit"] = "chromium"
    channel: str | None = None  # "chrome", "msedge" for branded browsers
    headless: bool = True
    slow_mo_ms: int = 0
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-IN"
    timezone_id: str = "Asia/Kolkata"
    ignore_https_errors: bool = False
    proxy_server: str | None = None
    default_timeout_ms: int = 15_000
    navigation_timeout_ms: int = 30_000
    extra_args: list[str] = Field(default_factory=list)


class EvidenceConfig(_Strict):
    output_dir: str = "evidence"
    screenshot: Literal["each_step", "on_failure", "off"] = "each_step"
    full_page_screenshots: bool = False
    trace: Literal["on", "retain_on_failure", "off"] = "on"
    video: Literal["on", "retain_on_failure", "off"] = "retain_on_failure"
    har: bool = False  # off by default: HAR files capture request bodies, including credentials
    zip_pack: bool = True
    # CSS selectors always blacked out in screenshots, on every page
    mask_selectors: list[str] = Field(
        default_factory=lambda: ["input[type=password]", "[data-sensitive]"]
    )


class OktaSelectors(_Strict):
    """Defaults cover Okta Identity Engine (identifier-first) and the Classic sign-in widget."""

    username: str = 'input[name="identifier"], input[name="username"], #okta-signin-username'
    password: str = 'input[name="credentials.passcode"][type="password"], #okta-signin-password'
    next_button: str = 'input[type="submit"][value="Next"], button[type="submit"]:has-text("Next")'
    submit_button: str = (
        'input[type="submit"][value="Verify"], input[type="submit"][value="Sign in"], '
        '#okta-signin-submit, button[type="submit"]:has-text("Verify")'
    )
    mfa_factor: str | None = None  # e.g. '[data-se="google_otp"] a' if the factor picker is shown
    totp_input: str = 'input[name="credentials.passcode"]:not([type="password"]), input[name="answer"]'
    error_banner: str = '.o-form-error-container, .okta-form-infobox-error, [role="alert"]'


class OktaConfig(_Strict):
    domain: str = ""
    mfa: Literal["totp", "none", "manual"] = "totp"
    session_ttl_minutes: int = 45
    session_dir: str = ".auth"
    selectors: OktaSelectors = Field(default_factory=OktaSelectors)


class UserConfig(_Strict):
    """Only *names* of secrets live in config - never the values."""

    username_secret: str
    password_secret: str
    totp_secret: str | None = None
    description: str = ""


class KafkaConfig(_Strict):
    bootstrap_servers: str = ""
    security_protocol: Literal["PLAINTEXT", "SSL", "SASL_SSL", "SASL_PLAINTEXT"] = "SASL_SSL"
    sasl_mechanism: str | None = "SCRAM-SHA-512"  # PLAIN, SCRAM-SHA-256, SCRAM-SHA-512, GSSAPI, OAUTHBEARER
    username_secret: str | None = None
    password_secret: str | None = None
    ssl_ca_location: str | None = None
    ssl_certificate_location: str | None = None
    ssl_key_location: str | None = None
    group_id_prefix: str = "qaforge-verifier"
    value_format: Literal["json", "avro", "string"] = "json"
    schema_registry_url: str | None = None
    schema_registry_user_secret: str | None = None
    schema_registry_password_secret: str | None = None
    lookback_seconds: int = 30
    extra: dict[str, Any] = Field(default_factory=dict)


class EventsConfig(_Strict):
    source: Literal["kafka", "http", "none"] = "none"
    default_timeout_s: int = 60
    poll_interval_s: float = 1.0
    http_base_url: str = ""
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)


class DefectConfig(_Strict):
    enabled: bool = True
    issue_type: str = "Bug"
    priority: str | None = None
    labels: list[str] = Field(default_factory=lambda: ["automation", "qaforge"])
    components: list[str] = Field(default_factory=list)
    link_type: str = "Relates"


class JiraConfig(_Strict):
    enabled: bool = False
    dry_run: bool = True  # writes the exact payloads to evidence instead of calling JIRA
    deployment: Literal["cloud", "datacenter"] = "cloud"
    base_url: str = ""
    project_key: str = ""
    mode: Literal["issues", "xray", "zephyr"] = "issues"
    user_secret: str | None = "JIRA_USER"  # Cloud: account email. DC: unused with PAT
    token_secret: str = "JIRA_TOKEN"  # Cloud: API token. DC: Personal Access Token
    attach_evidence: bool = True
    max_attachment_mb: int = 20
    verify_tls: bool | str = True  # True, False (never in prod) or path to a CA bundle
    xray_client_id_secret: str | None = "XRAY_CLIENT_ID"
    xray_client_secret_secret: str | None = "XRAY_CLIENT_SECRET"
    zephyr_token_secret: str | None = "ZEPHYR_TOKEN"
    zephyr_cycle_key: str | None = None
    defect: DefectConfig = Field(default_factory=DefectConfig)


class SecretsConfig(_Strict):
    provider: Literal["env", "vault"] = "env"
    dotenv_files: list[str] = Field(default_factory=list)  # for local/demo only
    vault_addr: str = ""
    vault_mount: str = "secret"
    vault_path: str = ""
    vault_token_env: str = "VAULT_TOKEN"


class DataConfig(_Strict):
    seed: int | None = None  # fixed seed = reproducible data; None = derived per run
    ifsc_bank_codes: list[str] = Field(default_factory=lambda: ["TEST", "QAFB"])
    test_card_bins: list[str] = Field(default_factory=lambda: ["400000", "510000"])
    account_number_length: int = 12
    mobile_prefix: str = "9"
    email_domain: str = "example.test"
    ledger_path: str = "evidence/ledger.sqlite"


class LocatorConfig(_Strict):
    #: warn = use the fallback, flag the test for a fix; fail = treat any fallback as a test failure
    fallback_policy: Literal["warn", "fail"] = "warn"


class Settings(_Strict):
    env: str
    is_production: bool = False
    allow_production: bool = False
    framework_label: str = "qaforge"
    apps: dict[str, AppConfig] = Field(default_factory=dict)
    users: dict[str, UserConfig] = Field(default_factory=dict)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    okta: OktaConfig = Field(default_factory=OktaConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    locators: LocatorConfig = Field(default_factory=LocatorConfig)
    log_level: str = "INFO"
    root_dir: str = "."

    @field_validator("env")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    def app(self, name: str) -> AppConfig:
        if name not in self.apps:
            raise KeyError(f"App '{name}' is not configured for env '{self.env}'. Known: {sorted(self.apps)}")
        return self.apps[name]

    def user(self, role: str) -> UserConfig:
        if role not in self.users:
            raise KeyError(f"Role '{role}' is not configured for env '{self.env}'. Known: {sorted(self.users)}")
        return self.users[role]

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else Path(self.root_dir) / p


class ProductionGuardError(RuntimeError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _env_overrides(prefix: str = "QAF__") -> dict:
    result: dict = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = [p.lower() for p in key[len(prefix):].split("__") if p]
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(value)
    return result


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(
    env: str | None = None,
    config_dir: str | Path = "config/environments",
    root_dir: str | Path = ".",
    cli_overrides: dict | None = None,
) -> Settings:
    root = Path(root_dir).resolve()
    env_name = (env or os.environ.get("QA_ENV") or "local").lower()
    cfg_dir = Path(config_dir)
    cfg_dir = cfg_dir if cfg_dir.is_absolute() else root / cfg_dir

    env_file = cfg_dir / f"{env_name}.yaml"
    if not env_file.exists():
        available = sorted(p.stem for p in cfg_dir.glob("*.yaml") if p.stem != "base")
        raise FileNotFoundError(f"No config for env '{env_name}' at {env_file}. Available: {available}")

    merged = _deep_merge(_read_yaml(cfg_dir / "base.yaml"), _read_yaml(env_file))
    merged = _deep_merge(merged, _env_overrides())
    merged = _deep_merge(merged, cli_overrides or {})
    merged["env"] = env_name
    merged["root_dir"] = str(root)

    settings = Settings.model_validate(merged)
    enforce_production_guard(settings)
    return settings


def enforce_production_guard(settings: Settings) -> None:
    """Automation must never touch production by accident.

    Running against prod needs BOTH `allow_production: true` in the env file AND
    the operator-supplied variable QA_ALLOW_PRODUCTION=I_UNDERSTAND.
    """
    looks_prod = settings.is_production or settings.env in PRODUCTION_NAMES
    if not looks_prod:
        return
    if settings.allow_production and os.environ.get("QA_ALLOW_PRODUCTION") == "I_UNDERSTAND":
        return
    raise ProductionGuardError(
        f"Refusing to run against production-like env '{settings.env}'. "
        "Set allow_production: true in its config AND QA_ALLOW_PRODUCTION=I_UNDERSTAND to override."
    )
