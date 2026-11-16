"""Okta SSO sign-in for test users.

Supports:
  * Okta Identity Engine identifier-first flow (username -> Next -> password -> Verify)
  * Classic sign-in widget (username + password on one form)
  * MFA:  totp   - code generated from the user's vaulted TOTP seed (Okta Verify / Google Auth)
          manual - pauses for a human (only for headed, attended debugging runs)
          none   - non-prod policy exempts the automation group from MFA

Recommended setup with your Okta admins (non-prod only):
  1. A dedicated group of automation users per role (maker, checker, admin...).
  2. Either an MFA-exempt sign-on policy for that group, or a TOTP authenticator enrolled
     with the seed stored in the vault. Never automate push or SMS factors.
  3. Password-never-expires or rotation through the vault, and lockout alerts to the QA team.
"""
from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from qaforge.auth.totp import fresh_totp
from qaforge.compliance.logger import get_logger
from qaforge.config.secrets import SecretProvider
from qaforge.config.settings import Settings
from qaforge.core.actions import Actions

log = get_logger("auth.okta")


class OktaLoginError(RuntimeError):
    pass


class OktaLogin:
    def __init__(self, page: Page, act: Actions, settings: Settings, secrets: SecretProvider) -> None:
        self.page = page
        self.act = act
        self.settings = settings
        self.secrets = secrets
        self.sel = settings.okta.selectors

    def is_login_page(self) -> bool:
        domain = self.settings.okta.domain.rstrip("/")
        if domain and self.page.url.startswith(domain):
            return True
        return self.page.locator(self.sel.username).first.is_visible()

    def login(self, role: str) -> None:
        user = self.settings.user(role)
        username = self.secrets.get(user.username_secret)
        password = self.secrets.get(user.password_secret)
        p = self.page

        with self.act.group(f"Okta sign-in as '{role}'"):
            username_box = p.locator(self.sel.username).first
            password_box = p.locator(self.sel.password).first
            self.act.expect_visible(username_box, "Okta username field")
            self.act.fill(username_box, username, "Username")

            if not password_box.is_visible():  # identifier-first: password is on the next screen
                self.act.click(p.locator(self.sel.next_button).first, "Next")
            self.act.expect_visible(password_box, "Okta password field")
            self.act.fill(password_box, password, "Password", sensitive=True)
            self.act.click(p.locator(self.sel.submit_button).first, "Verify password")
            self._raise_on_error("password")

            mfa = self.settings.okta.mfa
            if mfa == "totp":
                self._totp(user.totp_secret, role)
            elif mfa == "manual":
                log.warning("MFA mode 'manual': complete MFA in the browser window (headed run only)")
                p.pause()

    def _totp(self, totp_secret_name: str | None, role: str) -> None:
        if not totp_secret_name:
            raise OktaLoginError(f"okta.mfa=totp but user '{role}' has no totp_secret configured")
        seed = self.secrets.get(totp_secret_name)
        p = self.page
        if self.sel.mfa_factor:
            factor = p.locator(self.sel.mfa_factor).first
            try:
                factor.wait_for(state="visible", timeout=5_000)
                self.act.click(factor, "Choose TOTP authenticator")
            except PlaywrightError:
                log.info("Factor picker not shown; TOTP is the default factor")
        code_box = p.locator(self.sel.totp_input).first
        self.act.expect_visible(code_box, "Okta verification code field")
        self.act.fill(code_box, fresh_totp(seed), "One-time code", sensitive=True)
        self.act.click(p.locator(self.sel.submit_button).first, "Verify code")
        self._raise_on_error("MFA")

    def _raise_on_error(self, stage: str) -> None:
        banner = self.page.locator(self.sel.error_banner).first
        try:
            banner.wait_for(state="visible", timeout=1_500)
        except PlaywrightError:
            return
        text = banner.inner_text().strip()
        if text:
            self.act.screenshot(f"okta_{stage}_error")
            raise OktaLoginError(f"Okta rejected the {stage} step: {text}")
