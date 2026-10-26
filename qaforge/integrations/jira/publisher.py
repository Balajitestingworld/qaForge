"""Publishes run results to JIRA after the run.

Modes (jira.mode):
  issues - test cases are plain JIRA issues: add a result comment + attach the evidence zip
  xray   - Xray (Cloud or DC): import one Test Execution with every linked test's status
  zephyr - Zephyr Scale Cloud: create a test execution per linked test case in a cycle

In every mode, failures can raise defects (jira.defect.enabled) with DE-DUPLICATION: each
failure gets a stable signature (test + failing step + error type) stored as a label; if an
open bug already carries it, the run adds a "reproduced" comment instead of a duplicate bug.

jira.dry_run=true (the default) runs the exact same logic against a recorder and writes
every request that WOULD be sent to <run_dir>/jira_publish_plan.json - review it before
switching JIRA on, and keep it as evidence of what was published.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol

import requests

from qaforge.compliance.logger import get_logger
from qaforge.compliance.masker import masker
from qaforge.config.secrets import SecretProvider
from qaforge.config.settings import Settings
from qaforge.integrations.jira.client import JiraClient

log = get_logger("jira.publish")


class JiraLike(Protocol):
    def search(self, jql: str, fields: str = ..., max_results: int = ...) -> list[dict[str, Any]]: ...
    def create_issue(self, fields: dict[str, Any]) -> str: ...
    def add_comment(self, key: str, body: str) -> None: ...
    def add_attachment(self, key: str, file_path: Path) -> None: ...
    def link(self, link_type: str, inward_key: str, outward_key: str) -> None: ...


class RecordingJira:
    """Dry-run stand-in: records every call, returns harmless fake responses."""

    def __init__(self, existing_signatures: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._existing = existing_signatures or set()
        self._n = 0

    def search(self, jql: str, fields: str = "summary,status", max_results: int = 20) -> list[dict[str, Any]]:
        self.calls.append({"call": "search", "jql": jql})
        return [{"key": "DRYRUN-EXISTING"}] if any(s in jql for s in self._existing) else []

    def create_issue(self, fields: dict[str, Any]) -> str:
        self._n += 1
        key = f"DRYRUN-{self._n}"
        self.calls.append({"call": "create_issue", "result_key": key, "fields": masker.mask_obj(fields)})
        return key

    def add_comment(self, key: str, body: str) -> None:
        self.calls.append({"call": "add_comment", "issue": key, "body": masker.mask(body)})

    def add_attachment(self, key: str, file_path: Path) -> None:
        self.calls.append({"call": "add_attachment", "issue": key, "file": str(file_path),
                           "bytes": file_path.stat().st_size if file_path.exists() else None})

    def link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self.calls.append({"call": "link", "type": link_type, "inward": inward_key, "outward": outward_key})

    def post_external(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"call": "POST", "url": url, "payload": masker.mask_obj(_truncate_b64(payload))})
        return {"key": "DRYRUN-EXEC"}


def _truncate_b64(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: (f"<base64 {len(v)} chars>" if k == "data" and isinstance(v, str) else _truncate_b64(v))
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [_truncate_b64(v) for v in payload]
    return payload


def failure_signature(result: dict[str, Any]) -> str:
    failed_step = next((s["name"] for s in result.get("steps", []) if s["status"] == "failed"), "")
    err_type = (result.get("error") or "").split(":", 1)[0].split("\n")[0][:80]
    raw = f"{result['nodeid']}|{failed_step}|{err_type}"
    return "qaf-sig-" + hashlib.sha1(raw.encode()).hexdigest()[:12]


class ResultsPublisher:
    def __init__(self, settings: Settings, secrets: SecretProvider, run_dir: Path,
                 client: JiraLike | None = None) -> None:
        self.settings = settings
        self.cfg = settings.jira
        self.secrets = secrets
        self.run_dir = run_dir
        self.dry_run = self.cfg.dry_run
        if client is not None:
            self.jira: Any = client
        elif self.dry_run:
            self.jira = RecordingJira()
        else:
            self.jira = JiraClient(
                self.cfg.base_url, self.cfg.deployment, token=secrets.get(self.cfg.token_secret),
                user=secrets.get_optional(self.cfg.user_secret) if self.cfg.deployment == "cloud" else None,
                verify_tls=self.cfg.verify_tls,
            )

    # ----------------------------------------------------------------- main
    def publish(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        detailed = [self._load_detail(r) for r in results]
        linked = [r for r in detailed if r.get("jira_keys")]
        outcome: dict[str, Any] = {"mode": self.cfg.mode, "dry_run": self.dry_run, "linked_tests": len(linked),
                                   "defects": [], "errors": []}
        try:
            if self.cfg.mode == "issues":
                for r in linked:
                    self._publish_issue_result(r)
            elif self.cfg.mode == "xray":
                outcome["test_execution"] = self._publish_xray(linked)
            elif self.cfg.mode == "zephyr":
                self._publish_zephyr(linked)
        except Exception as exc:  # noqa: BLE001 - publishing must never fail the run silently or loudly
            log.error("JIRA result publishing failed: %s", exc)
            outcome["errors"].append(masker.mask(str(exc)))

        if self.cfg.defect.enabled:
            for r in detailed:
                if r["outcome"] in ("failed", "error"):
                    try:
                        outcome["defects"].append(self._raise_or_update_defect(r))
                    except Exception as exc:  # noqa: BLE001
                        log.error("Defect handling failed for %s: %s", r["nodeid"], exc)
                        outcome["errors"].append(masker.mask(str(exc)))

        if isinstance(self.jira, RecordingJira):
            plan = {"note": "DRY RUN - nothing was sent to JIRA. These are the requests that would be made.",
                    "outcome": outcome, "calls": self.jira.calls}
            (self.run_dir / "jira_publish_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            log.info("JIRA dry run: %d request(s) written to jira_publish_plan.json", len(self.jira.calls))
        (self.run_dir / "jira_publish_result.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
        return outcome

    def _load_detail(self, summary: dict[str, Any]) -> dict[str, Any]:
        f = Path(summary["evidence_dir"]) / "test.json"
        detail = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return {**detail, **summary}

    def _zip_ok(self, r: dict[str, Any]) -> Path | None:
        z = r.get("evidence_zip")
        if not (self.cfg.attach_evidence and z):
            return None
        p = Path(z)
        if not p.exists():
            return None
        if p.stat().st_size > self.cfg.max_attachment_mb * 1024 * 1024:
            log.warning("Evidence %s exceeds %d MB; not attached (link kept in comment)", p.name,
                        self.cfg.max_attachment_mb)
            return None
        return p

    # ------------------------------------------------------- plain issues
    def _result_comment(self, r: dict[str, Any]) -> str:
        icon = "(/)" if r["outcome"] == "passed" else "(x)" if r["outcome"] in ("failed", "error") else "(!)"
        run = r.get("run", {})
        lines = [
            f"h4. {icon} Automated run: *{r['outcome'].upper()}*",
            "||Field||Value||",
            f"|Environment|{r.get('env', run.get('env', '')).upper()}|",
            f"|Run ID|{r['run_id']}|",
            f"|Test|{{{{{r['nodeid']}}}}}|",
            f"|Duration|{r.get('duration_s')} s|",
            f"|Executed by|{run.get('triggered_by', '')} @ {run.get('host', '')}|",
            f"|Build / commit|{run.get('ci_build') or 'local run'} / {run.get('git_commit') or 'n/a'}|",
            f"|Evidence SHA-256|{{{{{r.get('evidence_zip_sha256') or r.get('manifest_sha256', '')}}}}}|",
        ]
        for fb in r.get("locator_fallbacks") or []:
            lines.append(f"(!) *LOCATOR FALLBACK USED* for {{{{{fb['element']}}}}}: primary {{{{{fb['primary']}}}}} "
                         f"found {fb['primary_count']}, used {{{{{fb['used']}}}}} - page object needs a fix")
        if r.get("error"):
            lines += ["{noformat}", masker.mask(r["error"])[:1500], "{noformat}"]
        return "\n".join(lines)

    def _publish_issue_result(self, r: dict[str, Any]) -> None:
        body = self._result_comment(r)
        zip_path = self._zip_ok(r)
        for key in r["jira_keys"]:
            self.jira.add_comment(key, body)
            if zip_path:
                self.jira.add_attachment(key, zip_path)
            log.info("JIRA %s updated with %s result", key, r["outcome"])

    # --------------------------------------------------------------- xray
    def _xray_status(self, outcome: str) -> str:
        return {"passed": "PASSED", "failed": "FAILED", "error": "FAILED"}.get(outcome, "TODO")

    def _publish_xray(self, linked: list[dict[str, Any]]) -> str | None:
        if not linked:
            return None
        tests = []
        for r in linked:
            evidence = []
            z = self._zip_ok(r)
            if z:
                evidence.append({"data": base64.b64encode(z.read_bytes()).decode(), "filename": z.name,
                                 "contentType": "application/zip"})
            for key in r["jira_keys"]:
                tests.append({"testKey": key, "status": self._xray_status(r["outcome"]),
                              "start": r.get("started_at"), "finish": r.get("finished_at"),
                              "comment": masker.mask((r.get("error") or "Passed")[:2000]), "evidence": evidence})
        run_id = linked[0]["run_id"]
        payload = {"info": {"project": self.cfg.project_key,
                            "summary": f"[Automation][{self.settings.env.upper()}] Run {run_id}",
                            "description": f"qaforge run {run_id}"}, "tests": tests}
        if self.cfg.deployment == "cloud":
            url = "https://xray.cloud.getxray.app/api/v2/import/execution"
            if isinstance(self.jira, RecordingJira):
                return self.jira.post_external(url, payload)["key"]
            token = requests.post(
                "https://xray.cloud.getxray.app/api/v2/authenticate", timeout=30,
                json={"client_id": self.secrets.get(self.cfg.xray_client_id_secret or ""),
                      "client_secret": self.secrets.get(self.cfg.xray_client_secret_secret or "")},
            ).json()
            masker.register_secret(token)
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=120)
        else:
            url = f"{self.cfg.base_url.rstrip('/')}/rest/raven/2.0/import/execution"
            if isinstance(self.jira, RecordingJira):
                return self.jira.post_external(url, payload)["key"]
            resp = self.jira.http.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        key = resp.json().get("key") or resp.json().get("testExecIssue", {}).get("key")
        log.info("Xray test execution %s created", key)
        return key

    # ------------------------------------------------------------- zephyr
    def _publish_zephyr(self, linked: list[dict[str, Any]]) -> None:
        if self.cfg.deployment != "cloud":
            raise NotImplementedError("Zephyr Scale publishing is implemented for Cloud; DC needs the ATM API")
        url = "https://api.zephyrscale.smartbear.com/v2/testexecutions"
        for r in linked:
            for key in r["jira_keys"]:
                payload = {"projectKey": self.cfg.project_key, "testCaseKey": key,
                           "testCycleKey": self.cfg.zephyr_cycle_key,
                           "statusName": "Pass" if r["outcome"] == "passed" else "Fail",
                           "comment": masker.mask((r.get("error") or "Automated run passed")[:2000]),
                           "executionTime": int((r.get("duration_s") or 0) * 1000)}
                if isinstance(self.jira, RecordingJira):
                    self.jira.post_external(url, payload)
                    continue
                token = self.secrets.get(self.cfg.zephyr_token_secret or "")
                requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"},
                              timeout=60).raise_for_status()

    # ------------------------------------------------------------ defects
    def _raise_or_update_defect(self, r: dict[str, Any]) -> dict[str, Any]:
        sig = failure_signature(r)
        d = self.cfg.defect
        jql = f'project = "{self.cfg.project_key}" AND labels = "{sig}" AND statusCategory != Done'
        existing = self.jira.search(jql)
        if existing:
            key = existing[0]["key"]
            self.jira.add_comment(key, f"Reproduced by automated run {r['run_id']} in "
                                       f"{self.settings.env.upper()}.\n" + self._result_comment(r))
            log.info("Failure matches open defect %s (%s) - commented, no duplicate raised", key, sig)
            return {"action": "commented", "key": key, "signature": sig, "test": r["nodeid"]}

        first_err = (r.get("error") or "failure").strip().splitlines()[0]
        first_err = re.sub(r"^(?:[\w]+\.)+(?=\w+(?:Error|Exception)\b)", "", first_err)[:120]
        steps = "\n".join(f"# {s['name']}{' *(failed here)*' if s['status'] == 'failed' else ''}"
                          for s in r.get("steps", []) if s["status"] != "info")
        description = "\n".join([
            "h3. Raised automatically by qaforge",
            f"*Environment:* {self.settings.env.upper()}   *Run:* {r['run_id']}",
            f"*Test:* {{{{{r['nodeid']}}}}}",
            f"*Linked test case(s):* {', '.join(r.get('jira_keys') or []) or 'none'}",
            "h4. Steps executed", steps or "_no steps recorded_",
            "h4. Error", "{noformat}", masker.mask(r.get("error") or "")[:3000], "{noformat}",
            "h4. Evidence", f"Evidence pack attached (SHA-256 {r.get('evidence_zip_sha256', 'n/a')}).",
            f"Signature: {sig} (used to avoid duplicate defects)",
        ])
        fields: dict[str, Any] = {
            "project": {"key": self.cfg.project_key},
            "issuetype": {"name": d.issue_type},
            "summary": masker.mask(f"[Automation][{self.settings.env.upper()}] {r['test_name']}: {first_err}")[:250],
            "description": description,
            "labels": [*d.labels, sig],
        }
        if d.priority:
            fields["priority"] = {"name": d.priority}
        if d.components:
            fields["components"] = [{"name": c} for c in d.components]
        key = self.jira.create_issue(fields)
        zip_path = self._zip_ok(r)
        if zip_path:
            self.jira.add_attachment(key, zip_path)
        for tc in r.get("jira_keys") or []:
            self.jira.link(d.link_type, tc, key)
        log.info("Raised defect %s for %s (%s)", key, r["nodeid"], sig)
        return {"action": "created", "key": key, "signature": sig, "test": r["nodeid"]}
