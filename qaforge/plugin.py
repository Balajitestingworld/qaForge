"""pytest plugin - wires the framework into every test automatically.

Enable in a test repo's root conftest.py:
    pytest_plugins = ["qaforge.plugin"]

Command line:
    --env sit             which config/environments/<env>.yaml to load (or QA_ENV)
    --headed              show the browser
    --qa-browser firefox  chromium | firefox | webkit
    --jira off|dry-run|on override JIRA publishing for this run
    --data-seed 1234      reproduce the exact test data of an earlier run

Fixtures for tests:  settings, secrets, open_app, ctx, events, verify, ledger, evidence, data_gen
"""
from __future__ import annotations

import functools
import hashlib
import os
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from qaforge.compliance.evidence import EvidencePack, RunInfo, new_run_id, write_run_summary
from qaforge.compliance.logger import bind, capture_test_log, configure_logging, get_logger, unbind, utc_now_iso
from qaforge.compliance.masker import masker
from qaforge.config.secrets import SecretProvider, build_secret_provider
from qaforge.config.settings import Settings, load_settings
from qaforge.core.browser import BrowserManager
from qaforge.core.session import AppSession
from qaforge.data.context import ScenarioContext
from qaforge.data.factories import use_generator
from qaforge.data.generators import DataGenerator
from qaforge.data.ledger import RunLedger
from qaforge.verifiers.compare import verify as _verify
from qaforge.verifiers.events import EventProbe, EventSource, build_event_source

log = get_logger("plugin")
_REPORTS = pytest.StashKey[dict[str, pytest.TestReport]]()


@dataclass
class QAState:
    settings: Settings
    run_id: str
    run_dir: Path
    run_info: RunInfo
    secrets: SecretProvider
    is_worker: bool


_STATE = pytest.StashKey[QAState]()


# ----------------------------------------------------------------------------- options
def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("qaforge")
    g.addoption("--env", action="store", default=None, help="Environment config to load (default: QA_ENV or local)")
    g.addoption("--headed", action="store_true", default=False, help="Run browsers headed")
    g.addoption("--qa-browser", action="store", default=None, choices=["chromium", "firefox", "webkit"])
    g.addoption("--jira", action="store", default=None, choices=["off", "dry-run", "on"],
                help="Override JIRA publishing for this run")
    g.addoption("--data-seed", action="store", type=int, default=None, help="Fixed test-data seed")
    g.addoption("--qaf-catalog", action="store", default=None,
                help="With --collect-only: write the collected tests (ids, markers, JIRA keys) as JSON")


