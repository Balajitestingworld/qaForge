"""Thin JIRA REST client (Cloud and Data Center / Server), built on `requests`.

Uses REST API v2 on both deployments because it accepts wiki-markup strings everywhere,
which keeps descriptions and comments simple. Retries on 429/5xx honouring Retry-After.

Auth:
  Cloud       - account e-mail + API token (HTTP basic)
  Data Center - Personal Access Token (Bearer)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from qaforge.compliance.logger import get_logger
from qaforge.compliance.masker import masker

log = get_logger("jira")


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, deployment: str, token: str, user: str | None = None,
                 verify_tls: bool | str = True, timeout_s: float = 30, max_retries: int = 4) -> None:
        self.base = base_url.rstrip("/")
        self.deployment = deployment
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.http = requests.Session()
        self.http.verify = verify_tls
        self.http.headers.update({"Accept": "application/json"})
        masker.register_secret(token)
        if deployment == "cloud":
            if not user:
                raise JiraError("JIRA Cloud needs the account e-mail (jira.user_secret) and an API token")
            self.http.auth = (user, token)
        else:
            self.http.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(self.max_retries + 1):
            resp = self.http.request(method, url, timeout=self.timeout_s, **kw)
            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("JIRA %s %s -> %s, retrying in %.0fs", method, path, resp.status_code, wait)
                time.sleep(min(wait, 60))
                continue
            if resp.status_code >= 400:
                raise JiraError(f"JIRA {method} {path} failed: {resp.status_code} {masker.mask(resp.text[:500])}")
            return resp.json() if resp.content and "json" in resp.headers.get("Content-Type", "") else None
        raise JiraError(f"JIRA {method} {path} failed after retries")

    # ------------------------------------------------------------- issues
    def get_issue(self, key: str, fields: str = "summary,status") -> dict[str, Any]:
        return self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": fields})

    def search(self, jql: str, fields: str = "summary,status", max_results: int = 20) -> list[dict[str, Any]]:
        # Cloud retired /search in favour of /search/jql; Data Center keeps /search
        path = "/rest/api/2/search/jql" if self.deployment == "cloud" else "/rest/api/2/search"
        data = self._request("GET", path, params={"jql": jql, "fields": fields, "maxResults": max_results})
        return (data or {}).get("issues", [])

    def create_issue(self, fields: dict[str, Any]) -> str:
        data = self._request("POST", "/rest/api/2/issue", json={"fields": fields})
        return data["key"]

    def add_comment(self, key: str, body: str) -> None:
        self._request("POST", f"/rest/api/2/issue/{key}/comment", json={"body": masker.mask(body)})

    def add_attachment(self, key: str, file_path: Path) -> None:
        with file_path.open("rb") as fh:
            self._request("POST", f"/rest/api/2/issue/{key}/attachments",
                          headers={"X-Atlassian-Token": "no-check"},
                          files={"file": (file_path.name, fh, "application/octet-stream")})

    def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self._request("POST", "/rest/api/2/issueLink", json={
            "type": {"name": link_type}, "inwardIssue": {"key": inward_key}, "outwardIssue": {"key": outward_key}})

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/rest/api/2/myself")
