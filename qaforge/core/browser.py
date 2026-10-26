"""Browser lifecycle: one browser per worker process, one isolated context per app session."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

from qaforge.compliance.logger import get_logger
from qaforge.config.settings import Settings

log = get_logger("browser")


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            self.start()
        assert self._browser is not None
        return self._browser

    @property
    def playwright(self) -> Playwright:
        if self._pw is None:
            self.start()
        assert self._pw is not None
        return self._pw

    def start(self) -> None:
        cfg = self.settings.browser
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, cfg.name)
        launch_args: dict[str, Any] = {"headless": cfg.headless, "slow_mo": cfg.slow_mo_ms, "args": cfg.extra_args}
        if cfg.channel:
            launch_args["channel"] = cfg.channel
        if cfg.proxy_server:
            launch_args["proxy"] = {"server": cfg.proxy_server}
        self._browser = launcher.launch(**launch_args)
        log.info("Launched %s %s (headless=%s)", cfg.name, self._browser.version, cfg.headless)

    def new_context(self, storage_state: dict[str, Any] | None, video_dir: Path | None,
                    har_path: Path | None = None) -> BrowserContext:
        cfg = self.settings.browser
        if cfg.ignore_https_errors and self.settings.is_production:
            raise RuntimeError("ignore_https_errors is not permitted in production-like environments")
        opts: dict[str, Any] = {
            "viewport": {"width": cfg.viewport_width, "height": cfg.viewport_height},
            "locale": cfg.locale,
            "timezone_id": cfg.timezone_id,
            "ignore_https_errors": cfg.ignore_https_errors,
            "accept_downloads": True,
            "storage_state": storage_state,
        }
        if video_dir is not None:
            opts["record_video_dir"] = str(video_dir)
            opts["record_video_size"] = {"width": cfg.viewport_width, "height": cfg.viewport_height}
        if har_path is not None:
            opts["record_har_path"] = str(har_path)
            opts["record_har_content"] = "omit"
        return self.browser.new_context(**opts)

    def stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._browser, self._pw = None, None
