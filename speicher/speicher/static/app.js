// cloudy Speicher – Dialoge, Snackbar, Live-Auftrag. Kein Inline-JS (CSP: script-src 'self').
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // Snackbar aus ?ok= / ?fehler= und Parameter danach aus der Adresse entfernen.
  const sb = $(".snackbar");
  if (sb && sb.dataset.text) {
    $(".txt", sb).textContent = sb.dataset.text;
    requestAnimationFrame(() => sb.classList.add("on"));
    setTimeout(() => sb.classList.remove("on"), sb.classList.contains("err") ? 8000 : 4000);
    sb.addEventListener("click", () => sb.classList.remove("on"));
    history.replaceState(null, "", location.pathname);
  }

  // Dialoge
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]");
    if (open) {
      const dlg = document.getElementById(open.dataset.open);
      if (dlg) dlg.showModal();
    }
    const close = e.target.closest("[data-close]");
    if (close) close.closest("dialog").close();
  });
  $$("dialog").forEach((d) => d.addEventListener("click", (e) => { if (e.target === d) d.close(); }));

  // Schalter, die einen Bereich ein-/ausblenden (z. B. „Formatieren“ -> Bestätigungsfeld)
  $$("[data-toggle]").forEach((sw) => {
    const area = document.getElementById(sw.dataset.toggle);
    const sync = () => {
      area.classList.toggle("on", sw.checked);
      $$("input", area).forEach((i) => { i.required = sw.checked; });
    };
    sw.addEventListener("change", sync);
    sync();
  });

  // Rückfrage vor heiklen Aktionen, Doppelklicks verhindern
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !confirm(msg)) { e.preventDefault(); return; }
    $$("button", e.target).forEach((b) => { if (b.type !== "button") setTimeout(() => { b.disabled = true; }, 0); });
  });

  // Liste filtern (Gerätesuche)
  $$("[data-filter]").forEach((input) => {
    const list = $(input.dataset.filter);
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      $$("[data-name]", list).forEach((row) => { row.hidden = Boolean(q) && !row.dataset.name.includes(q); });
    });
  });

  // Obere Leiste am Handy einfärben, sobald gescrollt wird
  const top = $(".top-mobile");
  if (top) addEventListener("scroll", () => top.classList.toggle("scrolled", scrollY > 4), { passive: true });

  // Live-Auftrag
  const box = $("[data-job]");
  if (!box) return;
  const log = $(".log", box);
  const title = $("[data-job-title]", box);
  const prog = $("[data-job-progress]", box);
  const btns = $$("[data-needs-idle]");
  let lief = box.dataset.job === "1";
  if (log) log.scrollTop = log.scrollHeight;
  const tick = async () => {
    if (document.hidden) return;
    try {
      const r = await fetch("/api/job", { headers: { accept: "application/json" } });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.name) return;
      if (log) {
        const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
        log.textContent = j.zeilen.join("\n");
        if (stick) log.scrollTop = log.scrollHeight;
      }
      if (title) title.textContent = j.laeuft ? j.name : j.name + " – " + (j.ok ? "fertig" : "fehlgeschlagen");
      if (prog) prog.hidden = !j.laeuft;
      btns.forEach((b) => { b.disabled = j.laeuft; });
      if (lief && !j.laeuft) location.reload();
      lief = j.laeuft;
    } catch (_) { /* offline – nächster Versuch */ }
  };
  setInterval(tick, 2500);
})();

// Auslastung (Freigaben-Seite): alle 3 Sekunden neu
(() => {
  const box = document.querySelector("[data-auslastung]");
  if (!box) return;
  const q = (s) => box.querySelector(s);
  const gb = (b) => (b / 1e9).toFixed(1).replace(".", ",") + " GB";
  const zahl = (n) => String(n).replace(".", ",");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const zeile = (icon, titel, sub, chip, chipCls) => {
    const li = el("div", "li");
    const shape = el("div", "shape round " + (chipCls === "ok" ? "primary" : "muted"));
    shape.append(el("span", "ms", icon));
    const txt = el("div", "txt");
    txt.append(el("p", "title-m", titel));
    if (sub) { const p = el("p", "body-s variant", sub); p.style.margin = "0"; p.style.overflow = "hidden"; p.style.textOverflow = "ellipsis"; p.style.whiteSpace = "nowrap"; txt.append(p); }
    const trail = el("div", "trail");
    if (chip) trail.append(el("span", "chip " + (chipCls || ""), chip));
    li.append(shape, txt, trail);
    return li;
  };
  async function laden() {
    let d;
    try { d = await (await fetch("/api/auslastung", { cache: "no-store" })).json(); } catch (_) { return; }
    q('[data-wert="cpu"]').textContent = `${zahl(d.cpu)} %`;
    q('[data-balken="cpu"]').style.width = Math.min(100, d.cpu) + "%";
    const ramPct = d.ram_total ? (d.ram_benutzt / d.ram_total) * 100 : 0;
    q('[data-wert="ram"]').textContent = `${gb(d.ram_benutzt)} von ${gb(d.ram_total)}`;
    q('[data-balken="ram"]').style.width = ramPct + "%";
    q('[data-wert="netz"]').textContent = `${zahl(d.netz_mb_s)} MB/s`;
    q('[data-wert="dienst"]').textContent = `davon Netzlaufwerk & TV: ${zahl(d.dienst_mb_s)} MB/s`;
    const platten = q("[data-platten]");
    platten.replaceChildren(...d.platten.map((p) => {
      const li = zeile("hard_drive", p.modell || p.name, p.name, `${p.busy} % beschäftigt`, p.busy > 80 ? "info" : "");
      return li;
    }));
    const hg = q("[data-hintergrund]");
    if (!d.hintergrund.length) {
      hg.replaceChildren(zeile("check_circle", "Nichts", d.regel === "leerlauf" ? (d.ruhig ? "Ruhig – Hintergrundarbeit darf laufen" : "Gerade wird übertragen") : "", "frei", "ok"));
    } else {
      hg.replaceChildren(...d.hintergrund.map((h) => zeile(
        h.zustand === "läuft" ? "autorenew" : (h.zustand === "pausiert" ? "pause_circle" : "schedule"),
        h.was, [h.detail, h.grund].filter(Boolean).join(" · "), h.zustand, h.zustand === "läuft" ? "ok" : "info")));
    }
  }
  laden();
  setInterval(laden, 3000);
})();
