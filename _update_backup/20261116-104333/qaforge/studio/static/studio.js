/* QA Forge Studio - browser side. Plain JavaScript, no external libraries or CDNs. */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const csrf = () => ($('meta[name="csrf"]') || {}).content || "";

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }

  async function api(url, body) {
    const res = await fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: JSON.stringify(body || {}) });
    let data = {};
    try { data = await res.json(); } catch (e) { data = { ok: false, error: "Studio did not answer (" + res.status + ")" }; }
    if (!res.ok && data.ok === undefined) data.ok = false;
    return data;
  }
  const readJSON = (id) => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };
  const human = (s) => { const t = String(s || "").replace(/_/g, " ").trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
  const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, "t_$1").slice(0, 60);

  /* ------------------------------------------------------------ small page helpers */
  $$("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));
  $$("[data-checkall]").forEach((box) => box.addEventListener("change", () => {
    $$(`input[name="${box.dataset.checkall}"]`).forEach((c) => { if (c.closest("tr").style.display !== "none") c.checked = box.checked; });
    countRun();
  }));
  function countRun() {
    const btn = $("[data-countlabel]");
    if (!btn) return;
    const n = $$('input[name="nodeid"]:checked').length;
    btn.textContent = n ? `Run ${n} test${n === 1 ? "" : "s"}` : btn.dataset.countlabel;
    btn.disabled = !n;
  }
  $$('input[name="nodeid"]').forEach((c) => c.addEventListener("change", countRun));
  countRun();
  function applyFilters() {
    const q = $("[data-filter]"), t = $("[data-tagfilter]");
    const sel = (q && q.dataset.filter) || (t && t.dataset.tagfilter);
    if (!sel) return;
    const text = q ? q.value.trim().toLowerCase() : "", tag = t ? t.value : "";
    $$(sel).forEach((tr) => {
      const ok = (!text || tr.dataset.text.includes(text)) && (!tag || (" " + tr.dataset.tags + " ").includes(" " + tag + " "));
      tr.style.display = ok ? "" : "none";
    });
  }
  $$("[data-filter],[data-tagfilter]").forEach((el) => el.addEventListener("input", applyFilters));
  $$("[data-sample]").forEach((btn) => btn.addEventListener("click", async () => {
    const out = $(btn.dataset.target);
    const res = await fetch(`/api/factories/${btn.dataset.sample}/sample`, { credentials: "same-origin" });
    out.textContent = JSON.stringify(await res.json(), null, 2);
    out.hidden = false;
  }));
  // env -> app -> role cascading selects (spy, recorder, health check)
  const envCfg = readJSON("env-data");
  if (envCfg) {
    const env = $("#f-env"), app = $("#f-app"), role = $("#f-role");
    const fill = (sel, values, keep) => {
      const cur = keep || sel.value; sel.innerHTML = "";
      if (sel === role) sel.append(h("option", { value: "", text: "No sign-in" }));
      values.forEach((v) => sel.append(h("option", { value: v, text: v, selected: v === cur })));
    };
    const sync = () => {
      const e = envCfg[env.value] || { apps: [], roles: [] };
      fill(app, e.apps, app.dataset.keep); fill(role, e.roles, role.dataset.keep);
      app.dataset.keep = ""; role.dataset.keep = "";
    };
    env.addEventListener("change", sync); sync();
  }

  /* ------------------------------------------------------------ job console */
  const jobEl = $("#job");
  if (jobEl) {
    const out = $("#console"), stamp = $("#job-stamp");
    let offset = 0, auto = true;
    out.addEventListener("scroll", () => { auto = out.scrollTop + out.clientHeight >= out.scrollHeight - 30; });
    const tick = async () => {
      const res = await fetch(`/api/jobs/${jobEl.dataset.job}?offset=${offset}`, { credentials: "same-origin" });
      if (!res.ok) { setTimeout(tick, 3000); return; }
      const d = await res.json();
      if (d.text) { out.textContent += d.text; if (auto) out.scrollTop = out.scrollHeight; }
      offset = d.offset;
      stamp.textContent = d.status; stamp.className = "stamp big " + d.status;
      if (["passed", "failed", "error", "cancelled"].includes(d.status)) {
        $$(".while-running").forEach((x) => x.remove());
        if (d.result_url) {
          const link = $("#result-link"); link.href = d.result_url; link.hidden = false;
          if (jobEl.dataset.autogo === "1" && d.status !== "cancelled") window.location = d.result_url;
        }
        return;
      }
      setTimeout(tick, 1000);
    };
    tick();
  }

  /* ------------------------------------------------------------ generic table editor */
  const tableEl = $("#table-editor");
  if (tableEl) {
    const cfg = readJSON("table-data");
    let cols = cfg.columns.slice(), rows = cfg.rows.map((r) => Object.assign({}, r));
    const fixed = cfg.kind === "mapping";
    const msg = $("#table-msg");
    function render() {
      tableEl.innerHTML = "";
      const head = h("tr", {}, h("th", { class: "num" }, "#"),
        cols.map((c, ci) => h("th", {}, fixed ? human(c) : h("div", { class: "withref" },
          h("input", { type: "text", value: c, "aria-label": "Column name", oninput: (e) => {
            const old = cols[ci], nu = e.target.value; cols[ci] = nu;
            rows.forEach((r) => { r[nu] = r[old]; if (nu !== old) delete r[old]; });
          } }),
          h("button", { type: "button", title: "Remove column", text: "✕", onclick: () => { const c0 = cols[ci]; cols.splice(ci, 1); rows.forEach((r) => delete r[c0]); render(); } })))),
        h("th", {}, ""));
      tableEl.append(head);
      rows.forEach((r, ri) => tableEl.append(h("tr", {}, h("td", { class: "num", text: ri + 1 }),
        cols.map((c) => h("td", {}, c === "rule" && fixed
          ? h("select", { onchange: (e) => { r.rule = e.target.value; } }, cfg.rules.map((x) => h("option", { value: x, text: x, selected: (r.rule || "equals") === x })))
          : h("input", { type: "text", value: r[c] || "", "aria-label": c, oninput: (e) => { r[c] = e.target.value; } }))),
        h("td", {}, h("button", { type: "button", class: "btn quiet danger", text: "Remove", onclick: () => { rows.splice(ri, 1); render(); } })))));
    }
    $("#add-row").addEventListener("click", () => { rows.push({}); render(); });
    const addCol = $("#add-col");
    if (addCol) addCol.addEventListener("click", () => { let n = "column" + (cols.length + 1); cols.push(n); render(); });
    $("#table-save").addEventListener("click", async () => {
      const titleEl = $("#table-title");
      const d = await api(cfg.save_url, { columns: cols, rows: rows, title: titleEl ? titleEl.value : undefined });
      msg.textContent = d.ok ? "Saved." : d.error; msg.className = d.ok ? "muted" : "errors";
    });
    render();
  }

  /* ------------------------------------------------------------ test designer */
  const editor = $("#editor");
  if (editor) specEditor(editor);

  function specEditor(root) {
    const cat = readJSON("catalog-data");
    const spec = readJSON("spec-data");
    spec.steps = spec.steps || []; spec.data = spec.data || []; spec.jira = spec.jira || []; spec.tags = spec.tags || [];
    const isNew = root.dataset.new === "1", readonly = root.dataset.readonly === "1";
    const KW = Object.fromEntries(cat.keywords.map((k) => [k.name, k]));
    let idTouched = !isNew || !!spec.id;
    let errors = [];

    const pagesByRef = Object.fromEntries(cat.pages.map((p) => [p.ref, p]));
    function pageSelect(value, onchange) {
      const sel = h("select", { onchange: (e) => onchange(e.target.value), "aria-label": "Page" }, h("option", { value: "", text: "Choose a page" }));
      const byApp = {};
      cat.pages.forEach((p) => (byApp[p.app] = byApp[p.app] || []).push(p));
      Object.entries(byApp).forEach(([app, ps]) => sel.append(h("optgroup", { label: human(app) },
        ps.map((p) => h("option", { value: p.ref, text: p.title + (p.path ? "" : " (reached by clicking)"), selected: p.ref === value })))));
      return sel;
    }
    function elementSelect(step, wantGrid, rerender) {
      const value = step.locator ? "__locator__" : (step.target || "");
      const sel = h("select", { "aria-label": "Screen element", onchange: (e) => {
        if (e.target.value === "__locator__") { step.target = null; step.locator = step.locator || ""; }
        else { step.target = e.target.value || null; step.locator = null; }
        rerender();
      } }, h("option", { value: "", text: "Choose an element" }));
      cat.pages.forEach((p) => {
        const els = p.elements.filter((e) => e.kind !== "other" && (!wantGrid || e.kind === "component" || /grid|table/.test(e.name)));
        if (!els.length) return;
        sel.append(h("optgroup", { label: `${human(p.app)} › ${p.title}` },
          els.map((e) => h("option", { value: `${p.ref}.${e.name}`, text: human(e.name) + (e.component ? ` (${e.component})` : ""),
            selected: `${p.ref}.${e.name}` === value, title: e.locator || "" }))));
      });
      sel.append(h("option", { value: "__locator__", text: "Type or paste a locator…", selected: value === "__locator__" }));
      if (step.target && !sel.querySelector(`option[value="${CSS.escape(step.target)}"]`))
        sel.append(h("option", { value: step.target, text: `Missing: ${step.target}`, selected: true }));
      return sel;
    }
    function locatorHint(step) {
      if (!step.target) return null;
      const ref = step.target, pageRef = ref.slice(0, ref.lastIndexOf(".")), name = ref.slice(ref.lastIndexOf(".") + 1);
      const p = pagesByRef[pageRef]; const e = p && p.elements.find((x) => x.name === name);
      return e && e.locator ? h("div", { class: "loc small", title: "Locator from the page object" }, e.locator) : null;
    }

    function refsBefore(index) {
      const groups = [];
      const data = [];
      spec.data.forEach((d) => { if (!d.name) return; data.push("${" + d.name + "}");
        (cat.factories[d.factory] || []).forEach((f) => data.push("${" + d.name + "." + f + "}")); });
      if (data.length) groups.push(["Test data", data]);
      if (spec.data_file) groups.push(["Data row", (cat.data_files[spec.data_file] || []).map((c) => "${row." + c + "}")]);
      const saved = [];
      spec.steps.slice(0, index < 0 ? spec.steps.length : index).forEach((s) => {
        if (s.save_as) saved.push("${" + s.save_as + "}");
        if (s.action === "generate_data" && s.save_as && s.options && cat.factories[s.options.factory])
          cat.factories[s.options.factory].forEach((f) => saved.push("${" + s.save_as + "." + f + "}"));
      });
      if (saved.length) groups.push(["Remembered by earlier steps", saved]);
      groups.push(["Generated on the spot", cat.generators.map((g) => "${gen." + g + "}")]);
      groups.push(["Other", ["${run_tag}", "${today}", "${secret:SECRET_NAME}"]]);
      return groups;
    }
    function withRef(input, index) {
      if (readonly) return input;
      const btn = h("button", { type: "button", text: "${}", title: "Insert a value", "aria-label": "Insert a value" });
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeMenus();
        const menu = h("div", { class: "refmenu", role: "menu" });
        refsBefore(index).forEach(([title, items]) => {
          menu.append(h("h4", { text: title }));
          items.forEach((it) => menu.append(h("button", { type: "button", text: it, onclick: () => {
            const s = input.selectionStart ?? input.value.length, e2 = input.selectionEnd ?? input.value.length;
            input.value = input.value.slice(0, s) + it + input.value.slice(e2);
            input.dispatchEvent(new Event("input")); closeMenus(); input.focus();
          } })));
        });
        const r = btn.getBoundingClientRect();
        menu.style.left = (window.scrollX + r.left - 200) + "px"; menu.style.top = (window.scrollY + r.bottom + 4) + "px";
        document.body.append(menu);
      });
      return h("div", { class: "withref" }, input, btn);
    }
    function closeMenus() { $$(".refmenu").forEach((m) => m.remove()); }
    document.addEventListener("click", closeMenus);

    function kvText(v) { return v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k}=${x}`).join("\n") : (v || ""); }
    function kvParse(t) { const o = {}; String(t).split(/\n/).forEach((l) => { const i = l.indexOf("="); if (i > 0) o[l.slice(0, i).trim()] = l.slice(i + 1).trim(); }); return o; }

    function optionControl(step, opt, index, rerender) {
      step.options = step.options || {};
      const val = step.options[opt.name] ?? opt.default ?? "";
      const set = (v) => { if (v === "" || v === null || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length)) delete step.options[opt.name]; else step.options[opt.name] = v; };
      const label = (ctrl) => h("label", {}, opt.label + (opt.required ? "" : " (optional)"), ctrl);
      const sel = (items, cb) => { const s = h("select", { onchange: (e) => { set(e.target.value); cb && cb(); } },
        h("option", { value: "", text: opt.required ? "Choose…" : "None" }),
        items.map(([v, t]) => h("option", { value: v, text: t, selected: String(val) === String(v) }))); return s; };
      switch (opt.kind) {
        case "bool": return h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!val, onchange: (e) => set(e.target.checked ? true : "") }), opt.label);
        case "number": return label(h("input", { type: "number", value: val, min: 0, oninput: (e) => set(e.target.value === "" ? "" : Number(e.target.value)) }));
        case "select": return label(h("select", { onchange: (e) => set(e.target.value) }, opt.choices.map((c) => h("option", { value: c, text: c, selected: (val || opt.default) === c }))));
        case "app": return label(sel(Object.keys(cat.apps).map((a) => [a, a]), rerender));
        case "role": { const roles = (step.options.app && cat.apps[step.options.app]) || [...new Set(Object.values(cat.apps).flat())];
          return label(sel(roles.map((r) => [r, r]))); }
        case "factory": return label(sel(Object.keys(cat.factories).map((f) => [f, f])));
        case "mapping": return label(sel(cat.mappings.map((m) => [m, human(m)])));
        case "spec": return label(sel(cat.specs.filter((s) => s.reusable && s.id !== spec.id).map((s) => [s.id, s.title])));
        case "flow": {
          const box = h("div", {}, label(sel(cat.flows.map((f) => [f.ref, `${human(f.app)} › ${human(f.method)}`]), rerender)));
          const f = cat.flows.find((x) => x.ref === val);
          if (f) box.append(h("div", { class: "help" }, (f.doc ? f.doc + " " : "") + "Inputs: " + (f.params.map((p) => `${p.name}${p.type ? " (" + p.type + ")" : ""}`).join(", ") || "none")));
          return box;
        }
        case "kv": { const ta = h("textarea", { rows: 3, placeholder: "field=value", oninput: (e) => set(kvParse(e.target.value)) }); ta.value = kvText(val);
          return label(withRef(ta, index)); }
        case "rows": {
          const rows = Array.isArray(val) ? val : [];
          const tbl = h("table", { class: "minirows" });
          const draw = () => {
            tbl.innerHTML = "";
            tbl.append(h("tr", {}, h("td", { class: "small muted", text: "Field" }), h("td", { class: "small muted", text: "Must be" }), h("td", { class: "small muted", text: "Compare as" }), h("td", {})));
            rows.forEach((r, ri) => {
              const exp = h("input", { type: "text", value: r.expected ?? "", "aria-label": "Expected", oninput: (e) => { r.expected = e.target.value; } });
              tbl.append(h("tr", {}, h("td", {}, h("input", { type: "text", value: r.field || "", "aria-label": "Field", oninput: (e) => { r.field = e.target.value; } })),
                h("td", {}, withRef(exp, index)),
                h("td", {}, h("select", { onchange: (e) => { r.rule = e.target.value; } }, cat.rules.map((x) => h("option", { value: x, text: x, selected: (r.rule || "equals") === x })))),
                h("td", {}, h("button", { type: "button", class: "btn quiet danger", text: "✕", "aria-label": "Remove field", onclick: () => { rows.splice(ri, 1); set(rows.length ? rows : ""); draw(); } }))));
            });
            tbl.append(h("tr", {}, h("td", { colspan: 4 }, h("button", { type: "button", class: "btn quiet", text: "Add field",
              onclick: () => { rows.push({ field: "", expected: "", rule: "equals" }); set(rows); draw(); } }))));
          };
          draw();
          return h("div", { style: "grid-column: 1 / -1" }, h("label", {}, opt.label + (opt.required ? "" : " (optional)")), tbl, opt.help ? h("div", { class: "help", text: opt.help }) : null);
        }
        default: { const inp = h("input", { type: "text", value: val, oninput: (e) => set(e.target.value) });
          return label(["key", "equals", "expected"].includes(opt.name) ? withRef(inp, index) : inp); }
      }
    }

    function stepCard(step, i) {
      const k = KW[step.action] || { label: step.action, options: [], help: "Unknown action" };
      const rerender = () => renderSteps();
      const actionSel = h("select", { "aria-label": "Action", onchange: (e) => {
        const nk = KW[e.target.value]; step.action = e.target.value;
        if (!nk.target) { step.target = null; step.locator = null; }
        if (nk.target === "page") step.locator = null;
        if (!nk.value) delete step.value;
        if (!nk.save_as) delete step.save_as;
        const keep = new Set(nk.options.map((o) => o.name));
        step.options = Object.fromEntries(Object.entries(step.options || {}).filter(([n]) => keep.has(n)));
        rerender();
      } }, cat.groups.map((g) => h("optgroup", { label: g }, cat.keywords.filter((x) => x.group === g).map((x) => h("option", { value: x.name, text: x.label, selected: x.name === step.action })))));
      const fields = h("div", { class: "fields" });
      if (k.target === "page") fields.append(h("label", {}, "Page", pageSelect(step.target, (v) => { step.target = v || null; })));
      else if (k.target) {
        fields.append(h("label", {}, k.target === "grid" ? "Table" : "Screen element", elementSelect(step, k.target === "grid", rerender), locatorHint(step)));
        if (step.locator !== null && step.locator !== undefined) {
          const li = h("input", { type: "text", value: step.locator, class: "mono", placeholder: 'page.get_by_role("button", name="Save")', oninput: (e) => { step.locator = e.target.value; } });
          fields.append(h("label", { style: "grid-column: 1 / -1" }, "Locator (Playwright)", li));
        }
      }
      if (k.value) { const vi = h("input", { type: "text", value: step.value ?? "", placeholder: k.value, oninput: (e) => { step.value = e.target.value; } });
        fields.append(h("label", {}, k.value, withRef(vi, i))); }
      k.options.forEach((o) => fields.append(optionControl(step, o, i, rerender)));
      if (k.save_as) fields.append(h("label", {}, "Remember as" + (k.save_as === "required" ? "" : " (optional)"),
        h("input", { type: "text", value: step.save_as || "", placeholder: "e.g. txn_ref", oninput: (e) => { step.save_as = e.target.value.trim(); } })));
      const errs = errors.filter((e) => e.step === i);
      const card = h("div", { class: "step" + (step.enabled === false ? " off" : "") + (errs.length ? " bad" : ""), "data-step": i },
        h("div", { class: "n", text: i + 1 }),
        h("div", { class: "body" },
          h("div", { class: "line" }, actionSel, fields),
          h("div", { class: "help", text: k.help }),
          h("div", { class: "more" },
            h("input", { type: "text", value: step.description || "", placeholder: "Describe this step in the evidence (optional)", oninput: (e) => { step.description = e.target.value; } }),
            h("label", { class: "check" }, h("input", { type: "checkbox", checked: step.enabled === false, onchange: (e) => { step.enabled = !e.target.checked; card.classList.toggle("off", !step.enabled); } }), "Skip this step")),
          errs.map((e) => h("div", { class: "errors small", text: e.message }))),
        h("div", { class: "tools" },
          h("button", { type: "button", title: "Move up", "aria-label": "Move up", text: "↑", onclick: () => { if (i > 0) { [spec.steps[i - 1], spec.steps[i]] = [spec.steps[i], spec.steps[i - 1]]; rerender(); } } }),
          h("button", { type: "button", title: "Move down", "aria-label": "Move down", text: "↓", onclick: () => { if (i < spec.steps.length - 1) { [spec.steps[i + 1], spec.steps[i]] = [spec.steps[i], spec.steps[i + 1]]; rerender(); } } }),
          h("button", { type: "button", title: "Insert a step below", "aria-label": "Insert a step below", text: "+", onclick: () => { spec.steps.splice(i + 1, 0, { action: "click", options: {} }); rerender(); } }),
          h("button", { type: "button", title: "Duplicate", "aria-label": "Duplicate", text: "⧉", onclick: () => { spec.steps.splice(i + 1, 0, JSON.parse(JSON.stringify(step))); rerender(); } }),
          h("button", { type: "button", title: "Delete step", "aria-label": "Delete step", text: "✕", onclick: () => { spec.steps.splice(i, 1); rerender(); } })));
      if (readonly) $$("input,select,textarea,button", card).forEach((x) => (x.disabled = true));
      return card;
    }

    const header = h("section", { class: "sheet" });
    const dataBox = h("section", { class: "sheet" });
    const stepsBox = h("div", { class: "steps" });
    const globalErr = h("div", { class: "errors", role: "alert" });

    function renderHeader() {
      header.innerHTML = "";
      const title = h("input", { type: "text", value: spec.title || "", id: "s-title", oninput: (e) => {
        spec.title = e.target.value; if (!idTouched) { spec.id = slug(spec.title); idInput.value = spec.id; } } });
      const idInput = h("input", { type: "text", value: spec.id || "", id: "s-id", class: "mono", readonly: !isNew,
        oninput: (e) => { idTouched = true; spec.id = e.target.value.trim(); } });
      header.append(
        h("div", { class: "row" },
          h("div", { style: "flex: 3 1 320px" }, h("label", { class: "f", for: "s-title" }, "Test name"), title),
          h("div", {}, h("label", { class: "f", for: "s-id" }, "Test id" + (isNew ? "" : " (fixed)")), idInput)),
        h("div", { class: "row" },
          h("div", {}, h("label", { class: "f", for: "s-jira" }, "JIRA test cases"),
            h("input", { type: "text", id: "s-jira", value: spec.jira.join(", "), placeholder: "BANK-123", oninput: (e) => { spec.jira = e.target.value.split(/[\s,]+/).filter(Boolean).map((x) => x.toUpperCase()); } })),
          h("div", {}, h("label", { class: "f", for: "s-tags" }, "Tags"),
            h("input", { type: "text", id: "s-tags", value: spec.tags.join(", "), placeholder: "smoke, regression", oninput: (e) => { spec.tags = e.target.value.split(/[\s,]+/).filter(Boolean).map((x) => x.toLowerCase()); } })),
          h("div", {}, h("label", { class: "f", for: "s-file" }, "Run once per row of"),
            h("select", { id: "s-file", onchange: (e) => { spec.data_file = e.target.value || null; } },
              h("option", { value: "", text: "No data file (runs once)" }),
              Object.keys(cat.data_files).map((f) => h("option", { value: f, text: f, selected: f === spec.data_file })))),
          h("div", { class: "tight" }, h("label", { class: "check", style: "margin-top:28px" },
            h("input", { type: "checkbox", checked: !!spec.reusable, onchange: (e) => { spec.reusable = e.target.checked; } }), "Reusable steps"))),
        h("label", { class: "f", for: "s-desc" }, "What this test proves"),
        h("input", { type: "text", id: "s-desc", value: spec.description || "", oninput: (e) => { spec.description = e.target.value; } }));
    }

    function renderData() {
      dataBox.innerHTML = "";
      dataBox.append(h("h2", { style: "margin-top:0" }, "Test data"),
        h("p", { class: "muted small" }, "Generated fresh for every run (synthetic, format-valid, traceable). Use it in steps as ${name.field}."));
      const tbl = h("table", { class: "grid" }, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Kind of data"), h("th", {}, "Fixed values (field=value per line)"), h("th", {}, "")));
      spec.data.forEach((d, di) => {
        const ta = h("textarea", { rows: 2, "aria-label": "Fixed values", oninput: (e) => { d.overrides = kvParse(e.target.value); } }); ta.value = kvText(d.overrides);
        tbl.append(h("tr", {},
          h("td", {}, h("input", { type: "text", value: d.name || "", "aria-label": "Data name", oninput: (e) => { d.name = e.target.value.trim(); } })),
          h("td", {}, h("select", { "aria-label": "Kind of data", onchange: (e) => { d.factory = e.target.value; } }, Object.keys(cat.factories).map((f) => h("option", { value: f, text: f, selected: f === d.factory }))),
            h("div", { class: "muted small" }, "Fields: " + (cat.factories[d.factory] || []).join(", "))),
          h("td", {}, withRef(ta, -1)),
          h("td", {}, h("button", { type: "button", class: "btn quiet danger", text: "Remove", onclick: () => { spec.data.splice(di, 1); renderData(); } }))));
      });
      dataBox.append(tbl, h("div", { class: "btns", style: "margin-top:8px" }, h("button", { type: "button", class: "btn", text: "Add test data",
        onclick: () => { const f = Object.keys(cat.factories)[0]; spec.data.push({ name: slug(f), factory: f, overrides: {} }); renderData(); } })));
      if (readonly) $$("input,select,textarea,button", dataBox).forEach((x) => (x.disabled = true));
    }

    function renderSteps() {
      stepsBox.innerHTML = "";
      spec.steps.forEach((s, i) => stepsBox.append(stepCard(s, i)));
      if (!readonly) {
        const pick = h("select", { "aria-label": "New step action" }, cat.groups.map((g) => h("optgroup", { label: g }, cat.keywords.filter((x) => x.group === g).map((x) => h("option", { value: x.name, text: x.label })))));
        pick.value = "click";
        stepsBox.append(h("div", { class: "addstep" }, pick, h("button", { type: "button", class: "btn", text: "Add step", onclick: () => {
          spec.steps.push({ action: pick.value, options: {} }); renderSteps(); stepsBox.querySelector(`[data-step="${spec.steps.length - 1}"] select`).focus(); } })));
      }
    }

    function payload() {
      const clean = JSON.parse(JSON.stringify(spec));
      clean.steps = clean.steps.map((s) => {
        const o = { action: s.action };
        ["target", "locator", "value", "save_as", "description"].forEach((k) => { if (s[k] !== undefined && s[k] !== null && s[k] !== "") o[k] = s[k]; });
        if (s.locator === "") o.locator = "";
        if (s.options && Object.keys(s.options).length) o.options = s.options;
        if (s.enabled === false) o.enabled = false;
        return o;
      });
      clean.data = clean.data.filter((d) => d.name);
      ["updated_by", "updated_at"].forEach((k) => delete clean[k]);
      if (!clean.data_file) delete clean.data_file;
      return clean;
    }

    async function save(runEnv) {
      const msg = $("#save-msg");
      msg.textContent = "Saving…"; msg.className = "muted";
      const body = Object.assign(payload(), { _new: isNew && !root.dataset.saved, _run_env: runEnv || undefined });
      const id = body.id || "new";
      const d = await api(`/api/specs/${encodeURIComponent(id)}`, body);
      errors = d.errors || [];
      globalErr.innerHTML = "";
      errors.filter((e) => e.step === null || e.step === undefined).forEach((e) => globalErr.append(h("div", { text: e.message })));
      if (d.error) globalErr.append(h("div", { text: d.error }));
      renderSteps();
      if (!d.ok) {
        msg.textContent = errors.length ? `Not saved: ${errors.length} problem${errors.length === 1 ? "" : "s"} to fix` : "Not saved";
        msg.className = "errors";
        const first = $(".step.bad"); if (first) first.scrollIntoView({ block: "center" });
        else globalErr.scrollIntoView({ block: "center" });
        return;
      }
      msg.textContent = "Saved."; msg.className = "muted";
      root.dataset.saved = "1";
      if (d.job_url) window.location = d.job_url;
      else if (isNew) window.location = d.url;
    }

    renderHeader(); renderData(); renderSteps();
    root.append(globalErr, header, dataBox, h("h2", {}, "Steps"), stepsBox);
    const sv = $("#save"), sr = $("#save-run");
    if (sv) sv.addEventListener("click", () => save(null));
    if (sr) sr.addEventListener("click", () => save($("#run-env").value));
  }

  /* ------------------------------------------------------------ spy results */
  const spyEl = $("#spy");
  if (spyEl) spyView(spyEl);

  function spyView(rootEl) {
    const data = readJSON("spy-data");
    const img = $("#shot-img"), shot = $("#shot");
    const rows = $$("tr[data-key]", rootEl);
    const boxes = {};
    function placeBoxes() {
      if (!img || !img.naturalWidth) return;
      const scale = img.clientWidth / img.naturalWidth;
      data.elements.forEach((e) => {
        if (e.frame) return; // boxes for iframe content are relative to the frame
        let b = boxes[e.key];
        if (!b) {
          b = h("div", { class: "box", title: e.description, onclick: () => { const r = $(`tr[data-key="${e.key}"]`); r.scrollIntoView({ block: "center" }); flashRow(e.key); } });
          shot.append(b); boxes[e.key] = b;
        }
        Object.assign(b.style, { left: e.box.x * scale + "px", top: e.box.y * scale + "px", width: Math.max(e.box.w * scale, 4) + "px", height: Math.max(e.box.h * scale, 4) + "px" });
      });
      syncBoxes();
    }
    function flashRow(key) { rows.forEach((r) => r.classList.toggle("sel", r.dataset.key === key)); Object.entries(boxes).forEach(([k, b]) => b.classList.toggle("hot", k === key)); }
    function syncBoxes() { rows.forEach((r) => { const b = boxes[r.dataset.key]; if (b) b.classList.toggle("off", !$("input.inc", r).checked); }); }
    if (img) { if (img.complete) placeBoxes(); img.addEventListener("load", placeBoxes); window.addEventListener("resize", placeBoxes); }
    rows.forEach((r) => { r.addEventListener("mouseenter", () => flashRow(r.dataset.key)); $("input.inc", r).addEventListener("change", () => { syncBoxes(); count(); }); });
    const all = $("#inc-all");
    if (all) all.addEventListener("change", () => { rows.forEach((r) => { const c = $("input.inc", r); if (!c.disabled) c.checked = all.checked; }); syncBoxes(); count(); });
    function count() { const n = rows.filter((r) => $("input.inc", r).checked).length; $("#add-btn").textContent = n ? `Save ${n} element${n === 1 ? "" : "s"}` : "Save elements"; $("#add-btn").disabled = !n; refreshAnchor(); }
    function refreshAnchor() {
      const sel = $("#d-anchor"); if (!sel) return; const cur = sel.value; sel.innerHTML = ""; sel.append(h("option", { value: "", text: "No check" }));
      rows.filter((r) => $("input.inc", r).checked).forEach((r) => { const n = $("input.nm", r).value; sel.append(h("option", { value: n, text: n, selected: n === cur })); });
    }
    rows.forEach((r) => $("input.nm", r).addEventListener("input", refreshAnchor));
    rows.forEach((r) => { const sel = $("select.cand", r), shown = $(".loc", r);
      if (sel && shown) sel.addEventListener("change", () => { shown.textContent = sel.value; }); });
    $$('input[name="dest"]').forEach((r) => r.addEventListener("change", () => {
      $("#dest-existing").hidden = r.value !== "existing" || !r.checked ? $("#dest-existing").hidden : false;
      const mode = $('input[name="dest"]:checked').value;
      $("#dest-existing").hidden = mode !== "existing"; $("#dest-new").hidden = mode !== "new";
    }));
    count();
    $("#add-btn").addEventListener("click", async () => {
      const mode = $('input[name="dest"]:checked').value;
      const elements = rows.filter((r) => $("input.inc", r).checked).map((r) => ({
        name: $("input.nm", r).value.trim(), locator: $("select.cand", r).value, sensitive: $("input.sens", r).checked,
        description: r.dataset.desc || "",
        fallbacks: !$("#keep-fallbacks").checked ? [] : $$("select.cand option", r)
          .filter((o) => !o.selected && !/position|CSS path/.test(o.dataset.strategy || "")).slice(0, 2).map((o) => o.value) }));
      const destination = mode === "existing" ? { mode, ref: $("#d-ref").value }
        : { mode, app: $("#d-app").value.trim(), class_name: $("#d-class").value.trim(), title: $("#d-title").value.trim(), path: $("#d-path").value.trim(), loaded_anchor: $("#d-anchor").value };
      const msg = $("#add-msg"); msg.textContent = "Saving…"; msg.className = "muted";
      const d = await api("/api/pages/add-elements", { elements, destination });
      if (d.ok) { msg.className = "muted"; msg.innerHTML = ""; msg.append("Saved. ", (d.notes || []).join(" "), " ", h("a", { href: d.url, text: "Open the page object" })); }
      else { msg.textContent = d.error; msg.className = "errors"; }
    });
  }

  /* ------------------------------------------------------------ generated page editor */
  const pageEl = $("#page-editor");
  if (pageEl) {
    const page = readJSON("page-data");
    const tbl = $("#pe-rows");
    function draw() {
      tbl.innerHTML = "";
      tbl.append(h("tr", {}, h("th", {}, "Name"), h("th", {}, "Locator, then fallbacks"), h("th", {}, "Note"), h("th", {}, "Hide in screenshots"), h("th", {}, "")));
      page.elements.forEach((e, i) => tbl.append(h("tr", {},
        h("td", {}, h("input", { type: "text", value: e.name, "aria-label": "Name", oninput: (ev) => { e.name = ev.target.value.trim(); anchor(); } })),
        h("td", {}, h("input", { type: "text", class: "mono", value: e.locator, "aria-label": "Locator", oninput: (ev) => { e.locator = ev.target.value; } }),
          (() => { const ta = h("textarea", { rows: Math.max(1, (e.fallbacks || []).length), placeholder: "Fallback locators, one per line (optional)", "aria-label": "Fallback locators",
            oninput: (ev) => { e.fallbacks = ev.target.value.split("\n").map((x) => x.trim()).filter(Boolean); } }); ta.value = (e.fallbacks || []).join("\n"); ta.style.marginTop = "4px"; return ta; })()),
        h("td", {}, h("input", { type: "text", value: e.description || "", "aria-label": "Note", oninput: (ev) => { e.description = ev.target.value; } })),
        h("td", {}, h("input", { type: "checkbox", checked: !!e.sensitive, "aria-label": "Sensitive", onchange: (ev) => { e.sensitive = ev.target.checked; } })),
        h("td", {}, h("button", { type: "button", class: "btn quiet danger", text: "Remove", onclick: () => { page.elements.splice(i, 1); draw(); anchor(); } })))));
    }
    function anchor() { const s = $("#pe-anchor"); const cur = page.loaded_anchor || ""; s.innerHTML = ""; s.append(h("option", { value: "", text: "No check" }));
      page.elements.forEach((e) => s.append(h("option", { value: e.name, text: e.name, selected: e.name === cur }))); }
    $("#pe-add").addEventListener("click", () => { page.elements.push({ name: "", locator: "page.get_by_role(\"button\", name=\"\")", description: "", sensitive: false }); draw(); anchor(); });
    $("#pe-save").addEventListener("click", async () => {
      page.title = $("#pe-title").value; page.path = $("#pe-path").value || null; page.loaded_anchor = $("#pe-anchor").value || null;
      const d = await api(pageEl.dataset.url, page);
      const msg = $("#pe-msg"); if (d.ok) window.location = d.url; else { msg.textContent = d.error; msg.className = "errors"; }
    });
    draw(); anchor();
  }
})();

/* ------------------------------------------------------------ environment settings form */
(function () {
  "use strict";
  const root = document.getElementById("env-form");
  if (!root) return;
  const data = JSON.parse(document.getElementById("env-form-data").textContent);
  const raw = data.raw || {}, base = data.base || {};
  const get = (obj, path) => path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
  const el = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v === undefined || v === null || v === false) return;
      if (k === "text") e.textContent = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v; else if (k === "checked") e.checked = !!v; else e.setAttribute(k, v === true ? "" : v); });
    kids.flat().forEach((c) => c !== null && c !== undefined && e.append(c instanceof Node ? c : document.createTextNode(String(c))));
    return e;
  };
  const values = {};
  const lists = { apps: [], users: [] };
  function control(f, current, inherited, onchange) {
    const id = "f-" + Math.random().toString(36).slice(2, 9);
    let input;
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox", id, checked: current === undefined ? !!inherited : !!current, onchange: (e) => onchange(e.target.checked) });
      return el("label", { class: "check", style: "margin-top:26px" }, input, f.label);
    }
    if (f.kind === "select") {
      const cur = current ?? "";
      input = el("select", { id, onchange: (e) => onchange(e.target.value) },
        f.choices.map((c) => el("option", { value: c, text: c === "" ? (inherited ? `Default (${inherited})` : "Not used") : c, selected: c === cur || (cur === "" && c === "" ) })));
      if (cur === "" && !f.choices.includes("")) input.insertBefore(el("option", { value: "", text: inherited ? `Default (${inherited})` : "Default", selected: true }), input.firstChild);
    } else {
      input = el("input", { type: f.kind === "number" ? "number" : "text", id, value: current ?? "",
        placeholder: inherited !== undefined && inherited !== null && inherited !== "" ? String(inherited) : (f.placeholder || ""),
        class: f.secret ? "mono" : null, oninput: (e) => onchange(e.target.value) });
    }
    return el("div", {}, el("label", { class: "f", for: id }, f.label + (f.required ? "" : "")), input, f.help ? el("div", { class: "help", text: f.help }) : null);
  }
  function listSection(sec) {
    const box = el("div");
    const current = raw[sec.list] || {};
    lists[sec.list] = Object.entries(current).map(([k, v]) => Object.assign({ _key: k }, v));
    const draw = () => {
      box.innerHTML = "";
      lists[sec.list].forEach((item, i) => {
        const grid = el("div", { class: "fgrid" },
          el("div", {}, el("label", { class: "f" }, sec.key_label), el("input", { type: "text", value: item._key || "", class: "mono", oninput: (e) => { item._key = e.target.value.trim(); } })),
          sec.fields.map((f) => control(f, item[f.key], undefined, (v) => { item[f.key] = v; })));
        box.append(el("div", { class: "listitem" }, grid,
          el("div", { class: "btns", style: "margin-top:6px" }, el("button", { type: "button", class: "btn quiet danger", text: `Remove this ${sec.item}`, onclick: () => { lists[sec.list].splice(i, 1); draw(); } }))));
      });
      box.append(el("button", { type: "button", class: "btn", text: `Add ${sec.item}`, onclick: () => { lists[sec.list].push({ _key: "" }); draw(); } }));
    };
    draw();
    return box;
  }
  data.schema.forEach((sec) => {
    const body = sec.list ? listSection(sec) : el("div", { class: "fgrid" }, sec.fields.map((f) =>
      control(f, get(raw, f.key), get(base, f.key), (v) => { values[f.key] = v; })));
    root.append(el("section", { class: "formsec" }, el("div", {}, el("h2", { text: sec.title }), sec.help ? el("p", { class: "small muted", text: sec.help }) : null), body));
  });
  document.getElementById("env-save").addEventListener("click", async () => {
    const msg = document.getElementById("env-msg");
    msg.textContent = "Checking…"; msg.className = "muted";
    const res = await fetch(data.save_url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf"]').content },
      body: JSON.stringify({ values, apps: lists.apps, users: lists.users }) });
    const d = await res.json().catch(() => ({ ok: false, error: "Studio did not answer" }));
    if (d.ok) window.location = d.url; else { msg.textContent = "Not saved: " + d.error; msg.className = "errors"; }
  });
})();
