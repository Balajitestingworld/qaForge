"""Self-tests for QA Forge Studio: first admin, sign-in, CSRF, roles, audit, spec saving."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from qaforge.studio.app import create_app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path):
    app = create_app(ROOT, data_dir=tmp_path / "studio_data")
    app.testing = True
    c = app.test_client()
    yield c
    shutil.rmtree(tmp_path, ignore_errors=True)


def _csrf(c, url):
    html = c.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login(c, user="ada.admin", pw="Adm1nPassword!", create=True):
    if create:
        c.post("/setup", data={"username": user, "password": pw, "display": "Ada", "csrf": _csrf(c, "/setup")},
               environ_base={"REMOTE_ADDR": "127.0.0.1"})
    return c.post("/login", data={"username": user, "password": pw, "csrf": _csrf(c, "/login")})


def test_first_admin_then_login_and_audit(client, tmp_path):
    assert client.get("/").status_code == 302
    r = _login(client)
    assert r.status_code == 302 and client.get("/").status_code == 200
    audit = (tmp_path / "studio_data" / "audit.log.jsonl").read_text()
    assert "user.create_first_admin" in audit and "login.ok" in audit


def test_post_without_csrf_is_rejected(client):
    _login(client)
    assert client.post("/tests/run", data={"nodeid": "x"}).status_code == 400


def test_wrong_password_locks_after_five_attempts(client):
    _login(client)
    client.post("/logout", data={"csrf": _csrf(client, "/")})
    for _ in range(5):
        _login(client, pw="Wrong-password-1", create=False)
    r = _login(client, create=False)  # right password, but locked
    assert "locked" in r.get_data(as_text=True)


def test_spec_save_validates_and_reports_step_numbers(client):
    _login(client)
    token = _csrf(client, "/specs/new")
    bad = {"_new": True, "id": "studio_tmp_spec", "title": "Tmp", "steps": [{"action": "click"}]}
    r = client.post("/api/specs/studio_tmp_spec", json=bad, headers={"X-CSRF-Token": token})
    assert r.status_code == 400 and r.get_json()["errors"][0]["step"] == 0
    good = {"_new": True, "id": "studio_tmp_spec", "title": "Tmp", "steps": [{"action": "note", "value": "hi"}]}
    try:
        r = client.post("/api/specs/studio_tmp_spec", json=good, headers={"X-CSRF-Token": token})
        assert r.get_json()["ok"] is True
        assert (ROOT / "tests/nocode/specs/studio_tmp_spec.yaml").exists()
    finally:
        (ROOT / "tests/nocode/specs/studio_tmp_spec.yaml").unlink(missing_ok=True)
