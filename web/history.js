// SPDX-License-Identifier: GPL-3.0-only
// History tab (idea.md, "Web UI spec"): /api/history as stacked SVG panels (room and outdoor temp, humidity, the AC
// mode) on one time axis, and the flash log's tail (/api/log?tail=) as the event list. Loaded after index.html's
// script; uses its $, rows, deg and clock and its "tab" event. The Pico has no wall clock (ADR 0004): a time
// is the phone's clock less an age, so an earlier boot's events show only their uptime.
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const TICK_H = { 180: 1, 720: 3, 1440: 6 }; // hours between time labels
  const L = 34, R = 6, T = 6, TEMP_H = 100, HUM_Y = 128, HUM_H = 52, BAND_Y = 192, BAND_H = 12, H = 230;
  const BAND = { cool: "var(--c-cool)", dry: "var(--c-dry)", fan: "var(--c-fan)", off: null, u: "url(#h-hatch)" };
  // data: /api/history and st: /api/state, fetched together at `at`: the history's ages and the uptime are as of then.
  // picked: the phone time of the minute the cursor is on, kept across a refresh; null: the newest minute.
  let minutes = 1440, data = null, log = [], st = null, timer = null, shown = false, at = 0, cur = null, picked = null;
  const ago = (a) => clock(a + (Date.now() - at) / 1000); // an age as of the fetch -> the phone's clock

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.append(e);
    return e;
  }

  function scale(vals, span, top, h) {
    // -> {y, ticks}: a y scale from the values' range, at least `span` wide, with round bounds.
    const v = vals.filter((x) => x != null);
    let lo = v.length ? Math.min(...v) : 0, hi = v.length ? Math.max(...v) : span;
    if (hi - lo < span) { const m = (lo + hi) / 2; lo = m - span / 2; hi = m + span / 2; }
    const step = [1, 2, 5, 10, 20].find((s) => (hi - lo) / s <= 3) || 50;
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let t = lo; t <= hi; t += step) ticks.push(t);
    return { y: (x) => top + h - ((x - lo) / (hi - lo)) * h, ticks };
  }

  function draw() {
    const box = $("h-chart"), d = data, n = d.temp.length;
    const W = Math.max(280, box.clientWidth || 358), up = st.device.uptime_s;
    const bridge = Math.max(1, Math.ceil(st.poll_s / 60)); // slots between two polls are no gap
    const dx = (W - L - R) / Math.max(1, n - 1), x = (i) => L + i * dx;
    const age = (i) => d.last_age_s + (n - 1 - i) * d.step_s;
    const slot = (a) => n - 1 - (a - d.last_age_s) / d.step_s; // an age -> its (fractional) slot
    const svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "history chart" });
    const pat = el("pattern", { id: "h-hatch", width: 4, height: 4, patternUnits: "userSpaceOnUse",
      patternTransform: "rotate(45)" }, el("defs", {}, svg));
    el("path", { d: "M0 0V4", style: "stroke:var(--dim)" }, pat);
    const g = (cls) => el("g", { class: cls }, svg);
    const grid = g("grid"), lab = g("lab");
    const text = (x0, y0, s, anchor) => { el("text", { x: x0, y: y0, "text-anchor": anchor || "end" }, lab).textContent = s; };

    // Each slot's band: the mode while on, nothing while off, hatched while unreachable this boot. A short gap
    // between polls takes the slot before it.
    const kind = d.power.map((p, i) => (p == null ? null : !p ? "off" : BAND[d.mode[i]] !== undefined ? d.mode[i] : "o"));
    for (let i = 0; i < n; i++) {
      if (kind[i] != null) continue;
      let j = i;
      while (j < n && kind[j] == null) j++;
      const fill = i > 0 && j < n && j - i <= bridge ? kind[i - 1] : null;
      for (let k = i; k < j; k++) kind[k] = fill || (age(k) < up ? "u" : null);
      i = j;
    }
    for (let i = 0; i < n; i++) {
      let j = i;
      while (j + 1 < n && kind[j + 1] === kind[i]) j++;
      const fill = kind[i] === "o" ? "var(--c-other)" : BAND[kind[i]];
      const x0 = Math.max(L, x(i) - dx / 2), x1 = Math.min(W - R, x(j) + dx / 2);
      if (fill) el("rect", { x: x0, y: BAND_Y, width: x1 - x0, height: BAND_H, style: "fill:" + fill }, svg);
      i = j;
    }

    const temp = scale(d.temp.concat(d.out), 4, T, TEMP_H), hum = scale(d.hum, 20, HUM_Y, HUM_H);
    for (const [s, unit] of [[temp, "°"], [hum, "%"]]) {
      for (const t of s.ticks) {
        el("path", { d: "M" + L + " " + s.y(t) + "H" + (W - R) }, grid);
        text(L - 4, s.y(t) + 4, t + unit);
      }
    }
    text(L - 4, BAND_Y + 10, "AC");

    // Time labels on the phone's clock, at round hours.
    const end = at - d.last_age_s * 1000;
    const t0 = new Date(end - (n - 1) * d.step_s * 1000);
    t0.setMinutes(0, 0, 0);
    for (let t = t0.getTime() + 3600e3; t <= end; t += 3600e3) {
      if (new Date(t).getHours() % TICK_H[minutes]) continue;
      const xi = x(slot((end - t) / 1000 + d.last_age_s));
      el("path", { d: "M" + xi + " " + T + "V" + (BAND_Y + BAND_H) }, grid);
      text(xi, H - 4, ago((at - t) / 1000), "middle");
    }

    for (const [vals, s, cls] of [[d.out, temp, "out"], [d.temp, temp, "ln"], [d.hum, hum, "ln"]]) {
      let p = "", gap = Infinity;
      vals.forEach((v, i) => {
        if (v == null) { gap++; return; }
        p += (gap <= bridge ? "L" : "M") + x(i).toFixed(1) + " " + s.y(v).toFixed(1) + (gap > bridge ? "h0" : "");
        gap = 0;
      });
      el("path", { d: p, class: cls }, svg);
    }

    // Where this boot's data begins, and its cycle starts (their ages from the log's uptime).
    if (slot(up) > 0) {
      const xb = x(slot(up));
      el("path", { d: "M" + xb + " " + T + "V" + (BAND_Y + BAND_H), class: "boot" }, svg);
      text(xb + 3, T + 10, "boot", "start");
    }
    for (const e of log) {
      if (e.boot !== st.device.boot || e.kind !== "cycle_start") continue;
      const i = slot(up - e.up_s);
      if (i >= 0) el("path", { d: "M" + x(i) + " " + (BAND_Y + BAND_H + 1) + "l4 7h-8z", class: "mark" }, svg);
    }

    cur = el("path", { class: "cur", d: "" }, svg);
    const pick = (ev) => {
      const r = svg.getBoundingClientRect();
      const i = Math.min(n - 1, Math.max(0, Math.round(((ev.clientX - r.left) * (W / r.width) - L) / dx)));
      picked = at - age(i) * 1000;
      readout(i, x);
    };
    svg.addEventListener("pointerdown", (ev) => { svg.setPointerCapture(ev.pointerId); pick(ev); });
    svg.addEventListener("pointermove", (ev) => { if (ev.buttons || ev.pointerType === "mouse") pick(ev); });
    box.replaceChildren(svg);
    const i = picked == null ? -1 : Math.round(slot((at - picked) / 1000));
    if (i >= 0 && i < n) readout(i, x); else readout(n - 1);
  }

  function readout(i, x) {
    // That minute's values above the chart; the newest one when nothing is picked.
    const d = data, a = d.last_age_s + (d.temp.length - 1 - i) * d.step_s, up = st.device.uptime_s;
    if (cur) cur.setAttribute("d", x ? "M" + x(i) + " " + T + "V" + (BAND_Y + BAND_H) : "");
    const ac = d.power[i] == null ? (a > up ? "before this boot" : "no reading")
      : d.power[i] ? d.mode[i] : "AC off";
    $("h-read").textContent = [ago(a), deg(d.temp[i]) + (d.out[i] == null ? "" : ", outdoor " + deg(d.out[i])),
      d.hum[i] == null ? "–" : d.hum[i] + " %", ac].join(" · ");
  }

  function events() {
    // Newest first. This boot's rows show the clock; an earlier boot's are grouped under it with their uptime.
    const list = [], boot = st.device.boot;
    let group = boot;
    for (const e of log.slice().reverse()) {
      if (e.kind === "heap") continue;
      if (e.boot !== group) {
        group = e.boot;
        const b = document.createElement("b");
        b.textContent = "boot " + e.boot;
        list.push([b, "earlier boot: time unknown, uptime shown"]);
      }
      const msg = document.createElement("span"), room = document.createElement("small");
      msg.textContent = e.kind + ": " + e.msg + " ";
      room.textContent = e.temp == null ? "" : e.temp.toFixed(1) + " °C " + (e.hum == null ? "" : e.hum + " %");
      msg.append(room);
      const u = e.up_s;
      list.push([e.boot === boot ? ago(st.device.uptime_s - u) : "+" + Math.floor(u / 3600) + ":"
        + String(Math.floor(u / 60) % 60).padStart(2, "0"), msg]);
    }
    rows($("h-events"), list);
  }

  async function load() {
    clearTimeout(timer);
    const sec = $("history");
    sec.classList.add("busy"); // the last render stays, dimmed, until the new one is in
    try {
      const h = await fetch("/api/history?minutes=" + minutes, { cache: "no-store" });
      if (!h.ok) throw new Error("HTTP " + h.status);
      data = await h.json();
      at = Date.now();
      const s = await fetch("/api/state", { cache: "no-store" });
      if (!s.ok) throw new Error("HTTP " + s.status);
      st = await s.json();
      const l = await fetch("/api/log?tail=300", { cache: "no-store" });
      if (l.ok) {
        log = (await l.text()).split("\n").map((s) => { try { return JSON.parse(s); } catch (e) { return null; } })
          .filter((e) => e && typeof e.boot === "number" && typeof e.kind === "string" && typeof e.up_s === "number");
      }
      $("h-err").textContent = "";
      draw();
      events();
    } catch (e) {
      $("h-err").textContent = "history not loaded (" + e.message + ")";
    }
    sec.classList.remove("busy");
    if (shown) timer = setTimeout(load, 60000);
  }

  for (const b of $("h-win").children) {
    b.addEventListener("click", () => {
      minutes = +b.dataset.min;
      for (const o of $("h-win").children) o.classList.toggle("on", o === b);
      load();
    });
  }
  document.addEventListener("tab", (ev) => {
    shown = ev.detail === "history";
    if (shown) load(); else clearTimeout(timer);
  });
  window.addEventListener("resize", () => { if (shown && data) draw(); });
})();
