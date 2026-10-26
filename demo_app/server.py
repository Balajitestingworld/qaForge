"""QA Demo Bank - a tiny, self-contained banking app used to prove the framework end to end.

NOT a real bank and NOT a real identity provider. It mimics the moving parts the framework
must handle in a real estate:
  * an Okta-compatible sign-in (identifier-first, password, TOTP) with the same field names
  * a retail portal (maker): add beneficiary, NEFT/IMPS/RTGS transfer with review + confirm
  * a back-office portal (checker): payments queue grid with approve action
  * an event bus exposed at /api/events standing in for Kafka topics

Run standalone:  python -m demo_app.server --port 8765
"""
from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from qaforge.auth.totp import totp

USERS = {
    "maker.demo@example.test": {"password": "Maker#Pass1", "totp": "JBSWY3DPEHPK3PXP", "role": "maker",
                                "name": "Asha Maker", "account": "501000123456", "balance": Decimal("985000.00")},
    "checker.demo@example.test": {"password": "Checker#Pass1", "totp": "KRSXG5CTMVRXEZLU", "role": "checker",
                                  "name": "Vikram Checker"},
}
LIMITS = {"NEFT": (Decimal("1.00"), Decimal("200000.00")), "IMPS": (Decimal("1.00"), Decimal("200000.00")),
          "RTGS": (Decimal("200000.00"), Decimal("5000000.00"))}
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, str] = {}
        self.beneficiaries: dict[str, list[dict]] = {}
        self.drafts: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.events: list[dict] = []
        self.offset = 0
        self.seq = 0

    def publish(self, topic: str, key: str, value: dict) -> None:
        with self.lock:
            self.offset += 1
            self.events.append({"topic": topic, "key": key, "partition": 0, "offset": self.offset,
                                "timestamp_ms": int(time.time() * 1000),
                                "headers": {"source": "demo-bank", "eventId": secrets.token_hex(8)}, "value": value})


STATE = State()


def inr(amount: Decimal) -> str:
    q = f"{amount:.2f}"
    whole, frac = q.split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head))
        whole = f"{head},{tail}"
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


CSS = """
body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f3f5f8;color:#1b2333}
header{background:#123a6b;color:#fff;padding:12px 28px;display:flex;justify-content:space-between;align-items:center}
header a{color:#cfe0ff;margin-left:18px;text-decoration:none} header b{font-size:17px}
main{max-width:860px;margin:28px auto;background:#fff;border:1px solid #dde3ec;border-radius:8px;padding:26px 30px}
h1{font-size:21px;margin:0 0 18px} label{display:block;margin:14px 0 4px;font-weight:600;font-size:13px}
input,select{width:100%;padding:9px 10px;border:1px solid #b9c3d2;border-radius:5px;font-size:15px;box-sizing:border-box}
button,input[type=submit]{margin-top:20px;background:#123a6b;color:#fff;border:0;padding:10px 20px;border-radius:5px;
font-size:15px;cursor:pointer;width:auto}
table{width:100%;border-collapse:collapse;margin-top:14px} th,td{border-bottom:1px solid #e3e8ef;padding:8px;text-align:left}
th{background:#eef2f7;font-size:12px;text-transform:uppercase}
.ok{background:#e4f5ea;color:#17663a;padding:10px 12px;border-radius:5px}
.err{background:#fdeaea;color:#a31d1d;padding:10px 12px;border-radius:5px}
.kv td:first-child{color:#5b6475;width:180px} .muted{color:#5b6475;font-size:13px}
.idp{max-width:380px} .idp h1{text-align:center}
"""


