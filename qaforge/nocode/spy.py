"""Locator Spy - harvest reliable locators from a live page.

How it works:
  1. An injected script lists the meaningful elements on the page (inputs, buttons, links,
     headings, tables, anything with a test id) including those inside open shadow roots and
     iframes, and records facts about each one: role, accessible name, label, placeholder,
     test id, id, name, text, position, whether it is sensitive.
  2. Candidate locators are built in Playwright's own priority order:
         test id > role + accessible name > label > placeholder > alt/title > text > id > name > CSS
  3. Every candidate is executed with Playwright's real locator engine and must resolve to
     exactly ONE element - the same element it was built for. Guesses that match nothing,
     match several elements, or match a different element are thrown away.
  4. Values that look generated (e.g. id="ember1234", random hashes) are penalised, and text of
     sensitive elements (account numbers, balances) is never used inside a locator.
  5. If nothing is unique, a position-based fallback (.nth) is offered and flagged as fragile.

Two modes: `scan_page` (whole page, headless OK) and `pick_session` (headed browser with an
overlay: the tester clicks the elements they want).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame, Page

from qaforge.compliance.masker import masker
from qaforge.nocode.locator_expr import build_expr, canonical

TEST_ID_ATTRS = ["data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation-id"]
DYNAMIC = re.compile(
    r"\d{4,}|[0-9a-f]{8,}|^(ember|ext-gen|mui-|react-select|ng-|gwt-|j_id|ctl00|yui_|:r|radix-)|__\w+_\d|[_-]\d+$",
    re.IGNORECASE)
SENSITIVE_WORDS = re.compile(r"password|passcode|pin|otp|cvv|card|account|balance|pan\b|aadhaar|ssn", re.I)

COLLECT_JS = r"""
(opts) => {
  const tidAttrs = opts.testIdAttrs, maskSel = opts.maskSelectors || [];
  const store = (window.__qafSpy = window.__qafSpy || []);
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  let targets;
  if (opts.indices) { targets = opts.indices.map(i => store[i]).filter(Boolean); }
  else { walk(document); targets = all; }
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const ROLE_OK = new Set(['button','link','checkbox','radio','tab','menuitem','menuitemcheckbox','menuitemradio',
    'option','switch','combobox','textbox','searchbox','slider','spinbutton','treeitem','gridcell','row','dialog','alert']);
  const implicitRole = (el) => {
    const t = el.tagName, type = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'A') return el.hasAttribute('href') ? 'link' : null;
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'INPUT') {
      if (['button','submit','reset','image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      if (['text','email','tel','url'].includes(type)) return 'textbox';
      return null;
    }
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'SELECT') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (/^H[1-6]$/.test(t)) return 'heading';
    if (t === 'TABLE') return 'table';
    if (t === 'IMG') return el.getAttribute('alt') ? 'img' : null;
    if (t === 'DIALOG') return 'dialog';
    return null;
  };
  const textOf = (id) => { const n = document.getElementById(id); return n ? norm(n.textContent) : ''; };
  const labelText = (el) => {
    if (el.labels && el.labels.length) {
      return norm([...el.labels].map(l => l.textContent).join(' '));
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return norm(lb.split(/\s+/).map(textOf).join(' '));
    return norm(el.getAttribute('aria-label'));
  };
  const accName = (el, role) => {
    const al = norm(el.getAttribute('aria-label')); if (al) return al;
    const lb = el.getAttribute('aria-labelledby'); if (lb) return norm(lb.split(/\s+/).map(textOf).join(' '));
    if (el.labels && el.labels.length) return labelText(el);
    if (el.tagName === 'INPUT' && ['button','submit','reset'].includes((el.type||'').toLowerCase())) return norm(el.value);
    if (el.tagName === 'INPUT' && el.type === 'image') return norm(el.alt);
    if (el.tagName === 'IMG') return norm(el.alt);
    if (['button','link','heading','tab','menuitem','option','checkbox','radio','switch','treeitem','gridcell'].includes(role))
      return norm(el.innerText || el.textContent).slice(0, 120);
    if (el.tagName === 'TABLE') { const c = el.querySelector('caption'); if (c) return norm(c.textContent); }
    return norm(el.getAttribute('title')) || norm(el.getAttribute('placeholder'));
  };
  const testId = (el) => { for (const a of tidAttrs) { const v = el.getAttribute(a); if (v) return [a, v]; } return null; };
  const isSensitive = (el) => (el.type === 'password') || maskSel.some(s => { try { return el.matches(s) || !!el.closest(s); } catch (e) { return false; } });
  const cssPath = (el) => {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 6) {
      if (n.id && /^[A-Za-z][\w-]*$/.test(n.id) && !/\d{4,}/.test(n.id)) { parts.unshift('#' + n.id); break; }
      let sel = n.tagName.toLowerCase();
      const p = n.parentElement;
      if (p) { const same = [...p.children].filter(c => c.tagName === n.tagName); if (same.length > 1) sel += `:nth-of-type(${same.indexOf(n) + 1})`; }
      parts.unshift(sel);
      n = p;
    }
    return parts.join(' > ');
  };
  const scopeOf = (el) => {
    const row = el.closest('tr, [role=row], li, [role=listitem]');
    if (!row || row === el) return null;
    const cells = [...row.querySelectorAll('td, th, [role=cell], [role=gridcell]')];
    const first = (cells.length ? cells : [row]).map(c => norm(c.innerText || c.textContent)).find(t => t && t.length <= 40);
    if (!first) return null;
    const isRow = row.tagName === 'TR' || row.getAttribute('role') === 'row';
    return { role: isRow ? 'row' : 'listitem', text: first, sensitive: isSensitive(row) };
  };
  const wanted = (el) => {
    if (el.closest('[data-qaf-ui]')) return false;
    if (!visible(el)) return false;
    const t = el.tagName;
    if (['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY'].includes(t)) return !(t === 'INPUT' && el.type === 'hidden') && !(t === 'A' && !el.hasAttribute('href'));
    const role = el.getAttribute('role');
    if (role && ROLE_OK.has(role)) return true;
    if (/^H[1-3]$/.test(t) || t === 'TABLE' || t === 'DIALOG') return true;
    if (testId(el)) return true;
    if (el.isContentEditable && el.hasAttribute('contenteditable')) return true;
    return false;
  };
  const out = [];
  for (const el of targets) {
    if (!opts.indices && !wanted(el)) continue;
    if (out.length >= (opts.limit || 400)) break;
    let idx = store.indexOf(el);
    if (idx < 0) { store.push(el); idx = store.length - 1; }
    const role = el.getAttribute('role') || implicitRole(el);
    const r = el.getBoundingClientRect();
    const isText = ['A','BUTTON','SUMMARY','H1','H2','H3','H4','LABEL','SPAN','TD','TH','LI','P','DIV','STRONG','B'].includes(el.tagName) && el.children.length < 4;
    out.push({
      idx, tag: el.tagName.toLowerCase(), type: el.getAttribute('type'), role,
      name: accName(el, role), label: ['INPUT','SELECT','TEXTAREA'].includes(el.tagName) ? labelText(el) : '',
      placeholder: norm(el.getAttribute('placeholder')), alt: norm(el.getAttribute('alt')), title: norm(el.getAttribute('title')),
      text: isText ? norm(el.innerText || el.textContent).slice(0, 120) : '',
      id: el.id || '', nameAttr: el.getAttribute('name') || '', testId: testId(el), sensitive: isSensitive(el),
      box: { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height },
      inShadow: el.getRootNode() instanceof ShadowRoot, cssPath: cssPath(el), scope: scopeOf(el),
      options: el.tagName === 'SELECT' ? [...el.options].slice(0, 15).map(o => norm(o.text)) : null,
    });
  }
  return out;
}
"""

INDEX_JS = "(els) => els.map(e => (window.__qafSpy || []).indexOf(e))"


@dataclass
class Candidate:
    expr: str
    strategy: str
    score: int
    count: int = 0
    unique: bool = False
    note: str = ""


@dataclass
class ElementResult:
    key: str
    tag: str
    role: str | None
    description: str
    suggested_name: str
    sensitive: bool
    frame: str | None
    box: dict[str, float]
    candidates: list[Candidate] = field(default_factory=list)
    best: str | None = None
    existing_ref: str | None = None
    fragile: bool = False


def _q(v: str) -> str:
    return json.dumps(v)


def _sensitive_text(text: str) -> bool:
    return bool(text) and (masker.mask(text) != text)


def _snake(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text or "").strip("_").lower()
    s = re.sub(r"_+", "_", s)[:40].strip("_")
    if s and s[0].isdigit():
        s = "el_" + s
    return s


_SUFFIX = {"button": "button", "link": "link", "checkbox": "checkbox", "radio": "option", "combobox": "select",
           "listbox": "list", "heading": "heading", "table": "grid", "grid": "grid", "tab": "tab", "dialog": "dialog",
           "img": "image", "searchbox": "search"}


def suggest_name(f: dict[str, Any]) -> str:
    base = ""
    for cand in (f.get("label"), f.get("name"), f.get("placeholder"),
                 (f.get("testId") or [None, None])[1], f.get("title"), f.get("text"), f.get("nameAttr"), f.get("id")):
        if cand and not _sensitive_text(cand):
            base = _snake(cand)
            if base:
                break
    suffix = _SUFFIX.get(f.get("role") or "")
    base = base or (suffix or f.get("tag") or "element")
    if suffix and not base.endswith(suffix):
        base = f"{base}_{suffix}"
    return base[:48]


def describe(f: dict[str, Any]) -> str:
    kind = f.get("role") or f.get("tag")
    label = next((x for x in (f.get("label"), f.get("name"), f.get("placeholder"), f.get("text")) if x), "")
    if f.get("sensitive") or _sensitive_text(label):
        label = masker.mask(label) if label else ""
    return f"{kind} {label!r}" if label else str(kind)


def build_candidates(f: dict[str, Any]) -> list[Candidate]:
    c: list[Candidate] = []
    sensitive = bool(f.get("sensitive"))
    role, tag = f.get("role"), f.get("tag")

    def text_ok(t: str) -> bool:
        return bool(t) and len(t) <= 80 and not (sensitive and tag not in ("input", "select", "textarea")) \
            and not _sensitive_text(t)

    if f.get("testId"):
        attr, val = f["testId"]
        dyn = bool(DYNAMIC.search(val))
        if attr == "data-testid":
            c.append(Candidate(f"page.get_by_test_id({_q(val)})", "test id", 100 - 40 * dyn,
                               note="value looks generated" if dyn else ""))
        else:
            c.append(Candidate(f"page.locator({_q(f'[{attr}={_q(val)}]')})", "test id", 98 - 40 * dyn))
    name = f.get("name") or ""
    if role and text_ok(name) and role not in ("table",):
        c.append(Candidate(f"page.get_by_role({_q(role)}, name={_q(name)}, exact=True)", "role and name", 90))
    if role == "table" and name:
        c.append(Candidate(f"page.get_by_role('table', name={_q(name)})", "role and name", 88))
    if f.get("label") and text_ok(f["label"]):
        c.append(Candidate(f"page.get_by_label({_q(f['label'])}, exact=True)", "label", 86))
    if f.get("placeholder") and text_ok(f["placeholder"]):
        c.append(Candidate(f"page.get_by_placeholder({_q(f['placeholder'])}, exact=True)", "placeholder", 80))
    if f.get("alt") and text_ok(f["alt"]):
        c.append(Candidate(f"page.get_by_alt_text({_q(f['alt'])}, exact=True)", "alt text", 76))
    if f.get("title") and text_ok(f["title"]):
        c.append(Candidate(f"page.get_by_title({_q(f['title'])}, exact=True)", "title", 74))
    if f.get("text") and text_ok(f["text"]) and tag not in ("input", "select", "textarea", "table"):
        c.append(Candidate(f"page.get_by_text({_q(f['text'])}, exact=True)", "text", 70))
    if f.get("id"):
        i = f["id"]
        dyn = bool(DYNAMIC.search(i))
        sel = f"#{i}" if re.fullmatch(r"[A-Za-z][\w-]*", i) else f"[id={_q(i)}]"
        c.append(Candidate(f"page.locator({_q(sel)})", "id", 64 - 40 * dyn, note="id looks generated" if dyn else ""))
    if f.get("nameAttr"):
        n = f["nameAttr"]
        dyn = bool(DYNAMIC.search(n))
        c.append(Candidate(f"page.locator({_q(f'{tag}[name={_q(n)}]')})", "name attribute", 60 - 40 * dyn))
    sc = f.get("scope")
    if sc and not sc.get("sensitive") and text_ok(sc.get("text", "")):
        inner = (f"get_by_role({_q(role)}, name={_q(name)}, exact=True)" if role and text_ok(name)
                 else f"get_by_test_id({_q(f['testId'][1])})" if f.get("testId") and f["testId"][0] == "data-testid"
                 else None)
        if inner:
            c.append(Candidate(f"page.get_by_role({_q(sc['role'])}).filter(has_text={_q(sc['text'])}).{inner}",
                               f"inside {sc['role']}", 58, note=f"scoped to the {sc['role']} showing '{sc['text']}'"))
    if role and not name:
        c.append(Candidate(f"page.get_by_role({_q(role)})", "role", 40))
    if f.get("cssPath"):
        c.append(Candidate(f"page.locator({_q(f['cssPath'])})", "CSS path", 12, note="structural CSS; breaks easily"))
    return c


def _frame_prefix(chain: list[str]) -> str:
    return "".join(f"frame_locator({_q(s)})." for s in chain)


def _with_frames(expr: str, chain: list[str]) -> str:
    return expr if not chain else "page." + _frame_prefix(chain) + expr[len("page."):]


def _root_for(page: Page, chain: list[str]) -> Any:
    root: Any = page
    for sel in chain:
        root = root.frame_locator(sel)
    return root


def verify_candidates(page: Page, chain: list[str], f: dict[str, Any], cands: list[Candidate]) -> list[Candidate]:
    target = f["idx"]
    root = _root_for(page, chain)
    good: list[Candidate] = []
    fallback: tuple[Candidate, list[int]] | None = None
    for c in cands:
        try:
            idxs = build_expr(c.expr, root).evaluate_all(INDEX_JS)
        except Exception:  # noqa: BLE001 - invalid guess, drop it
            continue
        c.count = len(idxs)
        if c.count == 1 and idxs[0] == target:
            c.unique = True
            good.append(c)
        elif target in idxs and c.count <= 25 and (fallback is None or c.score > fallback[0].score):
            fallback = (c, idxs)
    if fallback is not None and (not good or max(g.score for g in good) < fallback[0].score - 60):
        base, idxs = fallback
        good.append(Candidate(f"{base.expr}.nth({idxs.index(target)})", f"{base.strategy} + position",
                              max(base.score - 60, 5), 1, True,
                              "matches by position only; ask developers for a data-testid"))
    for c in good:
        c.expr = _with_frames(c.expr, chain)
    good.sort(key=lambda c: -c.score)
    return good[:4]


def _frame_selector(frame: Frame) -> str | None:
    el = frame.frame_element()
    attrs = el.evaluate("e => ({id: e.id, name: e.getAttribute('name'), title: e.getAttribute('title'),"
                        " src: e.getAttribute('src')})")
    parent = frame.parent_frame
    for sel in ([f"iframe[name={_q(attrs['name'])}]"] if attrs.get("name") else []) + \
               ([f"#{attrs['id']}"] if attrs.get("id") and not DYNAMIC.search(attrs["id"]) else []) + \
               ([f"iframe[title={_q(attrs['title'])}]"] if attrs.get("title") else []) + \
               ([f"iframe[src*={_q(attrs['src'].split('?')[0][-60:])}]"] if attrs.get("src") else []):
        try:
            if parent is not None and parent.locator(sel).count() == 1:
                return sel
        except Exception:  # noqa: BLE001
            continue
    return None


def _frames(page: Page) -> list[tuple[Frame, list[str]]]:
    out = [(page.main_frame, [])]

    def visit(frame: Frame, chain: list[str], depth: int) -> None:
        for child in frame.child_frames:
            if depth >= 2 or child.is_detached():
                continue
            sel = _frame_selector(child)
            if sel is None:
                continue
            out.append((child, [*chain, sel]))
            visit(child, [*chain, sel], depth + 1)

    visit(page.main_frame, [], 0)
    return out


def analyse(page: Page, frame: Frame, chain: list[str], facts: list[dict[str, Any]],
            known: dict[str, str], frame_no: int = 0) -> list[ElementResult]:
    results = []
    for f in facts:
        cands = verify_candidates(page, chain, f, build_candidates(f))
        best = cands[0].expr if cands else None
        existing = None
        for c in cands:
            try:
                existing = known.get(canonical(c.expr))
            except Exception:  # noqa: BLE001
                existing = None
            if existing:
                break
        results.append(ElementResult(
            key=f"{frame_no}:{f['idx']}", tag=f["tag"], role=f.get("role"), description=describe(f),
            suggested_name=suggest_name(f),
            sensitive=bool(f.get("sensitive")) or bool(SENSITIVE_WORDS.search(f.get("label") or f.get("name") or "")),
            frame=" > ".join(chain) or None, box=f["box"], candidates=cands, best=best, existing_ref=existing,
            fragile=bool(cands) and cands[0].score < 50))
    return results


def _dedupe_names(results: list[ElementResult]) -> None:
    seen: dict[str, int] = {}
    for r in results:
        n = r.suggested_name
        if n in seen:
            seen[n] += 1
            r.suggested_name = f"{n}_{seen[n]}"
        else:
            seen[n] = 1


def suggest_class(page: Page) -> str:
    try:
        heading = page.locator("h1").first.inner_text(timeout=1000)
    except Exception:  # noqa: BLE001
        heading = ""
    base = heading or page.title() or "New"
    words = re.findall(r"[A-Za-z0-9]+", base)[:4]
    name = "".join(w[:1].upper() + w[1:].lower() for w in words) or "New"
    if name[0].isdigit():
        name = "P" + name
    return name if name.endswith("Page") else name + "Page"


def scan_page(page: Page, out_dir: Path, known: dict[str, str], mask_selectors: list[str],
              test_id_attrs: list[str] | None = None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:  # noqa: BLE001 - long-polling apps never go idle; carry on
        pass
    started = time.monotonic()
    opts = {"testIdAttrs": test_id_attrs or TEST_ID_ATTRS, "maskSelectors": mask_selectors}
    results: list[ElementResult] = []
    for frame_no, (frame, chain) in enumerate(_frames(page)):
        facts = frame.evaluate(COLLECT_JS, opts)
        results.extend(analyse(page, frame, chain, facts, known, frame_no))
    _dedupe_names(results)
    shot = out_dir / "page.png"
    page.screenshot(path=str(shot), full_page=True,
                    mask=[page.locator(s) for s in mask_selectors], mask_color="#13303A", animations="disabled")
    data = {
        "url": masker.mask(page.url), "title": page.title(), "suggested_class": suggest_class(page),
        "suggested_path": urlparse(page.url).path or "/",
        "screenshot": shot.name, "seconds": round(time.monotonic() - started, 1),
        "elements": [asdict(r) for r in results],
        "stats": {"elements": len(results), "unique": sum(1 for r in results if r.best),
                  "fragile": sum(1 for r in results if r.fragile), "already_in_pom": sum(1 for r in results if r.existing_ref)},
    }
    (out_dir / "result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ------------------------------------------------------------------ pick mode
PICK_OVERLAY_JS = r"""
() => {
  if (window.__qafPickInstalled) return;
  window.__qafPickInstalled = true;
  window.__qafPicked = window.__qafPicked || [];
  const store = (window.__qafSpy = window.__qafSpy || []);
  const install = () => {
    if (document.querySelector('[data-qaf-ui]')) return;
    const bar = document.createElement('div');
    bar.setAttribute('data-qaf-ui', '1');
    bar.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#13303A;color:#fff;' +
      'font:14px Segoe UI,system-ui,sans-serif;padding:10px 12px;border-radius:6px;display:flex;gap:8px;align-items:center';
    bar.innerHTML = '<span id="qaf-msg">QA Forge spy</span>' +
      '<button id="qaf-pick">Pick element</button><button id="qaf-scan">Scan this page</button>' +
      '<button id="qaf-done">Finish</button>';
    for (const b of bar.querySelectorAll('button')) b.style.cssText = 'font:inherit;padding:4px 10px;border:0;border-radius:4px;background:#D9B44A;color:#13303A;cursor:pointer';
    document.documentElement.appendChild(bar);
    const box = document.createElement('div');
    box.setAttribute('data-qaf-ui', '1');
    box.style.cssText = 'position:fixed;pointer-events:none;border:2px solid #D9B44A;background:rgba(217,180,74,.15);z-index:2147483646;display:none';
    document.documentElement.appendChild(box);
    let picking = false;
    const msg = (t) => { bar.querySelector('#qaf-msg').textContent = t; };
    bar.querySelector('#qaf-pick').onclick = (e) => { e.stopPropagation(); picking = !picking; msg(picking ? 'Click an element' : 'QA Forge spy'); };
    bar.querySelector('#qaf-scan').onclick = (e) => { e.stopPropagation(); window.__qafScanReq = true; msg('Scanning...'); };
    bar.querySelector('#qaf-done').onclick = (e) => { e.stopPropagation(); window.__qafDone = true; msg('Done - you can close this window'); };
    document.addEventListener('mousemove', (e) => {
      if (!picking) { box.style.display = 'none'; return; }
      const el = document.elementFromPoint(e.clientX, e.clientY);
      if (!el || el.closest('[data-qaf-ui]')) return;
      const r = el.getBoundingClientRect();
      Object.assign(box.style, { display: 'block', left: r.x + 'px', top: r.y + 'px', width: r.width + 'px', height: r.height + 'px' });
    }, true);
    document.addEventListener('click', (e) => {
      if (!picking || e.target.closest('[data-qaf-ui]')) return;
      e.preventDefault(); e.stopPropagation();
      const el = e.target;
      let idx = store.indexOf(el); if (idx < 0) { store.push(el); idx = store.length - 1; }
      window.__qafPicked.push(idx);
      picking = false; box.style.display = 'none';
      msg('Captured ' + (el.getAttribute('aria-label') || el.innerText || el.tagName).slice(0, 30));
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
}
"""

POLL_JS = """() => { const p = window.__qafPicked || []; window.__qafPicked = [];
  const scan = window.__qafScanReq === true; window.__qafScanReq = false;
  return {picked: p, scan, done: window.__qafDone === true}; }"""


def pick_session(page: Page, out_dir: Path, known: dict[str, str], mask_selectors: list[str],
                 max_minutes: float = 30, log: Any = print) -> dict[str, Any]:
    """Headed: tester picks elements / scans pages while navigating; results accumulate."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page.add_init_script(f"({PICK_OVERLAY_JS})()")
    page.evaluate(PICK_OVERLAY_JS)
    opts = {"testIdAttrs": TEST_ID_ATTRS, "maskSelectors": mask_selectors}
    results: list[ElementResult] = []
    deadline = time.monotonic() + max_minutes * 60
    shot_name = None
    while time.monotonic() < deadline and not page.is_closed():
        try:
            state = page.evaluate(POLL_JS)
        except Exception:  # noqa: BLE001 - navigation in progress
            time.sleep(0.4)
            continue
        if state["picked"]:
            facts = page.evaluate(COLLECT_JS, {**opts, "indices": state["picked"]})
            new = analyse(page, page.main_frame, [], facts, known)
            results.extend(new)
            for r in new:
                log(f"Captured {r.description}: {r.best or 'no unique locator'}")
        if state["scan"]:
            facts = page.evaluate(COLLECT_JS, opts)
            results.extend(analyse(page, page.main_frame, [], facts, known))
            log(f"Scanned {page.url}: {len(facts)} elements")
        if state["picked"] or state["scan"]:
            shot_name = "page.png"
            page.screenshot(path=str(out_dir / shot_name), full_page=True,
                            mask=[page.locator(s) for s in mask_selectors], mask_color="#13303A")
        if state["done"]:
            break
        time.sleep(0.3)
    _dedupe_names(results)
    url = masker.mask(page.url) if not page.is_closed() else ""
    data = {"url": url, "title": "" if page.is_closed() else page.title(),
            "suggested_class": "NewPage" if page.is_closed() else suggest_class(page),
            "suggested_path": "/", "screenshot": shot_name, "seconds": 0, "mode": "pick",
            "elements": [asdict(r) for r in results],
            "stats": {"elements": len(results), "unique": sum(1 for r in results if r.best),
                      "fragile": sum(1 for r in results if r.fragile),
                      "already_in_pom": sum(1 for r in results if r.existing_ref)}}
    (out_dir / "result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
