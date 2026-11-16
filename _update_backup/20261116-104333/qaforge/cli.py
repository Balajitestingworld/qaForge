"""qaforge command line.

    qaforge new-page   --app retail_banking --name CustomerSearch --path /customers/search
    qaforge new-flow   --app retail_banking --name Onboarding
    qaforge new-test   --app retail --role maker --name customer_search --jira BANK-201
    qaforge verify-evidence evidence/<run>/tests/<test>.zip      # auditor integrity check
    qaforge doctor     --env sit                                  # config + secret availability check
    qaforge totp       --secret-name RETAIL_MAKER_TOTP_SEED --env local   # current code, for manual debugging
    qaforge studio     [--host 127.0.0.1 --port 8700]             # no-code web UI
    qaforge studio-user alice --role tester                       # add a Studio user
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TPL = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "scaffold_templates")),
                   keep_trailing_newline=True)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("-", "_").lower()


def _camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[_\-\s]+", name) if p)


def _write(path: Path, content: str) -> None:
    if path.exists():
        sys.exit(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "__init__.py").touch(exist_ok=True)
    if path.parts[0] == "apps":  # apps/<app>/__init__.py so the app package imports cleanly
        (Path(*path.parts[:2]) / "__init__.py").touch(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"created {path}")


def cmd_new_page(a: argparse.Namespace) -> None:
    cls = _camel(a.name) + ("" if a.name.endswith("Page") else "Page")
    content = _TPL.get_template("page.py.j2").render(class_name=cls, path=a.path or "/",
                                                     title=re.sub(r"(?<!^)(?=[A-Z])", " ", _camel(a.name)))
    _write(Path("apps") / a.app / "pages" / f"{_snake(cls)}.py", content)


def cmd_new_flow(a: argparse.Namespace) -> None:
    cls = _camel(a.name) + ("" if a.name.endswith("Flows") else "Flows")
    content = _TPL.get_template("flow.py.j2").render(class_name=cls, title=_camel(a.name))
    _write(Path("apps") / a.app / "flows" / f"{_snake(a.name)}.py", content)


def cmd_new_test(a: argparse.Namespace) -> None:
    content = _TPL.get_template("test.py.j2").render(func_name=_snake(a.name), app=a.app, role=a.role, jira=a.jira)
    _write(Path("tests") / a.folder / f"test_{_snake(a.name)}.py", content)


def cmd_verify(a: argparse.Namespace) -> None:
    from qaforge.compliance.evidence import verify_pack

    ok, problems = verify_pack(Path(a.path))
    if ok:
        print(f"OK - every file in {a.path} matches its SHA-256 in manifest.json")
        return
    print(f"TAMPERED / INCOMPLETE - {a.path}")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)


def cmd_doctor(a: argparse.Namespace) -> None:
    from qaforge.config.secrets import build_secret_provider
    from qaforge.config.settings import load_settings

    s = load_settings(env=a.env)
    sec = build_secret_provider(s)
    print(f"Environment : {s.env}  (production-like: {s.is_production})")
    for name, app in s.apps.items():
        print(f"App         : {name:<12} {app.base_url}  auth={app.auth}")
    missing = 0
    for role, u in s.users.items():
        for label, secret in (("user", u.username_secret), ("password", u.password_secret), ("totp", u.totp_secret)):
            if not secret:
                continue
            ok = sec.has(secret)
            missing += not ok
            print(f"Secret      : {role:<10} {label:<9} {secret:<34} {'present' if ok else 'MISSING'}")
    print(f"Events      : {s.events.source}")
    print(f"JIRA        : enabled={s.jira.enabled} dry_run={s.jira.dry_run} mode={s.jira.mode} "
          f"deployment={s.jira.deployment}")
    if missing:
        sys.exit(f"{missing} secret(s) missing - provide them through your vault / CI secret store")


def cmd_totp(a: argparse.Namespace) -> None:
    from qaforge.auth.totp import seconds_remaining, totp
    from qaforge.config.secrets import build_secret_provider
    from qaforge.config.settings import load_settings

    sec = build_secret_provider(load_settings(env=a.env))
    print(f"{totp(sec.get(a.secret_name))}  (valid {seconds_remaining():.0f}s more)")


def cmd_studio(a: argparse.Namespace) -> None:
    from qaforge.studio.app import serve

    serve(Path.cwd(), host=a.host, port=a.port, max_parallel=a.parallel, open_browser=a.open)


def cmd_studio_user(a: argparse.Namespace) -> None:
    import getpass

    from qaforge.studio.security import AuditLog, UserStore

    data = Path.cwd() / "studio_data"
    pw = getpass.getpass(f"Password for {a.username}: ")
    UserStore(data).create(a.username, pw, a.role, a.name or a.username)
    AuditLog(data).write("cli", "localhost", "user.create", a.username, {"role": a.role})
    print(f"Created {a.role} '{a.username}'")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="qaforge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new-page", help="scaffold a page object")
    p.add_argument("--app", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--path", default="/")
    p.set_defaults(fn=cmd_new_page)

    p = sub.add_parser("new-flow", help="scaffold a business flow")
    p.add_argument("--app", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(fn=cmd_new_flow)

    p = sub.add_parser("new-test", help="scaffold a test")
    p.add_argument("--app", required=True, help="app key from the env config, e.g. retail")
    p.add_argument("--role", default="maker")
    p.add_argument("--name", required=True)
    p.add_argument("--jira", default="BANK-000")
    p.add_argument("--folder", default="generated")
    p.set_defaults(fn=cmd_new_test)

    p = sub.add_parser("verify-evidence", help="check an evidence folder or zip against its manifest")
    p.add_argument("path")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("doctor", help="validate config and secret availability")
    p.add_argument("--env", default=None)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("totp", help="print the current TOTP for a configured seed secret")
    p.add_argument("--secret-name", required=True)
    p.add_argument("--env", default=None)
    p.set_defaults(fn=cmd_totp)

    p = sub.add_parser("studio", help="start QA Forge Studio (no-code web UI)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.add_argument("--parallel", type=int, default=2, help="how many jobs may run at the same time")
    p.add_argument("--open", action="store_true", help="open Studio in the default browser")
    p.set_defaults(fn=cmd_studio)

    p = sub.add_parser("studio-user", help="create a Studio user from the command line")
    p.add_argument("username")
    p.add_argument("--role", choices=["viewer", "tester", "admin"], default="tester")
    p.add_argument("--name", default="")
    p.set_defaults(fn=cmd_studio_user)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
