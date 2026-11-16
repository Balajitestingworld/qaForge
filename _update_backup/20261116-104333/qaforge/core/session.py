"""AppSession = one application, one role, one isolated browser context, fully audited.

A single test can hold several sessions at once, e.g. a maker in the retail portal and a
checker in the back-office portal, each with its own trace and video in the same evidence pack.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from playwright.sync_api import BrowserContext, Page

from qaforge.auth.okta import OktaLogin
from qaforge.auth.session_store import SessionStore
from qaforge.compliance.evidence import EvidencePack
from qaforge.compliance.logger import bind, get_logger
from qaforge.config.secrets import SecretProvider
from qaforge.config.settings import AppConfig, Settings
from qaforge.core.actions import Actions
from qaforge.core.browser import BrowserManager

log = get_logger("session")


class AppSession:
    def __init__(
        self,
        name: str,
        role: str | None,
        settings: Settings,
        app_config: AppConfig,
        context: BrowserContext,
        page: Page,
        evidence: EvidencePack,
        secrets: SecretProvider,
        video_dir: Path | None,
    ) -> None:
        self.name = name
        self.role = role
        self.settings = settings
        self.app_config = app_config
        self.base_url = app_config.base_url
        self.context = context
        self.page = page
        self.evidence = evidence
        self.secrets = secrets
        self.label = f"{name}:{role}" if role else name
        self.act = Actions(page, evidence, settings, app=self.label)
        self._video_dir = video_dir
        self._closed = False

    @classmethod
    def open(
        cls,
        name: str,
        role: str | None,
        settings: Settings,
        browsers: BrowserManager,
        evidence: EvidencePack,
        secrets: SecretProvider,
    ) -> AppSession:
        app_cfg = settings.app(name)
        store = SessionStore(settings)
        state = store.load(name, role) if (role and app_cfg.auth == "okta") else None

        video_dir = None
        if settings.evidence.video != "off":
            video_dir = evidence.dir / f"_video_tmp_{name}_{role or 'anon'}"
        har = evidence.dir / f"network-{name}.har" if settings.evidence.har else None
        context = browsers.new_context(storage_state=state, video_dir=video_dir, har_path=har)
        if settings.evidence.trace != "off":
            context.tracing.start(screenshots=True, snapshots=True, sources=False, title=f"{name}:{role}")
        page = context.new_page()
        session = cls(name, role, settings, app_cfg, context, page, evidence, secrets, video_dir)
        bind(app=session.label)
        log.info("Opened session %s (cached login: %s)", session.label, state is not None)
        if role and app_cfg.auth == "okta":
            session._ensure_signed_in(store, used_cache=state is not None)
        return session

    # ------------------------------------------------------------------ auth
    def _ensure_signed_in(self, store: SessionStore, used_cache: bool) -> None:
        landing = self.base_url.rstrip("/") + "/" + self.app_config.landing_path.lstrip("/")
        self.act.goto(landing, f"Open {self.name} as {self.role}")
        okta = OktaLogin(self.page, self.act, self.settings, self.secrets)

        needs_login: bool
        if self.app_config.ready_selector:
            ready = self.page.locator(self.app_config.ready_selector).first
            login = self.page.locator(self.settings.okta.selectors.username).first
            ready.or_(login).first.wait_for(state="visible")
            needs_login = not ready.is_visible()
        else:
            self.page.wait_for_load_state("load")
            needs_login = okta.is_login_page()

        if not needs_login:
            return
        if used_cache:
            log.info("Cached session for %s was rejected by the app; signing in again", self.label)
            store.invalidate(self.name, self.role or "")
        okta.login(self.role or "")
        if self.app_config.ready_selector:
            self.act.expect_visible(self.page.locator(self.app_config.ready_selector).first,
                                    f"{self.name} home after sign-in", timeout_ms=30_000)
        else:
            self.page.wait_for_url(lambda u: u.startswith(self.base_url), timeout=30_000)
        store.save(self.name, self.role or "", self.context.storage_state())

    # ----------------------------------------------------------------- close
    def close(self, failed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        ev = self.settings.evidence
        safe = f"{self.name}-{self.role or 'anon'}"
        try:
            if ev.trace == "on" or (ev.trace == "retain_on_failure" and failed):
                trace = self.evidence.dir / f"trace-{safe}.zip"
                self.context.tracing.stop(path=str(trace))
                self.evidence.register_attachment(trace, "playwright-trace")
            elif ev.trace != "off":
                self.context.tracing.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save trace for %s: %s", self.label, exc)
        video = self.page.video
        self.context.close()
        if video is not None and self._video_dir is not None:
            try:
                keep = ev.video == "on" or (ev.video == "retain_on_failure" and failed)
                src = Path(video.path())
                if keep and src.exists():
                    dest = self.evidence.dir / f"video-{safe}.webm"
                    shutil.move(str(src), dest)
                    self.evidence.register_attachment(dest, "video")
            finally:
                shutil.rmtree(self._video_dir, ignore_errors=True)
