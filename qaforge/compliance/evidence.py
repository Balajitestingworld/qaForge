"""Audit evidence: one self-contained, tamper-evident pack per test.

Layout of a run:
    evidence/<run_id>/
        run.json            run metadata (who, where, what build, which versions)
        run.log.jsonl       whole-run structured log
        run_summary.json    results of every test
        index.html          run dashboard linking to every test report
        tests/<test>/
            report.html     human-readable evidence report (printable to PDF)
            test.json       machine-readable record (steps, verifications, data used)
            test.log.jsonl  this test's log lines
            screenshots/    masked step screenshots
            trace-*.zip     Playwright traces (open with `playwright show-trace`)
            manifest.json   SHA-256 of every file + a hash over the manifest itself
        tests/<test>.zip    the sealed pack (optional, config evidence.zip_pack)

`verify_pack()` recomputes every hash so an auditor can prove nothing changed after the run.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from qaforge import __version__
from qaforge.compliance.logger import get_logger, utc_now_iso
from qaforge.compliance.masker import masker

log = get_logger("evidence")
_TEMPLATES = Path(__file__).parent / "templates"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(nodeid: str, max_len: int = 80) -> str:
    base = nodeid.split("/")[-1].replace(".py::", "__").replace("::", "__")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")
    digest = hashlib.sha1(nodeid.encode()).hexdigest()[:8]
    return f"{base[:max_len]}_{digest}"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603,S607 - fixed command, no user input
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _ci_build() -> str | None:
    for var in ("BUILD_URL", "CI_PIPELINE_URL", "BUILD_BUILDID", "GITHUB_RUN_ID", "BUILD_NUMBER", "CI_JOB_ID"):
        if os.environ.get(var):
            return f"{var}={os.environ[var]}"
    return None


@dataclass
class RunInfo:
    run_id: str
    env: str
    started_at: str
    triggered_by: str
    host: str
    framework_version: str
    python_version: str
    os: str
    git_commit: str | None
    ci_build: str | None
    playwright_version: str | None = None
    browser: str | None = None
    browser_version: str | None = None
    apps: dict[str, str] = field(default_factory=dict)
    finished_at: str | None = None

    @classmethod
    def create(cls, run_id: str, env: str, root: Path, apps: dict[str, str]) -> RunInfo:
        try:
            from importlib.metadata import version

            pw = version("playwright")
        except Exception:  # noqa: BLE001
            pw = None
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = "unknown"
        return cls(
            run_id=run_id,
            env=env,
            started_at=utc_now_iso(),
            triggered_by=os.environ.get("QA_TRIGGERED_BY", user),
            host=socket.gethostname(),
            framework_version=__version__,
            python_version=platform.python_version(),
            os=f"{platform.system()} {platform.release()}",
            git_commit=_git_commit(root),
            ci_build=_ci_build(),
            playwright_version=pw,
            apps=apps,
        )


@dataclass
class StepRecord:
    index: int
    name: str
    status: str  # passed | failed | info
    started_at: str
    duration_ms: int
    app: str | None = None
    detail: str | None = None
    screenshot: str | None = None
    error: str | None = None


@dataclass
class VerificationRecord:
    name: str
    source: str
    passed: bool
    rows: list[dict[str, Any]]
    started_at: str = field(default_factory=utc_now_iso)


class EvidencePack:
    def __init__(
        self,
        run: RunInfo,
        run_dir: Path,
        nodeid: str,
        test_name: str,
        jira_keys: list[str],
        markers: list[str],
        screenshot_policy: str,
        zip_pack: bool,
    ) -> None:
        self.run = run
        self.nodeid = nodeid
        self.test_name = test_name
        self.jira_keys = jira_keys
        self.markers = markers
        self.screenshot_policy = screenshot_policy
        self.zip_pack = zip_pack
        self.folder_name = safe_name(nodeid)
        self.dir = run_dir / "tests" / self.folder_name
        self.screens_dir = self.dir / "screenshots"
        self.screens_dir.mkdir(parents=True, exist_ok=True)
        self.steps: list[StepRecord] = []
        self.verifications: list[VerificationRecord] = []
        self.attachments: list[dict[str, str]] = []
        self.data_used: dict[str, Any] = {}
        self.notes: list[str] = []
        self.locator_fallbacks: list[dict[str, Any]] = []
        self.started_at = utc_now_iso()
        self._t0 = time.monotonic()
        self._shot_seq = 0

    # ---------------------------------------------------------------- steps
    def next_screenshot_path(self, label: str) -> Path:
        self._shot_seq += 1
        clean = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:50].strip("_") or "shot"
        return self.screens_dir / f"{self._shot_seq:03d}_{clean}.png"

    def add_step(
        self,
        name: str,
        status: str,
        started_at: str,
        duration_ms: int,
        app: str | None = None,
        detail: str | None = None,
        screenshot: Path | None = None,
        error: str | None = None,
    ) -> StepRecord:
        rec = StepRecord(
            index=len(self.steps) + 1,
            name=masker.mask(name),
            status=status,
            started_at=started_at,
            duration_ms=duration_ms,
            app=app,
            detail=masker.mask(detail) if detail else None,
            screenshot=str(screenshot.relative_to(self.dir)) if screenshot else None,
            error=masker.mask(error) if error else None,
        )
        self.steps.append(rec)
        return rec

    def note(self, text: str) -> None:
        self.notes.append(masker.mask(text))
        self.add_step(text, "info", utc_now_iso(), 0)

    # --------------------------------------------------------- attachments
    def attach_file(self, src: Path, kind: str, name: str | None = None) -> Path | None:
        if not src.exists():
            return None
        dest = self.dir / (name or src.name)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        self.attachments.append({"kind": kind, "file": dest.name})
        return dest

    def register_attachment(self, path: Path, kind: str) -> None:
        if path.exists():
            self.attachments.append({"kind": kind, "file": str(path.relative_to(self.dir))})

    def attach_json(self, name: str, payload: Any, kind: str = "data") -> Path:
        dest = self.dir / name
        dest.write_text(json.dumps(masker.mask_obj(payload), indent=2, ensure_ascii=False), encoding="utf-8")
        self.attachments.append({"kind": kind, "file": dest.name})
        return dest

    def add_verification(self, rec: VerificationRecord) -> None:
        rec.rows = [masker.mask_obj(r) for r in rec.rows]
        self.verifications.append(rec)

    def set_data_used(self, data: dict[str, Any]) -> None:
        self.data_used = masker.mask_obj(data)

    # ------------------------------------------------------------ finalize
    def finalize(self, outcome: str, error: str | None, log_lines_writer: Any | None = None) -> dict[str, Any]:
        duration_s = round(time.monotonic() - self._t0, 2)
        if self.screenshot_policy == "on_failure" and outcome == "passed":
            # policy says keep screenshots for failures only
            for shot in self.screens_dir.glob("*.png"):
                shot.unlink()
            for s in self.steps:
                s.screenshot = None

        record = {
            "run_id": self.run.run_id,
            "env": self.run.env,
            "nodeid": self.nodeid,
            "test_name": self.test_name,
            "jira_keys": self.jira_keys,
            "markers": self.markers,
            "outcome": outcome,
            "error": masker.mask(error) if error else None,
            "started_at": self.started_at,
            "finished_at": utc_now_iso(),
            "duration_s": duration_s,
            "steps": [asdict(s) for s in self.steps],
            "verifications": [asdict(v) for v in self.verifications],
            "data_used": self.data_used,
            "attachments": self.attachments,
            "notes": self.notes,
            "locator_fallbacks": self.locator_fallbacks,
            "needs_locator_fix": bool(self.locator_fallbacks),
            "run": asdict(self.run),
            "evidence_dir": str(self.dir),
        }
        if log_lines_writer is not None:
            log_lines_writer.write(self.dir / "test.log.jsonl")
        (self.dir / "test.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        html = _jinja.get_template("test_report.html.j2").render(r=record)
        (self.dir / "report.html").write_text(html, encoding="utf-8")

        manifest = write_manifest(self.dir)
        record["manifest_sha256"] = manifest["manifest_sha256"]
        if self.zip_pack:
            zip_path = self.dir.with_suffix(".zip")
            _zip_dir(self.dir, zip_path)
            record["evidence_zip"] = str(zip_path)
            record["evidence_zip_sha256"] = sha256_file(zip_path)
        # summary line for the run index (outside the sealed folder on purpose)
        (self.dir.parent / f"{self.folder_name}.result.json").write_text(
            json.dumps({k: record[k] for k in _SUMMARY_KEYS if k in record}, indent=2), encoding="utf-8"
        )
        log.info("Evidence sealed: %s (%s)", self.dir.name, outcome)
        return record


_SUMMARY_KEYS = (
    "run_id", "nodeid", "test_name", "jira_keys", "markers", "outcome", "error", "started_at",
    "finished_at", "duration_s", "evidence_dir", "evidence_zip", "evidence_zip_sha256", "manifest_sha256",
    "needs_locator_fix", "locator_fallbacks",
)


def write_manifest(folder: Path) -> dict[str, Any]:
    entries = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            entries.append({"file": str(p.relative_to(folder)).replace(os.sep, "/"),
                            "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    body = {"algorithm": "SHA-256", "generated_at": utc_now_iso(), "files": entries}
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    body["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    return body


def _zip_dir(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(Path(folder.name) / p.relative_to(folder)))


def verify_pack(path: Path) -> tuple[bool, list[str]]:
    """Recompute hashes for an evidence folder or sealed zip. Returns (ok, problems)."""
    problems: list[str] = []
    tmp_dir: Path | None = None
    if path.suffix == ".zip":
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="qaf-verify-"))
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp_dir)  # noqa: S202 - our own archive, verified below
        candidates = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if not candidates:
            return False, ["zip contains no evidence folder"]
        path = candidates[0]
    manifest_file = path / "manifest.json"
    if not manifest_file.exists():
        return False, ["manifest.json missing"]
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    canonical = json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != manifest["manifest_sha256"]:
        problems.append("manifest itself was modified (manifest_sha256 mismatch)")
    listed = set()
    for entry in manifest["files"]:
        f = path / entry["file"]
        listed.add(entry["file"])
        if not f.exists():
            problems.append(f"missing: {entry['file']}")
        elif sha256_file(f) != entry["sha256"]:
            problems.append(f"modified: {entry['file']}")
    for p in path.rglob("*"):
        rel = str(p.relative_to(path)).replace(os.sep, "/")
        if p.is_file() and p.name != "manifest.json" and rel not in listed:
            problems.append(f"unexpected file added: {rel}")
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return not problems, problems


def write_run_summary(run_dir: Path, run: RunInfo) -> dict[str, Any]:
    results = []
    for f in sorted((run_dir / "tests").glob("*.result.json")):
        results.append(json.loads(f.read_text(encoding="utf-8")))
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    run.finished_at = utc_now_iso()
    fallbacks = [{"test": r["nodeid"], **fb} for r in results for fb in r.get("locator_fallbacks") or []]
    summary = {"run": asdict(run), "counts": counts, "total": len(results), "results": results,
               "needs_locator_fix": sum(1 for r in results if r.get("needs_locator_fix"))}
    if fallbacks:
        (run_dir / "locator_fallbacks.json").write_text(json.dumps(fallbacks, indent=2), encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")
    (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for r in results:
        r["report_rel"] = str(Path(r["evidence_dir"]).relative_to(run_dir) / "report.html").replace(os.sep, "/")
    html = _jinja.get_template("run_index.html.j2").render(s=summary)
    (run_dir / "index.html").write_text(html, encoding="utf-8")
    return summary