def _cli_overrides(config: pytest.Config) -> dict[str, Any]:
    o: dict[str, Any] = {}
    if config.getoption("--headed"):
        o.setdefault("browser", {})["headless"] = False
    if config.getoption("--qa-browser"):
        o.setdefault("browser", {})["name"] = config.getoption("--qa-browser")
    jira = config.getoption("--jira")
    if jira == "off":
        o["jira"] = {"enabled": False}
    elif jira == "dry-run":
        o["jira"] = {"enabled": True, "dry_run": True}
    elif jira == "on":
        o["jira"] = {"enabled": True, "dry_run": False}
    if config.getoption("--data-seed") is not None:
        o.setdefault("data", {})["seed"] = config.getoption("--data-seed")
    return o


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "jira(*keys): link the test to JIRA test case keys")
    config.addinivalue_line("markers", "unit: framework self-test (no evidence pack)")
    is_worker = hasattr(config, "workerinput")
    run_id = (config.workerinput["qaf_run_id"] if is_worker  # type: ignore[attr-defined]
              else os.environ.get("QAF_RUN_ID") or new_run_id())
    settings = load_settings(env=config.getoption("--env"), root_dir=config.rootpath,
                             cli_overrides=_cli_overrides(config))
    run_dir = settings.path(settings.evidence.output_dir) / run_id
    # console output is left to pytest (shown on failure, or live with --log-cli-level=INFO)
    if not config.option.collectonly:  # listing tests must not create evidence folders
        configure_logging(run_dir, settings.log_level, console=False)
    secrets = build_secret_provider(settings)
    run_info = RunInfo.create(run_id, settings.env, Path(settings.root_dir),
                              {k: v.base_url for k, v in settings.apps.items()})
    run_info.browser = settings.browser.channel or settings.browser.name
    config.stash[_STATE] = QAState(settings, run_id, run_dir, run_info, secrets, is_worker)
    if not is_worker:
        log.info("Run %s started on env %s -> evidence in %s", run_id, settings.env, run_dir)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Machine-readable test catalog used by QA Forge Studio."""
    target = session.config.getoption("--qaf-catalog")
    if not target:
        return
    import json

    items = []
    for item in session.items:
        fn = getattr(item, "function", None)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn is not None and fn.__doc__ else ""
        callspec = getattr(item, "callspec", None)
        spec_case = callspec.params.get("spec_case") if callspec is not None else None
        items.append({
            "nodeid": item.nodeid,
            "name": item.name,
            "file": item.nodeid.split("::")[0],
            "markers": sorted({m.name for m in item.iter_markers()} - {"jira", "parametrize", "usefixtures"}),
            "jira": [k for m in item.iter_markers("jira") for k in m.args],
            "doc": doc,
            "kind": "nocode" if spec_case is not None else "code",
            "spec_id": spec_case[0] if spec_case is not None else None,
        })
    Path(target).write_text(json.dumps(items, indent=2), encoding="utf-8")


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:  # pytest-xdist: share one run id across workers
    node.workerinput["qaf_run_id"] = node.config.stash[_STATE].run_id


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    rep: pytest.TestReport = outcome.get_result()
    item.stash.setdefault(_REPORTS, {})[rep.when] = rep


def _outcome(item: pytest.Item) -> tuple[str, str | None, str | None]:
    reps = item.stash.get(_REPORTS, {})
    setup, call = reps.get("setup"), reps.get("call")
    for rep, failed_label in ((setup, "error"), (call, "failed")):
        if rep is None:
            continue
        if rep.failed:
            crash = getattr(rep.longrepr, "reprcrash", None)
            short = crash.message if crash is not None else str(rep.longrepr)[:2000]
            return failed_label, short, rep.longreprtext
        if rep.skipped:
            return "skipped", str(rep.longrepr)[-500:] if rep.longrepr else None, None
    return ("passed", None, None) if call is not None else ("error", "test did not run", None)


def _state(request: pytest.FixtureRequest) -> QAState:
    return request.config.stash[_STATE]


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def settings(request: pytest.FixtureRequest) -> Settings:
    return _state(request).settings


@pytest.fixture(scope="session")
def secrets(request: pytest.FixtureRequest) -> SecretProvider:
    return _state(request).secrets


@pytest.fixture(scope="session")
def run_info(request: pytest.FixtureRequest) -> RunInfo:
    return _state(request).run_info


@pytest.fixture(scope="session")
def browsers(settings: Settings, run_info: RunInfo) -> Iterator[BrowserManager]:
    mgr = BrowserManager(settings)
    yield mgr
    mgr.stop()


@pytest.fixture(scope="session")
def ledger(request: pytest.FixtureRequest, settings: Settings) -> RunLedger:
    st = _state(request)
    return RunLedger(settings.path(settings.data.ledger_path), settings.env, st.run_id)


@pytest.fixture(scope="session")
def event_source(settings: Settings, secrets: SecretProvider) -> Iterator[EventSource]:
    src = build_event_source(settings, secrets)
    yield src
    src.close()


@pytest.fixture(autouse=True)
def evidence(request: pytest.FixtureRequest) -> Iterator[EvidencePack]:
    st = _state(request)
    item = request.node
    if item.get_closest_marker("unit"):  # framework self-tests: no audit pack needed
        import tempfile

        with tempfile.TemporaryDirectory(prefix="qaf-unit-") as tmp:
            yield EvidencePack(st.run_info, Path(tmp), item.nodeid, item.name, [], ["unit"], "off", False)
        return
    jira_keys = [k for m in item.iter_markers("jira") for k in m.args]
    markers = sorted({m.name for m in item.iter_markers()} - {"jira", "parametrize", "usefixtures"})
    pack = EvidencePack(st.run_info, st.run_dir, item.nodeid, item.name, jira_keys, markers,
                        st.settings.evidence.screenshot, st.settings.evidence.zip_pack)
    bind(test_id=item.nodeid, test_short=item.name, jira=",".join(jira_keys) or None)
    capture = capture_test_log()
    with capture:
        log.info("TEST START %s", item.nodeid)
        yield pack
        outcome, short, full = _outcome(item)
        log.info("TEST END %s -> %s", item.nodeid, outcome.upper())
    if full:
        (pack.dir / "failure_traceback.txt").write_text(masker.mask(full), encoding="utf-8")
        pack.attachments.append({"kind": "traceback", "file": "failure_traceback.txt"})
    try:
        pack.finalize(outcome, short, capture)
    except Exception:  # noqa: BLE001 - never lose the test result because of reporting
        log.error("Could not seal evidence for %s:\n%s", item.nodeid, traceback.format_exc())
    unbind("test_id", "test_short", "jira", "app", "step")


@pytest.fixture(autouse=True)
def data_gen(request: pytest.FixtureRequest, settings: Settings, evidence: EvidencePack) -> DataGenerator:
    st = _state(request)
    seed = settings.data.seed
    if seed is None:
        seed = int(hashlib.sha256(f"{st.run_id}|{request.node.nodeid}".encode()).hexdigest()[:12], 16)
    gen = DataGenerator(settings.data, seed=seed, run_tag=st.run_id[-6:])
    use_generator(gen)
    evidence.notes.append(f"Test data seed: {seed} (re-run with --data-seed {seed} to reproduce)")
    return gen


@pytest.fixture
def ctx(request: pytest.FixtureRequest, ledger: RunLedger, evidence: EvidencePack) -> Iterator[ScenarioContext]:
    context = ScenarioContext(request.node.nodeid, ledger)
    yield context
    evidence.set_data_used(context.snapshot())


@pytest.fixture
def open_app(request: pytest.FixtureRequest, settings: Settings, browsers: BrowserManager,
             evidence: EvidencePack, secrets: SecretProvider, run_info: RunInfo
             ) -> Iterator[Callable[..., AppSession]]:
    """open_app("retail", role="maker") -> signed-in, audited AppSession (closed automatically)."""
    sessions: list[AppSession] = []

    def _open(name: str, role: str | None = None) -> AppSession:
        session = AppSession.open(name, role, settings, browsers, evidence, secrets)
        run_info.browser_version = browsers.browser.version
        sessions.append(session)
        return session

    yield _open
    failed = _outcome(request.node)[0] in ("failed", "error")
    for s in sessions:
        if failed:
            shot = s.act.screenshot(f"final_state_{s.name}")
            evidence.add_step(f"Final screen of {s.label} when the test failed", "info", utc_now_iso(), 0,
                              app=s.label, screenshot=shot)
        try:
            s.close(failed)
        except Exception as exc:  # noqa: BLE001
            log.warning("Closing session %s failed: %s", s.label, exc)


@pytest.fixture
def events(event_source: EventSource, settings: Settings, evidence: EvidencePack,
           ctx: ScenarioContext) -> EventProbe:
    return EventProbe(event_source, settings, evidence, ctx, test_started_ms=int(time.time() * 1000))


@pytest.fixture
def verify(ctx: ScenarioContext, evidence: EvidencePack) -> Callable[..., Any]:
    """verify(actual_dict, mapping, source="api:accounts") with context + evidence pre-bound."""
    return functools.partial(_verify, ctx=ctx, evidence=evidence)


# ----------------------------------------------------------------------------- session end
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    st = session.config.stash.get(_STATE, None)
    if st is None or st.is_worker or not (st.run_dir / "tests").exists():
        return
    summary = write_run_summary(st.run_dir, st.run_info)
    if st.settings.jira.enabled:
        from qaforge.integrations.jira.publisher import ResultsPublisher

        try:
            ResultsPublisher(st.settings, st.secrets, st.run_dir).publish(summary["results"])
        except Exception as exc:  # noqa: BLE001
            log.error("JIRA publishing failed: %s", exc)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    st = config.stash.get(_STATE, None)
    if st is None or st.is_worker:
        return
    index = st.run_dir / "index.html"
    if index.exists():
        terminalreporter.write_sep("-", "qaforge evidence")
        terminalreporter.write_line(f"Run report : {index}")
        plan = st.run_dir / "jira_publish_plan.json"
        if plan.exists():
            terminalreporter.write_line(f"JIRA plan  : {plan} (dry run - nothing sent)")
