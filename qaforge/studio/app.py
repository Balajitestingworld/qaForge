"""QA Forge Studio - the no-code control centre for the framework.

Start:  python -m qaforge studio            (http://127.0.0.1:8700)
        python -m qaforge studio --host 0.0.0.0 --port 8700   (team server - put it behind TLS/SSO)

Everything Studio does ends up as ordinary framework artefacts - YAML test specs, generated
page-object Python files, YAML data and mappings - and every run is a normal pytest run with
the usual evidence packs. Studio never executes code typed by a user.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
from dataclasses import asdict
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import yaml
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from pydantic import ValidationError

from qaforge.compliance.evidence import new_run_id, verify_pack
from qaforge.compliance.masker import masker
from qaforge.config.secrets import build_secret_provider
from qaforge.config.settings import load_settings
from qaforge.nocode import keywords as kw
from qaforge.nocode import registry
from qaforge.nocode.locator_expr import LocatorExprError, canonical
from qaforge.nocode.pom_writer import (ElementDef, GeneratedPage, PomError, add_elements, load_generated,
                                       save_generated)
from qaforge.nocode.spec import ID_RE, Spec, SpecError, list_specs, load_rows, load_spec, save_spec, spec_path
from qaforge.studio import catalog
from qaforge.studio.jobs import FINAL, JobManager
from qaforge.studio.security import ROLES, AuditLog, AuthError, UserStore, role_at_least, secret_key

IDLE_MINUTES = 30
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,50}$")


def create_app(root: Path | str = ".", data_dir: Path | str | None = None, max_parallel: int = 2,
               secure_cookies: bool = False) -> Flask:
    root = Path(root).resolve()
    data = Path(data_dir).resolve() if data_dir else root / "studio_data"
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key(data), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=secure_cookies, PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024, TEMPLATES_AUTO_RELOAD=True)
    users, audit = UserStore(data), AuditLog(data)
    jobs = JobManager(root, data, max_parallel=max_parallel)
    base_cfg = yaml.safe_load((root / "config/environments/base.yaml").read_text(encoding="utf-8")) or {}
    evidence_dir = (base_cfg.get("evidence") or {}).get("output_dir", "evidence")
    app.extensions["qaforge"] = {"root": root, "jobs": jobs, "users": users, "audit": audit}

    # ------------------------------------------------------------------ security plumbing
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _before() -> Any:
        g.user = None
        u = session.get("user")
        if u:
            if time.time() - session.get("seen", 0) > IDLE_MINUTES * 60:
                session.clear()
                if request.endpoint not in ("login", "static"):
                    flash("You were signed out after 30 minutes without activity.", "")
                    return redirect(url_for("login"))
            else:
                session["seen"] = time.time()
                rec = users.get(u["username"])
                if rec and rec.get("active", True):
                    g.user = {**u, "role": rec["role"]}
                else:
                    session.clear()
        if request.method == "POST":
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Your session expired - reload the page and try again.")
        return None

    @app.after_request
    def _headers(resp: Any) -> Any:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "media-src 'self'; frame-ancestors 'self'; form-action 'self'")
        return resp

    @app.context_processor
    def _ctx() -> dict[str, Any]:
        return {"user": g.get("user"), "envs": catalog.envs(root)}

    def requires(role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(fn)
            def inner(*a: Any, **k: Any) -> Any:
                if g.user is None:
                    if request.path.startswith("/api/"):
                        return jsonify(error="Sign in again"), 401
                    return redirect(url_for("login", next=request.path))
                if not role_at_least(g.user["role"], role):
                    abort(403, f"This needs the '{role}' role.")
                return fn(*a, **k)
            return inner
        return deco

    def log(action: str, target: str = "", detail: Any = None) -> None:
        audit.write(g.user["username"] if g.get("user") else None, request.remote_addr, action, target, detail)

    def is_local() -> bool:
        return request.remote_addr in ("127.0.0.1", "::1")

    # ------------------------------------------------------------------ helpers
    def default_env() -> str:
        names = catalog.envs(root)
        return "local" if "local" in names else (names[0] if names else "local")

    def apps_and_roles() -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
        apps: dict[str, set[str]] = {}
        per_env: dict[str, dict[str, Any]] = {}
        for env in catalog.envs(root):
            try:
                s = load_settings(env=env, root_dir=root)
            except Exception:  # noqa: BLE001 - guarded or broken envs are shown on the Environments page
                continue
            per_env[env] = {"apps": sorted(s.apps), "roles": sorted(s.users)}
            for a in s.apps:
                apps.setdefault(a, set()).update(s.users)
        return {a: sorted(r) for a, r in apps.items()}, per_env

    def register_env_secrets(env: str) -> None:
        """Lets Studio mask this env's secrets in job output even if a test prints one."""
        try:
            s = load_settings(env=env, root_dir=root)
            sec = build_secret_provider(s)
            for u in s.users.values():
                for name in (u.username_secret, u.password_secret, u.totp_secret):
                    sec.get_optional(name)
        except Exception:  # noqa: BLE001
            pass

    def valid_env(env: str) -> str:
        if env not in catalog.envs(root):
            abort(400, "Unknown environment")
        return env

    def page_index() -> list[registry.PageInfo]:
        return registry.read_pages(root)

    def editor_catalog() -> dict[str, Any]:
        apps, per_env = apps_and_roles()
        pages = page_index()
        try:
            flows = [asdict(f) for f in registry.discover_flows(root)]
        except Exception:  # noqa: BLE001 - a broken flow module must not block test building
            flows = []
        facs = {name: list(cls.model.model_fields) for name, cls in registry.factories().items()}
        data_cols: dict[str, list[str]] = {}
        for f in catalog.data_files(root):
            try:
                rows = load_rows(root, f)
                data_cols[f] = sorted({k for r in rows for k in r}) if rows else []
            except Exception:  # noqa: BLE001
                data_cols[f] = []
        from qaforge.data.generators import DataGenerator
        gens = sorted(n for n in dir(DataGenerator) if not n.startswith("_") and callable(getattr(DataGenerator, n))
                      and n not in ("seq",) and DataGenerator.__dict__.get(n) is not None
                      and getattr(DataGenerator, n).__code__.co_argcount <= 1)
        return {
            "keywords": [k.as_dict() for k in kw.KEYWORDS], "groups": kw.GROUPS, "rules": kw.RULES,
            "apps": apps, "per_env": per_env,
            "pages": [{"ref": p.ref, "app": p.app, "title": p.title, "path": p.path, "generated": p.generated,
                       "elements": [{"name": e.name, "kind": e.kind, "locator": e.locator, "component": e.component}
                                    for e in p.elements]} for p in pages],
            "flows": flows, "factories": facs, "mappings": catalog.mapping_names(root),
            "specs": [{"id": s.id, "title": s.title, "reusable": s.reusable} for s in list_specs(root)],
            "data_files": data_cols, "generators": gens,
        }

    def start_run(nodeids: list[str], env: str, browser: str, headed: bool, jira: str, title: str) -> Any:
        register_env_secrets(env)
        run_id = new_run_id()
        args = ["-m", "pytest", *nodeids, "--env", env, "-m", "", "-p", "no:cacheprovider", "--jira", jira,
                "--qa-browser", browser, "-o", "log_cli=true", "--log-cli-level=INFO",
                "-o", "log_cli_format=%(asctime)s %(message)s", "-o", "log_cli_date_format=%H:%M:%S",
                "-o", "console_output_style=classic", "-q"]
        if headed:
            args.append("--headed")
        job = jobs.start("run", title, g.user["username"], args, env={"QAF_RUN_ID": run_id}, run_id=run_id,
                         meta={"env": env, "tests": nodeids, "browser": browser, "jira": jira})
        log("run.start", title, {"env": env, "tests": nodeids, "job": job.id, "run_id": run_id})
        return job

    def nodeids_for_specs(spec_ids: list[str]) -> list[str]:
        items, _ = catalog.collect_tests(root, force=True)
        return [i["nodeid"] for i in items if i.get("spec_id") in spec_ids]

    def pydantic_errors(exc: ValidationError) -> list[dict[str, Any]]:
        out = []
        for e in exc.errors():
            loc = list(e.get("loc", ()))
            step = loc[1] if len(loc) > 1 and loc[0] == "steps" and isinstance(loc[1], int) else None
            msg = e.get("msg", "").removeprefix("Value error, ")
            where = "" if step is not None else " ".join(str(x) for x in loc if not isinstance(x, int))
            out.append({"step": step, "message": f"{where}: {msg}" if where else msg})
        return out

    # ------------------------------------------------------------------ auth routes
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if users.empty():
            return redirect(url_for("setup"))
        error, username = None, ""
        if request.method == "POST":
            username = request.form.get("username", "")
            try:
                u = users.verify(username, request.form.get("password", ""))
                session.clear()
                session.permanent = True
                session.update(user=u, seen=time.time(), csrf=secrets.token_urlsafe(24))
                audit.write(u["username"], request.remote_addr, "login.ok")
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
            except AuthError as exc:
                audit.write(username[:40], request.remote_addr, "login.failed")
                error = str(exc)
        return render_template("login.html", error=error, username=username, setup=False)

    @app.route("/setup", methods=["GET", "POST"])
    def setup() -> Any:
        if not users.empty():
            return redirect(url_for("login"))
        if not is_local():
            abort(403, "The first administrator can only be created on the Studio computer itself.")
        error = None
        if request.method == "POST":
            try:
                users.create(request.form.get("username", ""), request.form.get("password", ""), "admin",
                             request.form.get("display", ""))
                audit.write(request.form.get("username", ""), request.remote_addr, "user.create_first_admin")
                flash("Administrator created. Sign in to continue.", "good")
                return redirect(url_for("login"))
            except AuthError as exc:
                error = str(exc)
        return render_template("login.html", error=error, setup=True, username=request.form.get("username"))

    @app.post("/logout")
    def logout() -> Any:
        if g.user:
            log("logout")
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ home
    @app.get("/")
    @requires("viewer")
    def home() -> Any:
        runs = catalog.runs(root, evidence_dir, limit=8)
        specs = list_specs(root)
        pages = page_index()
        active = [j for j in jobs.list(20) if j.status not in FINAL]
        return render_template("home.html", runs=runs, last=runs[0] if runs else None, n_specs=len(specs),
                               n_pages=len(pages), n_elements=sum(len(p.elements) for p in pages), active=active)

    # ------------------------------------------------------------------ tests
    @app.get("/tests")
    @requires("viewer")
    def tests() -> Any:
        items, error = catalog.collect_tests(root, force=request.args.get("refresh") == "1")
        specs = {s.id: s for s in list_specs(root)}
        from qaforge.nocode.spec import broken_specs
        tags = sorted({m for i in items for m in i["markers"]})
        reusable = [s for s in specs.values() if s.reusable]
        return render_template("tests.html", items=items, error=error, specs=specs, tags=tags, reusable=reusable,
                               broken=broken_specs(root), default_env=default_env())

    @app.post("/tests/run")
    @requires("tester")
    def tests_run() -> Any:
        items, _ = catalog.collect_tests(root)
        known = {i["nodeid"] for i in items}
        chosen = [n for n in request.form.getlist("nodeid") if n in known]
        if not chosen:
            flash("Choose at least one test to run.", "bad")
            return redirect(url_for("tests"))
        env = valid_env(request.form.get("env", default_env()))
        browser = request.form.get("browser", "chromium")
        if browser not in ("chromium", "firefox", "webkit"):
            abort(400)
        jira = request.form.get("jira", "dry-run")
        if jira not in ("off", "dry-run", "on"):
            abort(400)
        if jira == "on" and not role_at_least(g.user["role"], "admin"):
            flash("Publishing to JIRA needs an admin; the run used dry-run instead.", "")
            jira = "dry-run"
        title = f"{len(chosen)} test{'s' if len(chosen) != 1 else ''} on {env}"
        job = start_run(chosen, env, browser, request.form.get("headed") == "1", jira, title)
        return redirect(url_for("job", job_id=job.id))

    # ------------------------------------------------------------------ test designer
    @app.get("/specs/new")
    @requires("tester")
    def spec_new() -> Any:
        draft = session.pop("draft_spec", None)
        spec = draft or {"id": "", "title": "", "steps": [{"action": "open_app", "options": {}}], "data": []}
        return render_template("spec_edit.html", spec=spec, is_new=True, catalog=editor_catalog(),
                               default_env=default_env())

    @app.get("/specs/<spec_id>")
    @requires("viewer")
    def spec_edit(spec_id: str) -> Any:
        try:
            spec = load_spec(root, spec_id).model_dump(mode="json")
        except SpecError as exc:
            abort(404, str(exc))
        except ValidationError:
            raw = yaml.safe_load(spec_path(root, spec_id).read_text(encoding="utf-8"))
            spec = raw
            flash("This test has problems (shown on the steps). Fix them and save.", "bad")
        return render_template("spec_edit.html", spec=spec, is_new=False, catalog=editor_catalog(),
                               default_env=default_env())

    @app.post("/api/specs/<spec_id>")
    @requires("tester")
    def api_spec_save(spec_id: str) -> Any:
        payload = request.get_json(force=True)
        is_new = bool(payload.pop("_new", False))
        run_env = payload.pop("_run_env", None)
        try:
            spec = Spec.model_validate(payload)
        except ValidationError as exc:
            return jsonify(ok=False, errors=pydantic_errors(exc)), 400
        if spec.id != spec_id and not is_new:
            return jsonify(ok=False, errors=[{"step": None, "message": "The test id can't be changed"}]), 400
        if is_new and spec_path(root, spec.id).exists():
            return jsonify(ok=False, errors=[{"step": None, "message": f"A test with id '{spec.id}' already exists"}]), 400
        for i, st in enumerate(spec.steps):
            if st.action == "use_steps" and st.options.get("spec") == spec.id:
                return jsonify(ok=False, errors=[{"step": i, "message": "A test can't reuse itself"}]), 400
        save_spec(root, spec, g.user["username"])
        log("spec.create" if is_new else "spec.save", spec.id, {"steps": len(spec.steps), "title": spec.title})
        result: dict[str, Any] = {"ok": True, "id": spec.id, "url": url_for("spec_edit", spec_id=spec.id)}
        if run_env:
            if spec.reusable:
                return jsonify(ok=False, errors=[{"step": None, "message": "Reusable steps run inside other tests"}]), 400
            nodeids = nodeids_for_specs([spec.id])
            if not nodeids:
                return jsonify(ok=False, errors=[{"step": None, "message": "Saved, but the test could not be collected"}]), 400
            job = start_run(nodeids, valid_env(run_env), "chromium", False, "dry-run", spec.title)
            result["job_url"] = url_for("job", job_id=job.id)
        return jsonify(result)

    @app.post("/specs/<spec_id>/delete")
    @requires("tester")
    def spec_delete(spec_id: str) -> Any:
        path = spec_path(root, spec_id)
        if path.exists():
            users_of = [s.id for s in list_specs(root)
                        if any(st.action == "use_steps" and st.options.get("spec") == spec_id for st in s.steps)]
            if users_of:
                flash(f"'{spec_id}' is used by {', '.join(users_of)} - remove those steps first.", "bad")
                return redirect(url_for("spec_edit", spec_id=spec_id))
            path.unlink()
            log("spec.delete", spec_id)
            flash(f"Deleted test '{spec_id}'.", "good")
        return redirect(url_for("tests"))

    # ------------------------------------------------------------------ page objects
    @app.get("/pages")
    @requires("viewer")
    def pages() -> Any:
        apps, per_env = apps_and_roles()
        by_app: dict[str, list[registry.PageInfo]] = {}
        for p in page_index():
            by_app.setdefault(p.app, []).append(p)
        checks = jobs.list(5, kind="check")
        return render_template("pages.html", by_app=by_app, apps=apps, per_env=per_env, checks=checks,
                               default_env=default_env())

    @app.get("/pages/<ref>")
    @requires("viewer")
    def page_detail(ref: str) -> Any:
        info = next((p for p in page_index() if p.ref == ref), None)
        if info is None:
            abort(404)
        source = (root / info.file).read_text(encoding="utf-8").splitlines()
        used_by = [s.id for s in list_specs(root) if any((st.target or "").startswith(ref + ".") or st.target == ref
                                                          for st in s.steps)]
        return render_template("page_detail.html", p=info, source=source, used_by=used_by)

    @app.get("/pages/<ref>/edit")
    @requires("tester")
    def page_edit(ref: str) -> Any:
        app_name, _, cls = ref.partition(".")
        try:
            gp = load_generated(root, app_name, cls)
        except PomError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("page_detail", ref=ref))
        return render_template("page_edit.html", page=gp.model_dump(), ref=ref)

    @app.post("/api/pages/<ref>")
    @requires("tester")
    def api_page_save(ref: str) -> Any:
        app_name, _, cls = ref.partition(".")
        try:
            load_generated(root, app_name, cls)
            payload = request.get_json(force=True)
            gp = GeneratedPage.model_validate({**payload, "app": app_name, "class_name": cls})
            save_generated(root, gp)
        except (PomError, ValidationError) as exc:
            msg = "; ".join(e["message"] for e in pydantic_errors(exc)) if isinstance(exc, ValidationError) else str(exc)
            return jsonify(ok=False, error=msg), 400
        log("page.save", ref, {"elements": len(gp.elements)})
        return jsonify(ok=True, url=url_for("page_detail", ref=ref))

    @app.post("/api/pages/add-elements")
    @requires("tester")
    def api_add_elements() -> Any:
        payload = request.get_json(force=True)
        dest = payload.get("destination") or {}
        try:
            new = [ElementDef.model_validate(e) for e in payload.get("elements", [])]
            if not new:
                return jsonify(ok=False, error="Tick at least one element"), 400
            if dest.get("mode") == "existing":
                app_name, _, cls = str(dest.get("ref", "")).partition(".")
                gp = load_generated(root, app_name, cls)
                gp, notes = add_elements(root, gp, new)
            else:
                cls = str(dest.get("class_name", ""))
                app_name = str(dest.get("app", ""))
                if any(p.class_name == cls and p.app == app_name for p in page_index()):
                    return jsonify(ok=False, error=f"{cls} already exists in {app_name}; add to it instead"), 400
                gp = GeneratedPage(app=app_name, class_name=cls, title=dest.get("title") or cls,
                                   path=dest.get("path") or None, elements=[])
                gp, notes = add_elements(root, gp, new)
                anchor = dest.get("loaded_anchor")
                if anchor and anchor in {e.name for e in gp.elements}:
                    gp.loaded_anchor = anchor
            save_generated(root, gp)
        except ValidationError as exc:
            return jsonify(ok=False, error="; ".join(e["message"] for e in pydantic_errors(exc))), 400
        except (PomError, LocatorExprError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        ref = f"{gp.app}.{gp.class_name}"
        log("page.add_elements", ref, {"added": [e.name for e in new], "notes": notes})
        return jsonify(ok=True, url=url_for("page_detail", ref=ref), notes=notes)

    @app.post("/pages/check")
    @requires("tester")
    def pages_check() -> Any:
        env = valid_env(request.form.get("env", default_env()))
        app_name, role, page_app = request.form.get("app", ""), request.form.get("role", ""), request.form.get("page_app", "")
        if not NAME_RE.match(app_name) or (role and not NAME_RE.match(role)) or not NAME_RE.match(page_app):
            abort(400)
        return _tool_job("check", f"Locator health: {page_app} on {env}",
                         ["--env", env, "--app", app_name, "--role", role, "--page-app", page_app],
                         {"env": env, "app": app_name, "role": role, "page_app": page_app})

    # ------------------------------------------------------------------ locator spy
    @app.route("/spy", methods=["GET", "POST"])
    @requires("tester")
    def spy() -> Any:
        apps, per_env = apps_and_roles()
        if request.method == "GET":
            return render_template("spy.html", apps=apps, per_env=per_env, default_env=default_env(),
                                   recent=jobs.list(8, kind="scan") + jobs.list(4, kind="pick"),
                                   prefill=request.args)
        env = valid_env(request.form.get("env", ""))
        app_name, role = request.form.get("app", ""), request.form.get("role", "")
        path, mode = request.form.get("path", "/") or "/", request.form.get("mode", "scan")
        if not NAME_RE.match(app_name) or (role and not NAME_RE.match(role)) or mode not in ("scan", "pick"):
            abort(400)
        if not path.startswith("/") or len(path) > 300:
            flash("Address must start with /", "bad")
            return redirect(url_for("spy"))
        return _tool_job(mode, f"{'Scan' if mode == 'scan' else 'Pick'} {app_name}{path} on {env}",
                         ["--env", env, "--app", app_name, "--role", role, "--path", path],
                         {"env": env, "app": app_name, "role": role, "path": path})

    def _tool_job(cmd: str, title: str, extra: list[str], meta: dict[str, Any]) -> Any:
        job_id = jobs.new_id()
        args = ["-m", "qaforge.nocode.browser_tools", cmd, *extra, "--out", str(jobs.job_dir(job_id))]
        job = jobs.start(cmd, title, g.user["username"], args, meta=meta, job_id=job_id)
        log("tool.start", title, {"job": job.id})
        return redirect(url_for("job", job_id=job.id))

    @app.get("/spy/<job_id>")
    @requires("tester")
    def spy_result(job_id: str) -> Any:
        job = jobs.get(job_id)
        if job is None or job.kind not in ("scan", "pick"):
            abort(404)
        result = jobs.result(job_id)
        if result is None:
            return redirect(url_for("job", job_id=job_id))
        generated = [p for p in page_index() if p.generated]
        apps, _ = apps_and_roles()
        app_dirs = sorted({p.app for p in page_index()})
        return render_template("spy_result.html", job=job, result=result, generated=generated, app_dirs=app_dirs)

    @app.get("/jobs/<job_id>/file/<name>")
    @requires("viewer")
    def job_file(job_id: str, name: str) -> Any:
        if name not in ("page.png", "recording.py"):
            abort(404)
        return send_from_directory(jobs.job_dir(job_id), name)

    # ------------------------------------------------------------------ recorder
    @app.route("/record", methods=["GET", "POST"])
    @requires("tester")
    def record() -> Any:
        apps, per_env = apps_and_roles()
        if request.method == "GET":
            return render_template("record.html", apps=apps, per_env=per_env, default_env=default_env(),
                                   recent=jobs.list(6, kind="record"))
        env = valid_env(request.form.get("env", ""))
        app_name, role, path = request.form.get("app", ""), request.form.get("role", ""), request.form.get("path", "/") or "/"
        if not NAME_RE.match(app_name) or (role and not NAME_RE.match(role)) or not path.startswith("/"):
            abort(400)
        return _tool_job("record", f"Record {app_name}{path} on {env}",
                         ["--env", env, "--app", app_name, "--role", role, "--path", path],
                         {"env": env, "app": app_name, "role": role, "path": path})

    @app.route("/record/<job_id>", methods=["GET", "POST"])
    @requires("tester")
    def record_result(job_id: str) -> Any:
        job = jobs.get(job_id)
        if job is None or job.kind != "record":
            abort(404)
        result = jobs.result(job_id)
        if result is None:
            return redirect(url_for("job", job_id=job_id))
        if request.method == "POST":
            spec_id, title = request.form.get("id", "").strip(), request.form.get("title", "").strip()
            if not ID_RE.match(spec_id) or not title:
                flash("Give the test an id (lowercase, underscores) and a title.", "bad")
            elif spec_path(root, spec_id).exists():
                flash(f"A test with id '{spec_id}' already exists.", "bad")
            else:
                session["draft_spec"] = {"id": spec_id, "title": title, "steps": result["steps"], "data": [],
                                         "description": f"Recorded by {g.user['username']}"}
                log("record.to_spec", spec_id, {"job": job_id, "steps": len(result["steps"])})
                return redirect(url_for("spec_new"))
        return render_template("record_result.html", job=job, result=result,
                               labels={k.name: k.label for k in kw.KEYWORDS})

    # ------------------------------------------------------------------ jobs
    @app.get("/jobs", endpoint="jobs")
    @requires("viewer")
    def jobs_page() -> Any:
        return render_template("jobs.html", jobs=jobs.list(80))

    @app.get("/jobs/<job_id>")
    @requires("viewer")
    def job(job_id: str) -> Any:
        j = jobs.get(job_id)
        if j is None:
            abort(404)
        return render_template("job.html", job=j, result_url=_result_url(j))

    def _result_url(j: Any) -> str | None:
        if j.kind in ("scan", "pick"):
            return url_for("spy_result", job_id=j.id)
        if j.kind == "record":
            return url_for("record_result", job_id=j.id)
        if j.kind == "check":
            return url_for("check_result", job_id=j.id)
        if j.kind == "run" and j.run_id:
            return url_for("run_detail", run_id=j.run_id)
        return None

    @app.get("/api/jobs/<job_id>")
    @requires("viewer")
    def api_job(job_id: str) -> Any:
        j = jobs.get(job_id)
        if j is None:
            return jsonify(error="unknown job"), 404
        text, offset = jobs.read_log(job_id, int(request.args.get("offset", 0)))
        has_result = j.status in FINAL and (jobs.result(job_id) is not None or j.kind == "run")
        return jsonify(status=j.status, text=masker.mask(text) if text else "", offset=offset,
                       result_url=_result_url(j) if has_result else None)

    @app.post("/jobs/<job_id>/cancel")
    @requires("tester")
    def job_cancel(job_id: str) -> Any:
        if jobs.cancel(job_id):
            log("job.cancel", job_id)
        return redirect(url_for("job", job_id=job_id))

    @app.get("/check/<job_id>")
    @requires("viewer")
    def check_result(job_id: str) -> Any:
        j = jobs.get(job_id)
        result = jobs.result(job_id)
        if j is None or result is None:
            return redirect(url_for("job", job_id=job_id))
        return render_template("check_result.html", job=j, result=result)

    # ------------------------------------------------------------------ data + mappings
    def _data_path(rel: str) -> Path:
        p = (root / rel).resolve()
        base = (root / "testdata").resolve()
        if base not in p.parents or p.suffix not in catalog.DATA_EXT:
            abort(400, "Data files live in testdata/")
        return p

    @app.get("/data")
    @requires("viewer")
    def data() -> Any:
        facs = {n: list(c.model.model_fields) for n, c in registry.factories().items()}
        used = {f: [s.id for s in list_specs(root) if s.data_file == f] for f in catalog.data_files(root)}
        return render_template("data.html", files=catalog.data_files(root), factories=facs, used=used)

    @app.get("/data/edit")
    @requires("viewer")
    def data_edit() -> Any:
        rel = request.args.get("file", "")
        p = _data_path(rel)
        rows = load_rows(root, rel) if p.exists() else []
        cols = list(dict.fromkeys(k for r in rows for k in r)) or ["id", "jira"]
        return render_template("table_edit.html", kind="data", name=rel, columns=cols,
                               rows=[{k: ("" if r.get(k) is None else str(r.get(k))) for k in cols} for r in rows],
                               save_url=url_for("api_data_save", file=rel), back=url_for("data"))

    @app.post("/data/new")
    @requires("tester")
    def data_new() -> Any:
        name = request.form.get("name", "").strip()
        if not NAME_RE.match(name):
            flash("File name: lowercase letters, digits and underscores.", "bad")
            return redirect(url_for("data"))
        rel = f"testdata/{name}.yaml"
        p = _data_path(rel)
        if not p.exists():
            p.write_text(yaml.safe_dump([{"id": "case1", "jira": ""}], sort_keys=False), encoding="utf-8")
            log("data.create", rel)
        return redirect(url_for("data_edit", file=rel))

    @app.post("/api/data")
    @requires("tester")
    def api_data_save() -> Any:
        rel = request.args.get("file", "")
        p = _data_path(rel)
        payload = request.get_json(force=True)
        cols = [c for c in payload.get("columns", []) if isinstance(c, str) and c.strip()]
        rows = [{c: r.get(c, "") for c in cols} for r in payload.get("rows", []) if any(str(v).strip() for v in r.values())]
        ids = [r.get("id") for r in rows if r.get("id")]
        if len(ids) != len(set(ids)):
            return jsonify(ok=False, error="Each row needs a different id"), 400
        bad = [c for c in cols if not re.match(r"^[A-Za-z_][A-Za-z0-9_ ]{0,40}$", c)]
        if bad:
            return jsonify(ok=False, error=f"Column names: letters, digits, spaces, underscores ({', '.join(bad)})"), 400
        if p.suffix == ".csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
            p.write_text(buf.getvalue(), encoding="utf-8")
        elif p.suffix == ".json":
            p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        else:
            clean = [{k: v for k, v in r.items() if v != ""} for r in rows]
            p.write_text(yaml.safe_dump(clean, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log("data.save", rel, {"rows": len(rows), "columns": cols})
        return jsonify(ok=True)

    @app.get("/api/factories/<name>/sample")
    @requires("viewer")
    def api_factory_sample(name: str) -> Any:
        facs = registry.factories()
        if name not in facs:
            abort(404)
        from qaforge.config.settings import DataConfig
        from qaforge.data.factories import use_generator
        from qaforge.data.generators import DataGenerator
        use_generator(DataGenerator(DataConfig(), seed=secrets.randbits(32), run_tag="SAMPLE"))
        return jsonify(masker.mask_obj(facs[name].build().model_dump(mode="json")))

    def _mapping_path(name: str) -> Path:
        if not NAME_RE.match(name):
            abort(400)
        return root / "config" / "mappings" / f"{name}.yaml"

    @app.get("/mappings")
    @requires("viewer")
    def mappings() -> Any:
        info = []
        for n in catalog.mapping_names(root):
            d = yaml.safe_load(_mapping_path(n).read_text(encoding="utf-8")) or {}
            used = [s.id for s in list_specs(root) if any(st.options.get("mapping") == n for st in s.steps)]
            info.append({"name": n, "title": d.get("name", n), "fields": len(d.get("fields", [])), "used": used})
        return render_template("mappings.html", mappings=info)

    @app.get("/mappings/<name>")
    @requires("viewer")
    def mapping_edit(name: str) -> Any:
        p = _mapping_path(name)
        d = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {"name": name, "fields": []}
        rows = [{"field": str(f.get("field", "")), "expected": "" if f.get("expected") is None else str(f.get("expected")),
                 "rule": f.get("rule", "equals")} for f in d.get("fields", [])]
        return render_template("table_edit.html", kind="mapping", name=name, title=d.get("name", name),
                               columns=["field", "expected", "rule"], rows=rows, rules=kw.RULES,
                               save_url=url_for("api_mapping_save", name=name), back=url_for("mappings"))

    @app.post("/mappings/new")
    @requires("tester")
    def mapping_new() -> Any:
        name = request.form.get("name", "").strip()
        if not NAME_RE.match(name):
            flash("Name: lowercase letters, digits and underscores.", "bad")
            return redirect(url_for("mappings"))
        return redirect(url_for("mapping_edit", name=name))

    @app.post("/api/mappings/<name>")
    @requires("tester")
    def api_mapping_save(name: str) -> Any:
        p = _mapping_path(name)
        payload = request.get_json(force=True)
        fields = []
        for r in payload.get("rows", []):
            if not str(r.get("field", "")).strip():
                continue
            rule = r.get("rule") or "equals"
            if rule not in kw.RULES:
                return jsonify(ok=False, error=f"Unknown rule '{rule}'"), 400
            f: dict[str, Any] = {"field": str(r["field"]).strip()}
            if rule != "exists":
                f["expected"] = str(r.get("expected", ""))
            if rule != "equals":
                f["rule"] = rule
            fields.append(f)
        doc = {"name": str(payload.get("title") or name), "fields": fields}
        p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log("mapping.save", name, {"fields": len(fields)})
        return jsonify(ok=True)

    # ------------------------------------------------------------------ runs + evidence
    @app.get("/runs")
    @requires("viewer")
    def runs() -> Any:
        return render_template("runs.html", runs=catalog.runs(root, evidence_dir))

    @app.get("/runs/<run_id>")
    @requires("viewer")
    def run_detail(run_id: str) -> Any:
        r = next((x for x in catalog.runs(root, evidence_dir, limit=500) if x["run_id"] == run_id), None)
        if r is None:
            job = next((j for j in jobs.list(200, kind="run") if j.run_id == run_id), None)
            if job is not None:
                return redirect(url_for("job", job_id=job.id))
            abort(404)
        run_dir = root / evidence_dir / run_id
        for res in r["results"]:
            res["report_url"] = url_for("evidence_file", sub=f"{run_id}/{Path(res['evidence_dir']).relative_to(run_dir).as_posix()}/report.html")
        jira = None
        jf = run_dir / "jira_publish_result.json"
        if jf.exists():
            jira = json.loads(jf.read_text(encoding="utf-8"))
        return render_template("run_detail.html", r=r, jira=jira,
                               has_plan=(run_dir / "jira_publish_plan.json").exists(),
                               integrity=session.pop(f"integrity_{run_id}", None))

    @app.post("/runs/<run_id>/verify")
    @requires("viewer")
    def run_verify(run_id: str) -> Any:
        if not re.match(r"^[\w-]+$", run_id):
            abort(400)
        tests_dir = root / evidence_dir / run_id / "tests"
        out = []
        for z in sorted(tests_dir.glob("*.zip")):
            ok, problems = verify_pack(z)
            out.append({"file": z.name, "ok": ok, "problems": problems})
        session[f"integrity_{run_id}"] = out
        log("evidence.verify", run_id, {"ok": all(o["ok"] for o in out), "packs": len(out)})
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/evidence/<path:sub>")
    @requires("viewer")
    def evidence_file(sub: str) -> Any:
        if ".." in Path(sub).parts:
            abort(404)
        return send_from_directory(root / evidence_dir, sub)

    # ------------------------------------------------------------------ environments
    @app.get("/envs")
    @requires("viewer")
    def envs() -> Any:
        names = catalog.envs(root)
        tab = request.args.get("env") or (default_env() if names else "")
        if tab not in names and tab != "_new":
            tab = names[0] if names else "_new"
        summary = catalog.env_summary(root, tab) if tab in names else None
        raw = catalog.env_raw(root, tab) if tab in names else {}
        local_file = catalog.local_secret_file(root, tab) if tab in names else None
        local_names: set[str] = set()
        if local_file and local_file.exists():
            local_names = {ln.split("=", 1)[0] for ln in local_file.read_text(encoding="utf-8").splitlines()
                           if "=" in ln and not ln.startswith("#")}
        availability: dict[str, bool] = {}
        if tab in names:
            try:
                sec = build_secret_provider(load_settings(env=tab, root_dir=root))
                availability = {n: sec.has(n) for n in catalog.secret_names(raw)}
            except Exception:  # noqa: BLE001 - guarded/broken envs: fall back to what Studio stored
                availability = {n: n in local_names for n in catalog.secret_names(raw)}
        return render_template("envs.html", names=names, tab=tab, s=summary, raw=raw, local_names=local_names,
                               all_secrets=catalog.secret_names(raw) if raw else [], availability=availability)

    @app.post("/envs/new")
    @requires("admin")
    def env_new() -> Any:
        name = request.form.get("name", "").strip().lower()
        copy_from = request.form.get("copy_from", "")
        try:
            path = catalog.env_file(root, name)
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("envs", env="_new"))
        if path.exists():
            flash(f"Environment '{name}' already exists.", "bad")
            return redirect(url_for("envs", env=name))
        raw = catalog.env_raw(root, copy_from) if copy_from in catalog.envs(root) else {"apps": {}, "users": {}}
        raw.pop("secrets", None) if raw.get("secrets", {}).get("dotenv_files") else None
        raw["is_production"], raw["allow_production"] = False, False
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log("env.create", name, {"copied_from": copy_from or None})
        flash(f"Created {name}" + (f" as a copy of {copy_from}" if copy_from else "") + ". Fill in its details.", "good")
        return redirect(url_for("env_form", env=name))

    @app.get("/envs/<env>/settings")
    @requires("admin")
    def env_form(env: str) -> Any:
        valid_env(env)
        return render_template("env_form.html", env=env, data={
            "raw": catalog.env_raw(root, env), "base": catalog.base_raw(root), "schema": catalog.ENV_FORM,
            "save_url": url_for("api_env_save", env=env), "done_url": url_for("envs", env=env)})

    def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = d
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if value is None or value == "":
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value

    def _prune(d: Any) -> Any:
        if isinstance(d, dict):
            out = {k: _prune(v) for k, v in d.items()}
            return {k: v for k, v in out.items() if v not in ({}, None)}
        return d

    @app.post("/api/envs/<env>")
    @requires("admin")
    def api_env_save(env: str) -> Any:
        valid_env(env)
        payload = request.get_json(force=True)
        raw = catalog.env_raw(root, env)
        fields = {f["key"]: f for sec in catalog.ENV_FORM for f in sec.get("fields", []) if "list" not in sec}
        for key, value in (payload.get("values") or {}).items():
            f = fields.get(key)
            if f is None:
                continue
            if f.get("kind") == "bool":
                value = bool(value)
            elif f.get("kind") == "number":
                value = int(value) if str(value).strip() else None
            elif f.get("kind") == "select" and value not in f["choices"]:
                return jsonify(ok=False, error=f"{f['label']}: choose one of the options"), 400
            else:
                value = str(value).strip() or None
            _set_path(raw, key, value)
        for list_name in ("apps", "users"):
            items: dict[str, Any] = {}
            for item in payload.get(list_name) or []:
                k = str(item.get("_key", "")).strip()
                if not k:
                    continue
                if not NAME_RE.match(k):
                    return jsonify(ok=False, error=f"'{k}': names are lowercase letters, digits and underscores"), 400
                if k in items:
                    return jsonify(ok=False, error=f"'{k}' is listed twice"), 400
                items[k] = {f: str(v).strip() for f, v in item.items() if f != "_key" and str(v).strip()}
            raw[list_name] = items
        raw = _prune(raw)
        text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        error = catalog.validate_env_yaml(root, env, text)
        if error:
            return jsonify(ok=False, error=error), 400
        catalog.env_file(root, env).write_text(text, encoding="utf-8")
        log("env.save", env, {"apps": sorted(raw.get("apps", {})), "roles": sorted(raw.get("users", {}))})
        return jsonify(ok=True, url=url_for("envs", env=env))

    @app.post("/envs/<env>/secret")
    @requires("admin")
    def env_secret(env: str) -> Any:
        valid_env(env)
        raw = catalog.env_raw(root, env)
        name, value = request.form.get("name", "").strip(), request.form.get("value", "")
        if raw.get("is_production"):
            flash("Production secrets must come from the vault, not from Studio.", "bad")
            return redirect(url_for("envs", env=env))
        if (raw.get("secrets") or {}).get("provider") == "vault":
            flash("This environment reads secrets from the vault. Store the value there.", "bad")
            return redirect(url_for("envs", env=env))
        try:
            catalog.set_local_secret(root, env, name, value)
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("envs", env=env))
        rel = f"config/secrets/{env}.env"
        files = (raw.setdefault("secrets", {})).setdefault("dotenv_files", [])
        if rel not in files:
            files.append(rel)
            catalog.env_file(root, env).write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log("env.secret_set" if value else "env.secret_cleared", f"{env}:{name}")  # the value is never logged
        flash(f"{'Saved' if value else 'Removed'} {name}. Its value can't be viewed again.", "good")
        return redirect(url_for("envs", env=env))

    @app.route("/envs/<env>", methods=["GET", "POST"])
    @requires("admin")
    def env_edit(env: str) -> Any:
        valid_env(env)
        path = root / "config" / "environments" / f"{env}.yaml"
        text, error = path.read_text(encoding="utf-8"), None
        if request.method == "POST":
            text = request.form.get("yaml", "").replace("\r\n", "\n")
            error = catalog.validate_env_yaml(root, env, text)
            if error is None:
                path.write_text(text, encoding="utf-8")
                log("env.save_text", env)
                flash(f"Saved {env}. The next run uses the new settings.", "good")
                return redirect(url_for("envs", env=env))
        return render_template("env_edit.html", env=env, text=text, error=error)

    # ------------------------------------------------------------------ admin
    @app.get("/admin")
    @requires("admin")
    def admin() -> Any:
        who = request.args.get("user") or None
        return render_template("admin.html", users=users.list(), roles=ROLES, audit=audit.tail(300, who), who=who)

    @app.post("/admin/users")
    @requires("admin")
    def admin_user_create() -> Any:
        try:
            users.create(request.form.get("username", ""), request.form.get("password", ""),
                         request.form.get("role", "viewer"), request.form.get("display", ""))
            log("user.create", request.form.get("username", ""), {"role": request.form.get("role")})
            flash("User created. Share the password through your usual secure channel.", "good")
        except AuthError as exc:
            flash(str(exc), "bad")
        return redirect(url_for("admin"))

    @app.post("/admin/users/<username>")
    @requires("admin")
    def admin_user_update(username: str) -> Any:
        try:
            if request.form.get("password"):
                users.set_password(username, request.form["password"])
                log("user.reset_password", username)
            role = request.form.get("role")
            active = request.form.get("active") == "1"
            if username == g.user["username"] and (role != "admin" or not active):
                raise AuthError("You can't remove your own admin access")
            users.update(username, role=role, active=active)
            log("user.update", username, {"role": role, "active": active})
            flash(f"Updated {username}.", "good")
        except AuthError as exc:
            flash(str(exc), "bad")
        return redirect(url_for("admin"))

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def _err(e: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=e.description), e.code
        return render_template("error.html", code=e.code, message=e.description), e.code

    return app


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) == 0


def serve(root: Path, host: str = "127.0.0.1", port: int = 8700, max_parallel: int = 2,
          open_browser: bool = False) -> None:
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    if _port_in_use(host, port):
        print(f"QA Forge Studio is already running at {url}")
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return
    app = create_app(root, max_parallel=max_parallel, secure_cookies=False)
    print(f"QA Forge Studio on {url}")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    if host not in ("127.0.0.1", "localhost"):
        print("Shared mode: put Studio behind your HTTPS reverse proxy before others use it.")
    try:
        from waitress import serve as wserve  # production WSGI server if installed
        wserve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