def page(title: str, body: str, user: str | None = None, portal: str = "") -> bytes:
    nav = ""
    if user and portal == "retail":
        nav = ('<nav><a data-testid="nav-dashboard" href="/retail/dashboard">Dashboard</a>'
               '<a data-testid="nav-add-beneficiary" href="/retail/beneficiaries/new">Add beneficiary</a>'
               '<a data-testid="nav-transfer" href="/retail/transfer">Transfer funds</a>'
               '<a href="/logout">Sign out</a></nav>')
    elif user and portal == "backoffice":
        nav = '<nav><a data-testid="nav-queue" href="/backoffice/payments">Payments queue</a><a href="/logout">Sign out</a></nav>'
    head = (f'<header data-testid="app-header"><b>QA Demo Bank · {portal.title()}</b>'
            f'<span><span data-testid="signed-in-user">{html.escape(user)}</span>{nav}</span></header>') if user else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{head}<main>{body}</main></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "QADemoBank/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # keep test output clean
        pass

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self._send(303, b"", headers={"Location": location, **(headers or {})})

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length", 0))
        data = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        return {k: v[0].strip() for k, v in data.items()}

    def _user(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = cookies.SimpleCookie(raw)
        sid = c.get("demo_sid")
        return STATE.sessions.get(sid.value) if sid else None

    def _require(self, role: str) -> str | None:
        user = self._user()
        if not user or USERS[user]["role"] != role:
            self._redirect(f"/okta/signin?return={quote(self.path)}")
            return None
        return user

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        routes = {
            "/": lambda: self._redirect("/retail/dashboard"),
            "/okta/signin": lambda: self._idp_identifier(q.get("return", "/retail/dashboard")),
            "/logout": self._logout,
            "/retail/dashboard": self._dashboard,
            "/retail/beneficiaries": lambda: self._beneficiaries(q),
            "/retail/beneficiaries/new": lambda: self._beneficiary_form(),
            "/retail/transfer": lambda: self._transfer_form(),
            "/retail/transfer/review": lambda: self._review(q.get("draft", "")),
            "/retail/transfer/success": lambda: self._success(q.get("ref", "")),
            "/backoffice": lambda: self._redirect("/backoffice/payments"),
            "/backoffice/payments": lambda: self._queue(q),
            "/api/events": lambda: self._events(q),
            "/health": lambda: self._send(200, b'{"status":"UP"}', "application/json"),
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._send(404, page("Not found", "<h1>Not found</h1>"))

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        form = self._form()
        routes = {
            "/okta/identify": lambda: self._idp_password(form),
            "/okta/password": lambda: self._idp_check_password(form),
            "/okta/mfa": lambda: self._idp_check_mfa(form),
            "/retail/beneficiaries/new": lambda: self._add_beneficiary(form),
            "/retail/transfer": lambda: self._submit_transfer(form),
            "/retail/transfer/confirm": lambda: self._confirm(form),
            "/backoffice/payments/approve": lambda: self._approve(form),
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._send(404, b"not found", "text/plain")

    # ------------------------------------------------------------- identity
    def _idp(self, inner: str, error: str = "") -> bytes:
        err = f'<div class="o-form-error-container err" role="alert">{html.escape(error)}</div>' if error else ""
        return page("Sign in", f'<div class="idp"><h1>Sign in</h1><p class="muted">Demo identity provider '
                               f'(Okta-compatible field names)</p>{err}{inner}</div>')

    def _idp_identifier(self, ret: str, error: str = "") -> None:
        self._send(200, self._idp(
            f'<form method="post" action="/okta/identify"><input type="hidden" name="return" value="{html.escape(ret)}">'
            '<label for="identifier">Username</label><input id="identifier" name="identifier" autocomplete="username">'
            '<input type="submit" value="Next"></form>', error))

    def _idp_password(self, f: dict[str, str]) -> None:
        ident, ret = f.get("identifier", ""), f.get("return", "/")
        if ident not in USERS:
            return self._idp_identifier(ret, "Unable to sign in")
        self._send(200, self._idp(
            f'<form method="post" action="/okta/password"><input type="hidden" name="identifier" value="{html.escape(ident)}">'
            f'<input type="hidden" name="return" value="{html.escape(ret)}"><p>{html.escape(ident)}</p>'
            '<label for="pw">Password</label><input id="pw" type="password" name="credentials.passcode">'
            '<input type="submit" value="Verify"></form>'))

    def _idp_check_password(self, f: dict[str, str]) -> None:
        ident, ret = f.get("identifier", ""), f.get("return", "/")
        user = USERS.get(ident)
        if not user or f.get("credentials.passcode") != user["password"]:
            return self._idp_identifier(ret, "Unable to sign in")
        self._send(200, self._idp(
            f'<form method="post" action="/okta/mfa"><input type="hidden" name="identifier" value="{html.escape(ident)}">'
            f'<input type="hidden" name="return" value="{html.escape(ret)}">'
            '<p>Enter the code from your authenticator app</p>'
            '<label for="code">Enter code</label><input id="code" type="text" name="credentials.passcode" '
            'inputmode="numeric" autocomplete="one-time-code"><input type="submit" value="Verify"></form>'))

    def _idp_check_mfa(self, f: dict[str, str]) -> None:
        ident, ret = f.get("identifier", ""), f.get("return", "/")
        user = USERS.get(ident)
        code = f.get("credentials.passcode", "")
        now = time.time()
        valid = user and code in {totp(user["totp"], at=now + d) for d in (-30, 0, 30)}
        if not valid:
            return self._idp_identifier(ret, "Invalid code. Try again.")
        sid = secrets.token_urlsafe(24)
        STATE.sessions[sid] = ident
        if not ret.startswith("/"):
            ret = "/"
        self._redirect(ret, {"Set-Cookie": f"demo_sid={sid}; Path=/; HttpOnly; SameSite=Lax"})

    def _logout(self) -> None:
        self._redirect("/okta/signin", {"Set-Cookie": "demo_sid=; Path=/; Max-Age=0"})

    # --------------------------------------------------------------- retail
    def _dashboard(self) -> None:
        user = self._require("maker")
        if not user:
            return
        u = USERS[user]
        body = (f'<h1 data-testid="dashboard-title">Welcome, {html.escape(u["name"])}</h1>'
                '<table class="kv"><tr><td>Savings account</td>'
                f'<td data-testid="primary-account" data-sensitive>{u["account"]}</td></tr>'
                f'<tr><td>Available balance</td><td data-testid="available-balance" data-sensitive>{inr(u["balance"])}</td></tr></table>')
        self._send(200, page("Dashboard", body, user, "retail"))

    def _beneficiary_form(self, error: str = "", f: dict[str, str] | None = None) -> None:
        user = self._require("maker")
        if not user:
            return
        f = f or {}
        v = lambda k: html.escape(f.get(k, ""))  # noqa: E731
        err = f'<div class="err" data-testid="form-error">{html.escape(error)}</div>' if error else ""
        body = (f'<h1 data-testid="add-beneficiary-title">Add beneficiary</h1>{err}'
                '<form method="post" data-testid="beneficiary-form">'
                f'<label for="bn">Beneficiary name</label><input id="bn" name="name" data-testid="ben-name" value="{v("name")}">'
                f'<label for="ba">Account number</label><input id="ba" name="account" data-testid="ben-account" value="{v("account")}">'
                '<label for="bc">Confirm account number</label><input id="bc" name="account_confirm" data-testid="ben-account-confirm">'
                f'<label for="bi">IFSC</label><input id="bi" name="ifsc" data-testid="ben-ifsc" value="{v("ifsc")}">'
                f'<label for="bk">Nickname</label><input id="bk" name="nickname" data-testid="ben-nickname" value="{v("nickname")}">'
                '<button type="submit" data-testid="ben-submit">Add beneficiary</button></form>')
        self._send(200, page("Add beneficiary", body, user, "retail"))

    def _add_beneficiary(self, f: dict[str, str]) -> None:
        user = self._require("maker")
        if not user:
            return
        if not re.fullmatch(r"\d{9,18}", f.get("account", "")):
            return self._beneficiary_form("Account number must be 9 to 18 digits", f)
        if f.get("account") != f.get("account_confirm"):
            return self._beneficiary_form("Account numbers do not match", f)
        ifsc = f.get("ifsc", "").upper()
        if not IFSC_RE.match(ifsc):
            return self._beneficiary_form("Enter a valid IFSC code", f)
        if not f.get("name") or not f.get("nickname"):
            return self._beneficiary_form("Name and nickname are required", f)
        ben = {"name": f["name"], "account": f["account"], "ifsc": ifsc, "nickname": f["nickname"]}
        with STATE.lock:
            STATE.beneficiaries.setdefault(user, []).append(ben)
        STATE.publish("beneficiary.created", f["account"], {
            "eventType": "BENEFICIARY_CREATED", "customer": user, "name": ben["name"], "accountNumber": ben["account"],
            "ifsc": ifsc, "nickname": ben["nickname"], "createdAt": now_iso()})
        self._redirect(f"/retail/beneficiaries?added={quote(ben['nickname'])}")

    def _beneficiaries(self, q: dict[str, str]) -> None:
        user = self._require("maker")
        if not user:
            return
        flash = (f'<div class="ok" data-testid="flash-success">Beneficiary {html.escape(q["added"])} added successfully</div>'
                 if q.get("added") else "")
        rows = "".join(f'<tr><td>{html.escape(b["nickname"])}</td><td>{html.escape(b["name"])}</td>'
                       f'<td data-sensitive>{b["account"]}</td><td>{b["ifsc"]}</td></tr>'
                       for b in STATE.beneficiaries.get(user, []))
        body = (f'<h1>Beneficiaries</h1>{flash}<table data-testid="beneficiary-grid"><thead><tr><th>Nickname</th>'
                f'<th>Name</th><th>Account</th><th>IFSC</th></tr></thead><tbody>{rows}</tbody></table>')
        self._send(200, page("Beneficiaries", body, user, "retail"))

    def _transfer_form(self, error: str = "") -> None:
        user = self._require("maker")
        if not user:
            return
        opts = "".join(f'<option value="{i}">{html.escape(b["nickname"])} - {html.escape(b["name"])}</option>'
                       for i, b in enumerate(STATE.beneficiaries.get(user, [])))
        err = f'<div class="err" data-testid="form-error">{html.escape(error)}</div>' if error else ""
        body = (f'<h1 data-testid="transfer-title">Transfer funds</h1>{err}<form method="post">'
                f'<label for="tb">Beneficiary</label><select id="tb" name="beneficiary" data-testid="tr-beneficiary">'
                f'<option value="">Select</option>{opts}</select>'
                '<label for="tm">Payment mode</label><select id="tm" name="mode" data-testid="tr-mode">'
                '<option>NEFT</option><option>IMPS</option><option>RTGS</option></select>'
                '<label for="ta">Amount (INR)</label><input id="ta" name="amount" data-testid="tr-amount">'
                '<label for="tr">Remarks</label><input id="tr" name="remarks" data-testid="tr-remarks">'
                '<button type="submit" data-testid="tr-continue">Continue</button></form>')
        self._send(200, page("Transfer", body, user, "retail"))

    def _submit_transfer(self, f: dict[str, str]) -> None:
        user = self._require("maker")
        if not user:
            return
        bens = STATE.beneficiaries.get(user, [])
        try:
            ben = bens[int(f.get("beneficiary", ""))]
        except (ValueError, IndexError):
            return self._transfer_form("Select a beneficiary")
        mode = f.get("mode", "NEFT")
        try:
            amount = Decimal(f.get("amount", "").replace(",", "")).quantize(Decimal("0.01"))
        except InvalidOperation:
            return self._transfer_form("Enter a valid amount")
        low, high = LIMITS.get(mode, LIMITS["NEFT"])
        if amount < low:
            return self._transfer_form(f"Minimum amount for {mode} is {inr(low)}")
        if amount > high:
            return self._transfer_form(f"Amount exceeds the {mode} per-transaction limit of {inr(high)}")
        draft = secrets.token_hex(6)
        STATE.drafts[draft] = {"user": user, "ben": ben, "mode": mode, "amount": amount, "remarks": f.get("remarks", "")}
        self._redirect(f"/retail/transfer/review?draft={draft}")

    def _review(self, draft_id: str) -> None:
        user = self._require("maker")
        if not user:
            return
        d = STATE.drafts.get(draft_id)
        if not d:
            return self._transfer_form("Session expired, start again")
        b = d["ben"]
        body = ('<h1 data-testid="review-title">Review transfer</h1><table class="kv">'
                f'<tr><td>To</td><td data-testid="rv-beneficiary">{html.escape(b["name"])}</td></tr>'
                f'<tr><td>Account</td><td data-testid="rv-account" data-sensitive>{b["account"]}</td></tr>'
                f'<tr><td>IFSC</td><td data-testid="rv-ifsc">{b["ifsc"]}</td></tr>'
                f'<tr><td>Mode</td><td data-testid="rv-mode">{d["mode"]}</td></tr>'
                f'<tr><td>Amount</td><td data-testid="rv-amount">{inr(d["amount"])}</td></tr>'
                f'<tr><td>Remarks</td><td data-testid="rv-remarks">{html.escape(d["remarks"])}</td></tr></table>'
                f'<form method="post" action="/retail/transfer/confirm"><input type="hidden" name="draft" value="{draft_id}">'
                '<button type="submit" data-testid="rv-confirm">Confirm and pay</button></form>')
        self._send(200, page("Review", body, user, "retail"))

    def _confirm(self, f: dict[str, str]) -> None:
        user = self._require("maker")
        if not user:
            return
        d = STATE.drafts.pop(f.get("draft", ""), None)
        if not d:
            return self._transfer_form("Session expired, start again")
        with STATE.lock:
            STATE.seq += 1
            ref = f"TXN{datetime.now().strftime('%Y%m%d')}{STATE.seq:04d}{secrets.token_hex(2).upper()}"
        b = d["ben"]
        payment = {"ref": ref, "user": user, "ben": b, "mode": d["mode"], "amount": d["amount"],
                   "remarks": d["remarks"], "status": "PENDING_APPROVAL", "created": now_iso()}
        STATE.payments[ref] = payment
        STATE.publish("payments.initiated", ref, {
            "eventType": "PAYMENT_INITIATED", "txnRef": ref, "mode": d["mode"], "amount": f"{d['amount']:.2f}",
            "currency": "INR", "debitAccount": USERS[user]["account"],
            "beneficiary": {"name": b["name"], "accountNumber": b["account"], "ifsc": b["ifsc"],
                            "nickname": b["nickname"]},
            "remarks": d["remarks"], "status": "PENDING_APPROVAL", "initiatedBy": user, "createdAt": payment["created"]})
        self._redirect(f"/retail/transfer/success?ref={ref}")

    def _success(self, ref: str) -> None:
        user = self._require("maker")
        if not user:
            return
        p = STATE.payments.get(ref)
        if not p:
            return self._send(404, page("Not found", "<h1>Unknown reference</h1>", user, "retail"))
        body = ('<h1 data-testid="success-title">Transfer submitted</h1>'
                '<div class="ok">Your payment has been sent for approval.</div><table class="kv">'
                f'<tr><td>Reference</td><td data-testid="txn-reference">{ref}</td></tr>'
                f'<tr><td>Status</td><td data-testid="txn-status">{p["status"]}</td></tr>'
                f'<tr><td>Amount</td><td data-testid="txn-amount">{inr(p["amount"])}</td></tr>'
                f'<tr><td>Mode</td><td data-testid="txn-mode">{p["mode"]}</td></tr>'
                f'<tr><td>Beneficiary</td><td data-testid="txn-beneficiary">{html.escape(p["ben"]["name"])}</td></tr></table>')
        self._send(200, page("Success", body, user, "retail"))

    # ----------------------------------------------------------- backoffice
    def _queue(self, q: dict[str, str]) -> None:
        user = self._require("checker")
        if not user:
            return
        term = q.get("q", "").strip()
        flash = (f'<div class="ok" data-testid="flash-success">Payment {html.escape(q["approved"])} approved</div>'
                 if q.get("approved") else "")
        rows = ""
        for p in sorted(STATE.payments.values(), key=lambda x: x["created"], reverse=True):
            if term and term not in p["ref"]:
                continue
            action = (f'<form method="post" action="/backoffice/payments/approve" style="margin:0">'
                      f'<input type="hidden" name="ref" value="{p["ref"]}"><button style="margin:0;padding:5px 12px" '
                      f'data-testid="approve-{p["ref"]}">Approve</button></form>' if p["status"] == "PENDING_APPROVAL" else "")
            rows += (f'<tr><td>{p["ref"]}</td><td>{html.escape(p["ben"]["name"])}</td><td data-sensitive>{p["ben"]["account"]}</td>'
                     f'<td>{inr(p["amount"])}</td><td>{p["mode"]}</td><td>{p["status"]}</td><td>{action}</td></tr>')
        body = (f'<h1 data-testid="queue-title">Payments queue</h1>{flash}<form method="get">'
                f'<label for="qs">Search reference</label><input id="qs" name="q" data-testid="bo-search" value="{html.escape(term)}">'
                '<button type="submit" data-testid="bo-search-btn">Search</button></form>'
                '<table data-testid="bo-payments-grid"><thead><tr><th>Reference</th><th>Beneficiary</th><th>Account</th>'
                f'<th>Amount</th><th>Mode</th><th>Status</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table>')
        self._send(200, page("Payments queue", body, user, "backoffice"))

    def _approve(self, f: dict[str, str]) -> None:
        user = self._require("checker")
        if not user:
            return
        p = STATE.payments.get(f.get("ref", ""))
        if p and p["status"] == "PENDING_APPROVAL":
            p["status"] = "APPROVED"
            STATE.publish("payments.approved", p["ref"], {
                "eventType": "PAYMENT_APPROVED", "txnRef": p["ref"], "status": "APPROVED", "approvedBy": user,
                "amount": f"{p['amount']:.2f}", "approvedAt": now_iso()})
        self._redirect(f"/backoffice/payments?q={quote(f.get('ref', ''))}&approved={quote(f.get('ref', ''))}")

    # --------------------------------------------------------------- events
    def _events(self, q: dict[str, str]) -> None:
        topic, since = q.get("topic"), int(q.get("since", "0") or 0)
        with STATE.lock:
            found = [e for e in STATE.events if (not topic or e["topic"] == topic) and e["timestamp_ms"] >= since]
        self._send(200, json.dumps(found).encode(), "application/json")


def start_in_thread(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="demo-bank", daemon=True).start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="QA Demo Bank")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"QA Demo Bank on http://{a.host}:{a.port}  (retail: /retail/dashboard, back office: /backoffice)")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
