"""Secret resolution. Config files only ever hold secret *names*; values come from here.

Providers:
  EnvSecretProvider   - process environment (+ optional dotenv files for local/demo runs).
                        CI tools and enterprise agents inject vaulted values as env vars.
  VaultSecretProvider - HashiCorp Vault KV v2 over its REST API (no extra dependency).

To add CyberArk / Azure Key Vault / AWS Secrets Manager, implement SecretProvider.get().
Every resolved value is registered with the masker, so it can never appear in evidence.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

import requests

from qaforge.compliance.masker import masker
from qaforge.config.settings import Settings


class SecretNotFoundError(KeyError):
    pass


class SecretProvider(ABC):
    @abstractmethod
    def _lookup(self, name: str) -> str | None: ...

    def get(self, name: str) -> str:
        value = self._lookup(name)
        if value is None or value == "":
            raise SecretNotFoundError(
                f"Secret '{name}' is not available from {type(self).__name__}. "
                "Provide it via your vault / CI secret store (never in config files)."
            )
        masker.register_secret(value)
        return value

    def get_optional(self, name: str | None) -> str | None:
        if not name:
            return None
        try:
            return self.get(name)
        except SecretNotFoundError:
            return None

    def has(self, name: str) -> bool:
        return bool(self._lookup(name))


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class EnvSecretProvider(SecretProvider):
    def __init__(self, dotenv_files: list[Path] | None = None) -> None:
        self._file_values: dict[str, str] = {}
        for f in dotenv_files or []:
            if f.exists():
                self._file_values.update(_parse_dotenv(f))

    def _lookup(self, name: str) -> str | None:
        return os.environ.get(name) or self._file_values.get(name)


class VaultSecretProvider(SecretProvider):
    """Reads one KV v2 secret document (vault_mount/vault_path) and caches it for the run."""

    def __init__(self, addr: str, mount: str, path: str, token: str, verify: bool | str = True) -> None:
        self._addr, self._mount, self._path = addr.rstrip("/"), mount, path
        self._token, self._verify = token, verify
        self._cache: dict[str, str] | None = None
        masker.register_secret(token)

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            url = f"{self._addr}/v1/{self._mount}/data/{self._path}"
            resp = requests.get(url, headers={"X-Vault-Token": self._token}, timeout=15, verify=self._verify)
            resp.raise_for_status()
            self._cache = {k: str(v) for k, v in resp.json()["data"]["data"].items()}
        return self._cache

    def _lookup(self, name: str) -> str | None:
        return os.environ.get(name) or self._load().get(name)


def build_secret_provider(settings: Settings) -> SecretProvider:
    cfg = settings.secrets
    if cfg.provider == "vault":
        token = os.environ.get(cfg.vault_token_env, "")
        if not token:
            raise SecretNotFoundError(f"Vault token env var '{cfg.vault_token_env}' is not set")
        return VaultSecretProvider(cfg.vault_addr, cfg.vault_mount, cfg.vault_path, token)
    return EnvSecretProvider([settings.path(f) for f in cfg.dotenv_files])
