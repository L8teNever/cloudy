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
