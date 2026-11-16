"""Browser jobs started by QA Forge Studio (each runs as its own process).

    python -m qaforge.nocode.browser_tools scan   --env local --app retail --role maker --path /retail/transfer --out DIR
    python -m qaforge.nocode.browser_tools pick   --env local --app retail --role maker --path / --out DIR   (visible browser)
    python -m qaforge.nocode.browser_tools check  --env local --app retail --role maker --pages a.BPage,a.CPage --out DIR
    python -m qaforge.nocode.browser_tools record --env local --app retail --role maker --path / --out DIR    (visible browser)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qaforge.auth.session_store import SessionStore
from qaforge.compliance.evidence import EvidencePack, RunInfo
from qaforge.compliance.logger import configure_logging
from qaforge.config.secrets import build_secret_provider
from qaforge.config.settings import Settings, load_settings
from qaforge.core.base_page import BaseComponent
from qaforge.core.browser import BrowserManager
from qaforge.core.locator_chain import LocatorChain
from qaforge.core.session import AppSession
from qaforge.nocode import registry


def say(msg: str) -> None:
    print(msg, flush=True)


def _settings(env: str, root: Path, headed: bool) -> Settings:
    return load_settings(env=env, root_dir=root, cli_overrides={
        "browser": {"headless": not headed},
        "evidence": {"screenshot": "off", "trace": "off", "video": "off"}})


@contextmanager
def session_for(settings: Settings, app: str, role: str | None) -> Iterator[AppSession]:
    tmp = Path(tempfile.mkdtemp(prefix="qaf-tool-"))
    configure_logging(tmp, "WARNING", console=False)
    run = RunInfo.create("studio-tool", settings.env, Path(settings.root_dir), {})
    pack = EvidencePack(run, tmp, "studio::tool", "studio tool", [], [], "off", False)
    browsers = BrowserManager(settings)
    session = None
    try:
        say(f"Opening {app}" + (f" as {role}" if role else "") + f" on {settings.env}...")
        session = AppSession.open(app, role, settings, browsers, pack, build_secret_provider(settings))
        say("Signed in." if role else "Opened.")
        yield session
    finally:
        if session is not None:
            try:
                session.close(failed=False)
            except Exception:  # noqa: BLE001
                pass
        browsers.stop()


def _goto(session: AppSession, path: str | None, url: str | None) -> None:
    target = url or (session.base_url.rstrip("/") + "/" + (path or "/").lstrip("/"))
    say(f"Opening {target}")
    session.page.goto(target, wait_until="domcontentloaded")


def cmd_scan(a: argparse.Namespace, root: Path) -> int:
    from qaforge.nocode.spy import scan_page

    s = _settings(a.env, root, headed=False)
    known = registry.locator_index(registry.read_pages(root))
    with session_for(s, a.app, a.role or None) as session:
        _goto(session, a.path, a.url)
        say("Scanning the page and checking every locator with Playwright...")
        data = scan_page(session.page, Path(a.out), known, s.evidence.mask_selectors)
    st = data["stats"]
    say(f"Done: {st['elements']} elements, {st['unique']} with a unique locator, {st['fragile']} fragile, "
        f"{st['already_in_pom']} already in page objects.")
    return 0


def cmd_pick(a: argparse.Namespace, root: Path) -> int:
    from qaforge.nocode.spy import pick_session

    s = _settings(a.env, root, headed=True)
    known = registry.locator_index(registry.read_pages(root))
    with session_for(s, a.app, a.role or None) as session:
        _goto(session, a.path, a.url)
        say("A browser window is open. Use the toolbar at the bottom right: Pick element, Scan this page, Finish.")
        data = pick_session(session.page, Path(a.out), known, s.evidence.mask_selectors, log=say)
    say(f"Captured {data['stats']['elements']} elements.")
    return 0


def cmd_check(a: argparse.Namespace, root: Path) -> int:
    from playwright.sync_api import Locator

    s = _settings(a.env, root, headed=False)
    pages = registry.read_pages(root)
    wanted = [p for p in pages if p.ref in set(a.pages.split(","))] if a.pages else [p for p in pages if p.app == a.page_app]
    report: list[dict[str, Any]] = []
    with session_for(s, a.app, a.role or None) as session:
        for info in wanted:
            entry: dict[str, Any] = {"ref": info.ref, "title": info.title, "path": info.path, "elements": []}
            if not info.path:
                entry["skipped"] = "This page has no address - it is reached by a journey, so it can't be opened directly."
                report.append(entry)
                say(f"{info.title}: skipped (no address)")
                continue
            try:
                cls = registry.load_page_class(info.ref, root)
                po = cls(session)
                session.page.goto(po.url, wait_until="domcontentloaded")
                try:
                    po.wait_until_loaded()
                    entry["loaded"] = True
                except Exception as exc:  # noqa: BLE001
                    entry["loaded"] = False
                    entry["load_error"] = str(exc).split("\n")[0][:300]
                for el in info.elements:
                    val = getattr(po, el.name, None)
                    loc = val.root if isinstance(val, BaseComponent) else val
                    if isinstance(loc, LocatorChain):
                        counts = [c.count() for c in loc.candidates]
                        n, primary = counts[0], loc.primary
                        backups = [{"locator": lx, "count": cx} for lx, cx in zip(el.fallbacks, counts[1:])]
                    elif isinstance(loc, Locator):
                        n, primary, backups = loc.count(), loc, []
                    else:
                        continue
                    visible = n == 1 and primary.is_visible()
                    status = "ok" if n == 1 else ("not_found" if n == 0 else "ambiguous")
                    if status != "ok" and any(b["count"] == 1 for b in backups):
                        status = "fallback_only"
                    entry["elements"].append({"name": el.name, "locator": el.locator, "count": n,
                                              "visible": visible, "status": status, "fallbacks": backups})
                bad = [e for e in entry["elements"] if e["status"] != "ok"]
                say(f"{info.title}: {len(entry['elements']) - len(bad)} ok, {len(bad)} need attention")
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc).split("\n")[0][:300]
                say(f"{info.title}: could not check ({entry['error']})")
            report.append(entry)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "result.json").write_text(json.dumps({"env": a.env, "app": a.app, "role": a.role,
                                                         "pages": report}, indent=2), encoding="utf-8")
    return 0


def cmd_record(a: argparse.Namespace, root: Path) -> int:
    from qaforge.nocode.codegen_import import import_recording

    s = _settings(a.env, root, headed=False)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    state_file = None
    if a.role:
        with session_for(s, a.app, a.role):
            pass  # signs in (or reuses the cached Okta session) and stores it
        state_file = SessionStore(s)._file(a.app, a.role)
    base = s.app(a.app).base_url
    url = a.url or base.rstrip("/") + "/" + (a.path or "/").lstrip("/")
    script = out / "recording.py"
    cmd = [sys.executable, "-m", "playwright", "codegen", "--target", "python-pytest", "-o", str(script),
           "--viewport-size", f"{s.browser.viewport_width},{s.browser.viewport_height}",
           "--lang", s.browser.locale, "--timezone", s.browser.timezone_id]
    if s.browser.channel:
        cmd += ["--channel", s.browser.channel]
    if state_file and state_file.exists():
        cmd += ["--load-storage", str(state_file)]
    cmd.append(url)
    say("The recorder window is opening. Click through the journey, then close the window to finish.")
    proc = subprocess.run(cmd, check=False)  # noqa: S603 - fixed command built from validated config
    if not script.exists():
        say("No recording was saved (the recorder needs a desktop session to show its window).")
        return proc.returncode or 1
    known = registry.locator_index(registry.read_pages(root))
    res = import_recording(script.read_text(encoding="utf-8"), base, known, app=a.app, role=a.role or None)
    (out / "result.json").write_text(json.dumps({"steps": res.steps, "warnings": res.warnings,
                                                 "reused": res.reused, "inline": res.inline}, indent=2),
                                     encoding="utf-8")
    say(f"Recorded {len(res.steps)} steps: {res.reused} use page-object elements, {res.inline} new locators.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qaforge.nocode.browser_tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "pick", "check", "record"):
        p = sub.add_parser(name)
        p.add_argument("--env", required=True)
        p.add_argument("--app", required=True)
        p.add_argument("--role", default="")
        p.add_argument("--path", default="/")
        p.add_argument("--url", default=None)
        p.add_argument("--out", required=True)
        p.add_argument("--pages", default="")
        p.add_argument("--page-app", default="")
    a = ap.parse_args(argv)
    root = Path.cwd()
    return {"scan": cmd_scan, "pick": cmd_pick, "check": cmd_check, "record": cmd_record}[a.cmd](a, root)


if __name__ == "__main__":
    sys.exit(main())
