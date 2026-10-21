// SPDX-License-Identifier: GPL-3.0-only
// Profiles and Settings tabs (idea.md, "Web UI spec"). Loaded after index.html's script and uses its helpers ($, api)
// and its events: "tab" (a tab was shown) and "state" (a fresh /api/state). window.ruleSummary is for the Run picker.
// Each tab edits a copy (draft) of the document from GET; Save sends the whole document with PUT. Every input carries
// data-path, the JSON path the server's errors use (profiles.day.rules[0].phases[1].max_min), so an error marks its
// field. A path with no input (e.g. ...phases, or a profile) marks the block with that data-block, or the nearest
// block above it; one with no block at all (e.g. "$") is shown at the top of the tab.
"use strict";
(() => {
  // [min, max, step]: the ranges from idea.md's "Validation" table, and our steps.
  const LIMITS = {
    target_c: [18, 30, 0.5], max_min: [1, 120, 1], cooldown_min: [0, 480, 5], confirm_reads: [1, 10, 1],
    min_interval_min: [0, 1440, 15], temp: [10, 45, 0.5], outdoor_temp: [10, 45, 0.5], hum: [20, 95, 1],
    poll_s: [30, 300, 10], hard_cap_min: [30, 480, 10], manual_hold_min: [10, 480, 10],
    manual_release_off_min: [5, 240, 5], finish_fan_min: [0, 60, 5], limit_temp: [0, 60, 1], limit_hum: [0, 100, 1],
  };
  const MAX = { profiles: 10, rules: 8, conditions: 4, phases: 5 };
  const MODES = ["cool", "dry", "fan"];
  const FANS = ["auto", "quiet", "1", "2", "3", "4", "5"];
  const SENSORS = [["temp", "temp"], ["hum", "humidity"], ["outdoor_temp", "outdoor"]];
  const SENSOR_TEXT = Object.fromEntries(SENSORS);
  const OP_TEXT = { ">": ">", ">=": "≥", "<": "<", "<=": "≤" };
  const GOAL = { cool: "temp", dry: "hum" }; // the sensor a mode's `until` usually waits for
  const NAME_RE = /^[a-z0-9_]{1,24}$/;

  const clone = (v) => JSON.parse(JSON.stringify(v));

  function h(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "class") e.className = v;
      else if (k.includes("-")) e.setAttribute(k, v);
      else e[k] = v;
    }
    e.append(...kids.flat(2).filter((c) => c != null && c !== false));
    return e;
  }
  const button = (text, onclick, props) => h("button", Object.assign({ type: "button", onclick }, props), text);

  // --- JSON paths, as the server writes them: profiles.day.rules[0].name

  const keys = (path) => path.replace(/\[(\d+)\]/g, ".$1").split(".");
  const at = (doc, path) => keys(path).reduce((o, k) => (o == null ? undefined : o[k]), doc);
  function put(doc, path, value) {
    const ks = keys(path);
    const last = ks.pop();
    ks.reduce((o, k) => o[k], doc)[last] = value;
  }
  function up(path) {
    const p = path.replace(/(\.[^.[\]]+|\[\d+\])$/, "");
    return p === path ? "" : p;
  }
  const under = (k, p) => k === p || k.startsWith(p + ".") || k.startsWith(p + "[");

  // --- one editor per tab

  class Editor {
    constructor(tab, root, url, view, extra) {
      Object.assign(this, { tab, root, url, view, saved: null, draft: null, state: null, errors: [], warnings: [],
                            result: "", busy: false, timer: 0, open: new Set() }, extra);
    }

    async load() {
      this.root.replaceChildren(h("p", { class: "hint" }, "loading…"));
      try {
        const r = await fetch(this.url, { cache: "no-store" });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
        this.saved = data;
        this.draft = clone(data);
        this.errors = [];
        this.warnings = [];
        this.render();
      } catch (e) {
        this.root.replaceChildren(h("p", { class: "hint" }, "Could not load: " + e.message));
      }
    }

    dirty() {
      return this.draft != null && JSON.stringify(this.draft) !== JSON.stringify(this.saved);
    }

    render() {
      const y = window.scrollY;
      this.top = h("div");
      this.root.replaceChildren(...this.view(this), this.bar());
      this.mark();
      this.update();
      window.scrollTo(0, y);
    }

    bar() {
      this.saveBtn = button("Save", () => this.save(), { class: "primary" });
      this.note = h("span", { class: "note" });
      return h("div", { class: "bar" }, this.saveBtn, button("Revert", () => this.revert()),
        this.resetUrl && button("Restore defaults", () => this.reset()), this.note);
    }

    // A value typed into a field: no re-render, so the field keeps its focus.
    set(path, value, i) {
      if (i == null) put(this.draft, path, value); else at(this.draft, path)[i] = value;
      this.update();
    }

    // A change of shape (add, remove, move, a mode that brings other fields): re-render. Marks from before would
    // point at the wrong fields, so they go until the next validation.
    edit(fn) {
      fn();
      this.errors = [];
      this.warnings = [];
      this.render();
      this.validateSoon();
    }

    // Open/closed blocks are kept by path: when a list changes, its items' paths move with them.
    reindex(list, map) {
      const out = new Set();
      for (const k of this.open) {
        const m = k.startsWith(list + "[") && /^\[(\d+)\](.*)$/.exec(k.slice(list.length));
        if (!m) { out.add(k); continue; }
        const j = map(+m[1]);
        if (j >= 0) out.add(list + "[" + j + "]" + m[2]);
      }
      this.open = out;
    }

    rekey(from, to) {
      this.open = new Set([...this.open].map((k) => (under(k, from) ? to + k.slice(from.length) : k)));
    }

    update() {
      const dirty = this.dirty();
      const n = this.errors.length, w = this.warnings.length;
      let note = this.result;
      if (!note && n) note = n + (n === 1 ? " problem" : " problems") + ", marked below";
      if (!note && dirty) note = "unsaved changes";
      if (w) note += (note ? "; " : "") + w + (w === 1 ? " warning" : " warnings");
      this.note.textContent = note;
      this.note.classList.toggle("bad", n > 0);
      this.saveBtn.disabled = this.busy || !dirty;
      const link = document.querySelector('#tabs a[href="#' + this.tab.toLowerCase() + '"]');
      link.textContent = this.tab + (dirty ? " •" : "");
      for (const el of this.root.querySelectorAll("[data-sum]")) el.textContent = summary(at(this.draft, el.dataset.sum));
      for (const el of this.root.querySelectorAll("[data-text]")) el.textContent = at(this.draft, el.dataset.text) || "";
      this.guards();
    }

    guards() {
      // The active profile can't be renamed or deleted; it is the one /api/state names.
      const active = this.state ? this.state.profile : this.saved && this.saved.active;
      for (const b of this.root.querySelectorAll("[data-guard]")) {
        b.disabled = b.dataset.guard === active;
        b.title = b.disabled ? "the active profile: switch to another one first" : "";
      }
      for (const el of this.root.querySelectorAll("[data-badge]")) {
        el.textContent = el.dataset.badge + (el.dataset.badge === active ? " · active" : "");
      }
    }

    targets(path) {
      const q = (attr, p) => [...this.root.querySelectorAll("[" + attr + '="' + CSS.escape(p) + '"]')];
      const fields = q("data-path", path);
      if (fields.length) return fields;
      for (let p = path; p; p = up(p)) {
        const blocks = q("data-block", p);
        if (blocks.length) return blocks;
      }
      return [];
    }

    mark() {
      for (const el of this.root.querySelectorAll(".bad, .warn")) el.classList.remove("bad", "warn");
      for (const el of this.root.querySelectorAll(".msg")) el.remove();
      const loose = [];
      const place = (item, cls) => {
        const found = this.targets(item.path);
        const msg = () => h("span", { class: cls === "warn" ? "msg w" : "msg" }, item.msg);
        if (!found.length) {
          loose.push(h("span", { class: cls === "warn" ? "msg w" : "msg" },
            (item.path === "$" ? "" : item.path + ": ") + item.msg));
          return;
        }
        for (const el of found) {
          const box = el.closest(".f") || el;
          box.classList.add(cls);
          if (box.classList.contains("f")) {
            if (![...box.querySelectorAll(".msg")].some((m) => m.textContent === item.msg)) box.append(msg());
          } else {
            const head = box.querySelector(":scope > summary");
            if (head) head.after(msg()); else box.prepend(msg());
          }
          for (let d = box.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
        }
      };
      this.errors.forEach((e) => place(e, "bad"));
      this.warnings.forEach((w) => place(w, "warn"));
      this.top.replaceChildren(...loose);
    }

    take(r) {
      // A reply from PUT or /validate: {"ok", "errors" | "warnings"}, or {"error"} (401, 409, 413, 503).
      const d = r.data || {};
      this.errors = d.errors || (r.status === 200 ? [] : [{ path: "$", msg: d.error || "HTTP " + r.status }]);
      this.warnings = d.warnings || [];
      this.mark();
    }

    validateSoon() {
      if (!this.validateUrl) return;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.validate(), 500);
    }

    async validate() {
      const sent = JSON.stringify(this.draft);
      try {
        const r = await api("POST", this.validateUrl, this.draft, false);
        if (JSON.stringify(this.draft) !== sent || this.busy) return; // edited meanwhile: a newer check follows
        this.result = "";
        this.take(r);
        this.update();
      } catch (e) {
        // No token yet, or offline: Save will say so.
      }
    }

    async save() {
      clearTimeout(this.timer);
      const body = clone(this.draft);
      this.busy = true;
      this.result = "saving…";
      this.update();
      try {
        const r = await api("PUT", this.url, body);
        this.take(r);
        if (r.status === 200) {
          this.saved = body;
          this.result = "Saved; applies from the next tick (a running cycle finishes as it started)";
        } else {
          this.result = r.status === 400 ? "Not saved: see the marked fields" : "Not saved: " + this.errors[0].msg;
        }
      } catch (e) {
        this.result = "Not saved: " + e.message;
      }
      this.busy = false;
      this.update();
    }

    async revert() {
      if (this.dirty() && !confirm("Drop the unsaved changes?")) return;
      this.result = "";
      await this.load();
    }

    async reset() {
      if (!confirm("Replace all profiles with the defaults (day, night, away)?")) return;
      try {
        const r = await api("POST", this.resetUrl);
        if (r.status !== 200) {
          this.take(r);
          this.result = "Not restored";
          this.update();
          return;
        }
        await this.load();
        this.result = "Defaults restored";
        this.update();
      } catch (e) {
        this.result = "Not restored: " + e.message;
        this.update();
      }
    }
  }

  // --- fields: each returns a .f block holding the input (with data-path) and later its message

  function wrap(label, ...kids) {
    return h("div", { class: "f" }, label && h("span", null, label), ...kids);
  }

  function num(ed, path, label, [min, max, step], i) {
    const v = i == null ? at(ed.draft, path) : (at(ed.draft, path) || [])[i];
    const input = h("input", { type: "number", min, max, step, value: v ?? "", inputMode: step < 1 ? "decimal" : "numeric",
                               "data-path": path, "aria-label": label || path });
    input.addEventListener("input", () => ed.set(path, input.value === "" ? null : Number(input.value), i));
    input.addEventListener("change", () => ed.validateSoon());
    const bump = (d) => () => {
      const now = input.value === "" ? (d > 0 ? min - step : max + step) : Number(input.value);
      input.value = Math.min(max, Math.max(min, Math.round((now + d * step) / step) * step));
      input.dispatchEvent(new Event("input"));
      input.dispatchEvent(new Event("change"));
    };
    return wrap(label, h("span", { class: "in" },
      button("−", bump(-1), { class: "step", tabIndex: -1, "aria-label": "less" }), input,
      button("+", bump(1), { class: "step", tabIndex: -1, "aria-label": "more" })));
  }

  function pair(ed, path, label, limits) {
    // [low, high]: two inputs on one path, so an error on the pair marks both.
    const f = wrap(label);
    const lo = num(ed, path, null, limits, 0).querySelector(".in");
    const hi = num(ed, path, null, limits, 1).querySelector(".in");
    f.append(h("span", { class: "in" }, lo, h("span", { class: "sep" }, "–"), hi));
    return f;
  }

  function text(ed, path, label, opt = {}) {
    const input = h("input", { type: "text", value: at(ed.draft, path) ?? "", maxLength: opt.maxLength,
                               placeholder: opt.placeholder, inputMode: opt.inputMode, className: opt.cls,
                               autocapitalize: "off", "data-path": path, "aria-label": label || path });
    input.setAttribute("spellcheck", "false");
    input.addEventListener("input", () => ed.set(path, opt.nullIfEmpty ? input.value.trim() || null : input.value));
    input.addEventListener("change", () => ed.validateSoon());
    return wrap(label, input);
  }

  function sel(ed, path, label, options, onset) {
    // onset(value): a change of shape, applied through ed.edit(); otherwise the value is set in place.
    const s = h("select", { "data-path": path, "aria-label": label || path },
      options.map((o) => (Array.isArray(o) ? h("option", { value: o[0] }, o[1]) : h("option", { value: o }, o))));
    s.value = at(ed.draft, path);
    s.addEventListener("change", () => {
      if (onset) ed.edit(() => onset(s.value));
      else { ed.set(path, s.value); ed.validateSoon(); }
    });
    return wrap(label, s);
  }

  function check(ed, path, label, lock) {
    const input = h("input", { type: "checkbox", checked: !!at(ed.draft, path), disabled: !!lock, "data-path": path });
    input.addEventListener("change", () => ed.set(path, input.checked));
    return wrap(null, h("label", null, input, " " + label), lock && h("span", { class: "lock" }, lock));
  }

  function details(ed, path, head, ...body) {
    const d = h("details", { class: "card", open: ed.open.has(path), "data-block": path }, h("summary", null, head),
      ...body);
    d.addEventListener("toggle", () => (d.open ? ed.open.add(path) : ed.open.delete(path)));
    return d;
  }

  function moves(ed, list, i, listPath, min, ask) {
    // Up, down, remove for item i of list (at listPath); remove keeps at least `min` items.
    const swap = (j) => ed.edit(() => {
      [list[i], list[j]] = [list[j], list[i]];
      ed.reindex(listPath, (x) => (x === i ? j : x === j ? i : x));
    });
    const remove = () => {
      if (ask && !confirm(ask)) return;
      ed.edit(() => {
        list.splice(i, 1);
        ed.reindex(listPath, (x) => (x === i ? -1 : x > i ? x - 1 : x));
      });
    };
    return h("span", { class: "row" },
      button("↑", () => swap(i - 1), { disabled: i === 0, title: "move up" }),
      button("↓", () => swap(i + 1), { disabled: i === list.length - 1, title: "move down" }),
      button("remove", remove, { disabled: list.length <= min }));
  }

  // --- plain-language summary: "When temp > 29 for 2 readings: cool to 26 for up to 30 min (stop early at ≤ 26),
  // then fan 20 min, then off. Wait 30 min."

  const condText = (c, goal) => (c.sensor === goal ? "" : (SENSOR_TEXT[c.sensor] || c.sensor) + " ")
    + (OP_TEXT[c.op] || c.op) + " " + c.value;

  function phaseText(p) {
    const until = p.until ? " (stop early at " + condText(p.until, GOAL[p.mode]) + ")" : "";
    if (p.mode === "cool") return "cool to " + p.target_c + " for up to " + p.max_min + " min" + until;
    if (p.mode === "fan" && !p.until) return "fan " + p.max_min + " min";
    return p.mode + " for up to " + p.max_min + " min" + until;
  }

  function summary(r) {
    if (!r || !r.when || !Array.isArray(r.when.conditions) || !Array.isArray(r.phases)) return "";
    const conds = r.when.conditions.map((c) => condText(c)).join(r.when.match === "any" ? " or " : " and ");
    const n = r.confirm_reads;
    let s = "When " + conds + " for " + n + (n === 1 ? " reading" : " readings") + ": "
      + r.phases.map(phaseText).join(", then ") + ", then off.";
    if (r.cooldown_min > 0) s += " Wait " + r.cooldown_min + " min.";
    if (r.min_interval_min > 0) s += " At most one start every " + r.min_interval_min + " min.";
    return s;
  }

  window.ruleSummary = summary;

  // --- Profiles tab

  function uniqueName(taken, base) {
    if (!taken.includes(base)) return base;
    for (let n = 2; ; n++) {
      const name = base.slice(0, 24 - String(n).length - 1) + "_" + n;
      if (!taken.includes(name)) return name;
    }
  }

  function newRule(rules) {
    return {
      name: uniqueName(rules.map((r) => r.name), "rule"),
      when: { match: "all", conditions: [{ sensor: "temp", op: ">", value: 29 }] },
      confirm_reads: 2,
      phases: [
        { mode: "cool", target_c: 26, fan: "auto", max_min: 30, until: { sensor: "temp", op: "<=", value: 26 } },
        { mode: "fan", fan: "3", max_min: 20, until: null },
      ],
      cooldown_min: 30,
      min_interval_min: 0,
    };
  }

  function goalFor(p) {
    if (p.mode === "cool") return { sensor: "temp", op: "<=", value: p.target_c ?? 26 };
    if (p.mode === "dry") return { sensor: "hum", op: "<=", value: 65 };
    return { sensor: "temp", op: "<=", value: 26 };
  }

  function setMode(p, mode) {
    p.mode = mode;
    if (mode === "cool") p.target_c = p.target_c ?? 26;
    else delete p.target_c;
  }

  function condRow(ed, path, c, ...tools) {
    return h("div", { class: "row", "data-block": path },
      sel(ed, path + ".sensor", null, SENSORS, (v) => { c.sensor = v; }),
      sel(ed, path + ".op", null, Object.entries(OP_TEXT)),
      num(ed, path + ".value", null, LIMITS[c.sensor] || LIMITS.temp), ...tools);
  }

  function phaseBlock(ed, path, phases, k, listPath) {
    const p = phases[k];
    const stop = h("input", { type: "checkbox", checked: p.until != null });
    stop.addEventListener("change", () => ed.edit(() => { p.until = stop.checked ? goalFor(p) : null; }));
    return h("div", { class: "phase", "data-block": path },
      h("div", { class: "grid" },
        sel(ed, path + ".mode", "phase " + (k + 1), MODES, (v) => setMode(p, v)),
        p.mode === "cool" && num(ed, path + ".target_c", "to °C", LIMITS.target_c),
        sel(ed, path + ".fan", "fan", FANS),
        num(ed, path + ".max_min", "up to min", LIMITS.max_min)),
      h("div", { class: "row", "data-block": path + ".until" },
        h("label", null, stop, " stop early at"),
        p.until != null && condRow(ed, path + ".until", p.until)),
      moves(ed, phases, k, listPath, 1));
  }

  function ruleBlock(ed, path, rules, i, listPath) {
    const r = rules[i];
    const when = r.when, conds = when.conditions, phases = r.phases;
    return details(ed, path, [h("b", { "data-text": path + ".name" }), h("div", { class: "sum", "data-sum": path })],
      h("div", { class: "grid" },
        text(ed, path + ".name", "name", { maxLength: 24 }),
        num(ed, path + ".confirm_reads", "readings", LIMITS.confirm_reads),
        num(ed, path + ".cooldown_min", "wait after, min", LIMITS.cooldown_min),
        num(ed, path + ".min_interval_min", "min between starts", LIMITS.min_interval_min)),
      h("div", { "data-block": path + ".when" },
        h("div", { class: "row" }, "Start when", sel(ed, path + ".when.match", null, [["all", "all"], ["any", "any"]]),
          "of:"),
        h("div", { "data-block": path + ".when.conditions" },
          conds.map((c, j) => condRow(ed, path + ".when.conditions[" + j + "]", c,
            button("×", () => ed.edit(() => conds.splice(j, 1)),
              { disabled: conds.length <= 1, title: "remove", "aria-label": "remove condition" })))),
        h("div", { class: "tools" }, button("+ condition", () => ed.edit(() => conds.push(
          { sensor: "temp", op: ">", value: 29 })), { disabled: conds.length >= MAX.conditions }))),
      h("div", { "data-block": path + ".phases" },
        phases.map((p, k) => phaseBlock(ed, path + ".phases[" + k + "]", phases, k, path + ".phases")),
        h("div", { class: "tools" }, button("+ phase", () => ed.edit(() => phases.push(
          { mode: "fan", fan: "auto", max_min: 20, until: null })), { disabled: phases.length >= MAX.phases }))),
      h("div", { class: "tools" }, "rule:", moves(ed, rules, i, listPath, 1, "Remove rule " + r.name + "?")));
  }

  function askName(ed, question, initial) {
    const answer = prompt(question, initial || "");
    if (answer == null) return null;
    const name = answer.trim();
    if (!NAME_RE.test(name)) { alert("A name has 1–24 characters: a–z, 0–9 and _"); return null; }
    if (name === "off" || name in ed.draft.profiles) { alert('"' + name + '" is taken'); return null; }
    return name;
  }

  function renameProfile(ed, old) {
    const name = askName(ed, "New name for " + old, old);
    if (!name) return;
    ed.edit(() => {
      const out = {};
      for (const [k, v] of Object.entries(ed.draft.profiles)) out[k === old ? name : k] = v;
      ed.draft.profiles = out;
      ed.rekey("profiles." + old, "profiles." + name);
    });
  }

  function copyRule(ed, name, rules) {
    // "copy a rule from…": the other profiles' rules; the copy gets a free name.
    const from = [];
    for (const [pn, p] of Object.entries(ed.draft.profiles)) {
      if (pn !== name) p.rules.forEach((r, i) => from.push([pn + "/" + i, pn + ": " + r.name]));
    }
    if (!from.length) return null;
    const s = h("select", { "aria-label": "copy a rule from another profile", disabled: rules.length >= MAX.rules },
      h("option", { value: "" }, "copy a rule from…"), from.map(([v, t]) => h("option", { value: v }, t)));
    s.addEventListener("change", () => {
      const [pn, i] = s.value.split("/");
      const r = clone(ed.draft.profiles[pn].rules[+i]);
      r.name = uniqueName(rules.map((x) => x.name), r.name);
      ed.edit(() => {
        rules.push(r);
        ed.open.add("profiles." + name + ".rules[" + (rules.length - 1) + "]");
      });
    });
    return s;
  }

  function profileBlock(ed, name) {
    const path = "profiles." + name;
    const prof = ed.draft.profiles[name];
    const rules = prof.rules;
    const add = () => ed.edit(() => {
      rules.push(newRule(rules));
      ed.open.add(path + ".rules[" + (rules.length - 1) + "]");
    });
    const duplicate = () => {
      const copy = askName(ed, "Name for the copy of " + name, uniqueName(Object.keys(ed.draft.profiles), name));
      if (copy) ed.edit(() => { ed.draft.profiles[copy] = Object.assign(clone(prof), { label: copy }); });
    };
    const remove = () => {
      if (confirm("Delete profile " + name + "?")) ed.edit(() => { delete ed.draft.profiles[name]; });
    };
    return details(ed, path, [h("b", { "data-text": path + ".label" }), " ", h("span", { class: "lock", "data-badge": name })],
      h("div", { class: "grid" },
        text(ed, path + ".label", "label", { maxLength: 32, cls: "wide" }),
        h("div", { class: "tools" },
          button("rename", () => renameProfile(ed, name), { "data-guard": name }),
          button("duplicate", duplicate, { disabled: Object.keys(ed.draft.profiles).length >= MAX.profiles }),
          button("delete", remove, { "data-guard": name }))),
      h("div", { "data-block": path + ".rules" }, rules.map((r, i) => ruleBlock(ed, path + ".rules[" + i + "]", rules, i,
        path + ".rules"))),
      h("div", { class: "tools" }, button("+ rule", add, { disabled: rules.length >= MAX.rules }),
        copyRule(ed, name, rules)));
  }

  function profilesView(ed) {
    const names = Object.keys(ed.draft.profiles).sort();
    const add = () => {
      const name = askName(ed, "Name of the new profile (a–z, 0–9, _)");
      if (name) ed.edit(() => {
        ed.draft.profiles[name] = { label: name, rules: [newRule([])] };
        ed.open.add("profiles." + name);
      });
    };
    return [
      h("p", { class: "hint" }, "A profile is a list of rules; the first rule whose trigger holds starts a cycle. "
        + "Saved changes apply from the next tick; a running cycle finishes with the rule it started with."),
      ed.top,
      h("div", { "data-block": "profiles" }, names.map((n) => profileBlock(ed, n))),
      h("div", { class: "tools" }, button("+ profile", add, { disabled: names.length >= MAX.profiles })),
    ];
  }

  // --- Settings tab

  function settingsView(ed) {
    const shadow = ed.state && ed.state.shadow;
    return [
      h("p", { class: "hint" }, "Saved settings apply at the next tick."),
      ed.top,
      h("div", { class: "card grid" },
        text(ed, "ac_ip", "AC address", { placeholder: "from secrets.py", nullIfEmpty: true }),
        testButton(ed),
        num(ed, "poll_s", "read the AC every, s", LIMITS.poll_s),
        num(ed, "hard_cap_min", "longest cycle, min", LIMITS.hard_cap_min),
        num(ed, "manual_hold_min", "hands off after the remote, min", LIMITS.manual_hold_min),
        num(ed, "manual_release_off_min", "hold ends after off for, min", LIMITS.manual_release_off_min),
        num(ed, "finish_fan_min", "fan when stopping, min", LIMITS.finish_fan_min),
        pair(ed, "sensor_limits.temp", "trusted temp, °C", LIMITS.limit_temp),
        pair(ed, "sensor_limits.hum", "trusted humidity, %", LIMITS.limit_hum),
        check(ed, "dry_run", "dry run: decide, never write to the AC",
          shadow && "locked: shadow mode runs dry whatever this says")),
    ];
  }

  function testButton(ed) {
    // POST /api/ac/test with the address typed (saved or not); empty: the one in use. Read-only on the AC.
    const out = h("span", { class: "lock", "aria-live": "polite" });
    const run = async () => {
      const ip = ed.draft.ac_ip;
      if (ip && !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) { out.textContent = "not an IPv4 address"; return; }
      b.disabled = true;
      out.textContent = "testing…";
      try {
        const r = await api("POST", "/api/ac/test", ip ? { ip } : {});
        const d = r.data || {};
        if (r.status !== 200) out.textContent = d.error || "HTTP " + r.status;
        else if (d.reachable) out.textContent = "answers at " + d.ip + ": " + (d.type || "?") + ", fw " + (d.fw || "?")
          + (d.comm_err ? ", comm error " + d.comm_err : "") + ", " + d.ms + " ms";
        else out.textContent = "no answer from " + (d.ip || "?") + " (" + d.error + ")";
      } catch (e) {
        out.textContent = "not tested: " + e.message;
      }
      b.disabled = false;
    };
    const b = button("Test connection", run);
    return h("span", { class: "row" }, b, out);
  }

  const profilesEd = new Editor("Profiles", $("profiles"), "/api/profiles", profilesView,
    { validateUrl: "/api/profiles/validate", resetUrl: "/api/profiles/reset" });
  const settingsEd = new Editor("Settings", $("settings-form"), "/api/settings", settingsView, {});

  document.addEventListener("tab", (ev) => {
    const ed = { profiles: profilesEd, settings: settingsEd }[ev.detail];
    if (ed && !ed.draft) ed.load();
  });
  document.addEventListener("state", (ev) => {
    const s = ev.detail;
    const shadowChanged = !!(settingsEd.state && settingsEd.state.shadow) !== !!s.shadow;
    profilesEd.state = settingsEd.state = s;
    if (profilesEd.draft) profilesEd.guards();
    if (shadowChanged && settingsEd.draft) settingsEd.render();
  });
  window.addEventListener("beforeunload", (ev) => {
    if (profilesEd.dirty() || settingsEd.dirty()) ev.preventDefault();
  });
})();
