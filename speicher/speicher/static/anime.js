// Anime: AniWorld durchsuchen, Staffeln/Folgen wählen und in die Mediathek (→ Jellyfin) laden
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const appEl = $(".files-app");
  if (!appEl) return;
  const CSRF = appEl.dataset.csrf || "";

  const toast = (msg, err = false) => {
    const sb = $(".snackbar");
    if (!sb) return;
    sb.classList.toggle("err", Boolean(err));
    const ic = $(".ms", sb);
    if (ic) ic.textContent = err ? "error" : "check_circle";
    $(".txt", sb).textContent = msg;
    sb.classList.add("on");
    clearTimeout(sb._t);
    sb._t = setTimeout(() => sb.classList.remove("on"), err ? 7000 : 3800);
  };
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const ms = (name) => el("span", "ms", name);
  const img = (url) => (url ? `/api/mediathek/bild?url=${encodeURIComponent(url)}` : "");
  const pad = (n) => String(n).padStart(2, "0");
  const mb = (b) => (b ? `${(b / 1e6).toFixed(0)} MB` : "");
  const LANG = { 1: "Deutsch", 2: "Eng-Sub", 3: "Ger-Sub" };

  async function api(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET") headers["X-CSRF"] = CSRF;
    if (opts.body && typeof opts.body === "object") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, { ...opts, headers });
    let data = {};
    try { data = await res.json(); } catch (_) { /* leer */ }
    if (!res.ok) throw new Error(data.fehler || `Fehler ${res.status}`);
    return data;
  }

  // ---------- Suche ----------
  const grid = $("#ani-grid");
  const statusEl = $("#ani-status");
  const emptyEl = $("#ani-empty");
  const input = $("#ani-search");
  const serieCache = new Map();
  let suchNr = 0;

  const serie = (slug) => {
    if (!serieCache.has(slug)) {
      const p = api(`/api/anime/serie?slug=${encodeURIComponent(slug)}`);
      p.catch(() => serieCache.delete(slug));
      serieCache.set(slug, p);
    }
    return serieCache.get(slug);
  };

  function card(it) {
    const btn = el("button", "poster-card");
    btn.type = "button";
    const art = el("div", "poster-art");
    const ph = ms("animation");
    ph.classList.add("ph");
    art.append(ph);
    const body = el("div", "poster-body");
    body.append(el("p", "poster-title", it.titel));
    const sub = el("p", "poster-sub", "AniWorld");
    body.append(sub);
    btn.append(art, body);
    btn.title = it.beschreibung || "";
    btn.addEventListener("click", () => openSerie(it.slug, it.titel));
    btn._fill = (s) => {
      if (s.cover) {
        const i = el("img");
        i.src = img(s.cover);
        i.alt = s.titel;
        i.loading = "lazy";
        art.replaceChildren(i);
      }
      const n = s.staffeln.filter((x) => !x.film).length;
      sub.textContent = s.film === "einzeln" ? [s.jahr, "Film"].filter(Boolean).join(" · ")
        : [s.jahr, n && !s.film ? `${n} Staffel${n > 1 ? "n" : ""}` : "", s.staffeln.some((x) => x.film) || s.film ? "Filme" : ""].filter(Boolean).join(" · ");
    };
    return btn;
  }

  async function suchen(q) {
    const nr = ++suchNr;
    if (q.length < 2) { grid.replaceChildren(); statusEl.hidden = true; emptyEl.hidden = false; return; }
    emptyEl.hidden = true;
    statusEl.hidden = false;
    statusEl.textContent = "Suche auf AniWorld …";
    try {
      const { items } = await api(`/api/anime/suche?q=${encodeURIComponent(q)}`);
      if (nr !== suchNr) return;
      statusEl.textContent = items.length ? `${items.length} Treffer` : "Nichts gefunden.";
      const cards = items.map(card);
      grid.replaceChildren(...cards);
      // Cover & Jahr nachladen (3 gleichzeitig, nur die ersten 18)
      const queue = items.slice(0, 18).map((it, i) => [it, cards[i]]);
      const worker = async () => {
        while (queue.length && nr === suchNr) {
          const [it, c] = queue.shift();
          try { c._fill(await serie(it.slug)); } catch (_) { /* ohne Cover */ }
        }
      };
      await Promise.all([worker(), worker(), worker()]);
    } catch (e) {
      if (nr === suchNr) statusEl.textContent = e.message;
    }
  }
  let t;
  input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => suchen(input.value.trim()), 450); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(t); suchen(input.value.trim()); } });

  // ---------- Detail ----------
  const dlg = $("#dlg-anime");
  let cur = null;          // serie()
  let curSeason = null;    // nr
  let curLang = "1";
  let curEps = [];
  let modus = "laden";
  const KNOEPFE = {
    laden: { "#ani-load-season": "Ganze Staffel laden", "#ani-load-picked": "Ausgewählte laden", "#ani-load-all": "Alles laden" },
    live: { "#ani-load-season": "Staffel live", "#ani-load-picked": "Ausgewählte live", "#ani-load-all": "Alles live" },
  };
  function setModus(m) {
    modus = m;
    $$("#ani-modus .pill").forEach((x) => x.classList.toggle("active", x.dataset.modus === m));
    const hint = $("#ani-modus-hint");
    if (hint) hint.hidden = m !== "live";
    for (const [sel, text] of Object.entries(KNOEPFE[m])) {
      const b = $(sel);
      if (b) b.lastChild.textContent = text;
    }
  }
  $("#ani-modus")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-modus]");
    if (b) setModus(b.dataset.modus);
  });

  async function openSerie(slug, titel) {
    cur = null;
    $("#ani-title").textContent = titel || slug;
    $("#ani-sub").textContent = "Lädt …";
    $("#ani-desc").textContent = "";
    $("#ani-badges").replaceChildren();
    $("#ani-seasons").replaceChildren();
    $("#ani-episodes").replaceChildren();
    $("#ani-poster").replaceChildren(Object.assign(ms("animation"), { className: "ms ph" }));
    $("#ani-bg").hidden = true;
    setButtons(false);
    dlg.showModal();
    try {
      cur = await serie(slug);
    } catch (e) {
      $("#ani-sub").textContent = e.message;
      return;
    }
    $("#ani-title").textContent = cur.titel;
    $("#ani-sub").textContent = [cur.jahr, cur.genres.slice(0, 4).join(", ")].filter(Boolean).join(" · ");
    $("#ani-desc").textContent = cur.beschreibung;
    $("#ani-badges").append(el("span", "badge-pill", "Anime"));
    if (cur.cover) {
      const i = el("img");
      i.src = img(cur.cover);
      i.alt = cur.titel;
      $("#ani-poster").replaceChildren(i);
      $("#ani-bg").src = img(cur.cover);
      $("#ani-bg").hidden = false;
    }
    const row = $("#ani-seasons");
    for (const s of cur.staffeln) {
      const b = el("button", "pill", s.name);
      b.type = "button";
      b.dataset.nr = s.nr;
      b.addEventListener("click", () => openSeason(s.nr));
      row.append(b);
    }
    const first = cur.staffeln.find((s) => !s.film) || cur.staffeln[0];
    openSeason(first.nr);
  }

  function setButtons(on) {
    for (const id of ["#ani-load-season", "#ani-load-picked", "#ani-load-all"]) {
      const b = $(id);
      if (b) b.disabled = !on;
    }
    const p = $("#ani-load-picked");
    if (p && on) p.disabled = !$$("#ani-episodes input:checked").length;
  }

  async function openSeason(nr) {
    curSeason = nr;
    $$("#ani-seasons .pill").forEach((b) => b.classList.toggle("active", Number(b.dataset.nr) === nr));
    const box = $("#ani-episodes");
    box.replaceChildren(el("div", "progress indet", ""));
    box.firstChild.append(el("i"));
    setButtons(false);
    try {
      const { folgen } = await api(`/api/anime/staffel?slug=${encodeURIComponent(cur.slug)}&nr=${nr}`);
      if (curSeason !== nr) return;
      curEps = folgen;
      paintEpisodes();
      setButtons(folgen.length > 0);
    } catch (e) {
      box.replaceChildren(el("p", "body-m variant", e.message));
    }
  }

  function jobFor(ep) {
    return jobs.find((j) => j.pfad === ep.pfad && j.sprache === curLang && j.status !== "abgebrochen");
  }

  function paintEpisodes() {
    const box = $("#ani-episodes");
    if (!curEps.length) { box.replaceChildren(el("p", "body-m variant", "Keine Folgen gefunden.")); return; }
    const rows = curEps.map((ep) => {
      const j = jobFor(ep);
      const row = el("label", "ep-row" + (j && ["fertig", "vorhanden"].includes(j.status) ? " ready" : ""));
      row.style.cursor = "pointer";
      const cb = el("input");
      cb.type = "checkbox";
      cb.value = ep.nr;
      cb.addEventListener("change", () => setButtons(true));
      row.append(cb, el("div", "ep-num", curSeason === 0 ? `F${pad(ep.nr)}` : `E${pad(ep.nr)}`));
      const txt = el("div", "txt");
      txt.append(el("p", "body-m", ep.titel || `Folge ${ep.nr}`));
      const langs = ep.sprachen.length ? ep.sprachen.map((k) => LANG[k]).join(" · ") : "";
      const warn = ep.sprachen.length && !ep.sprachen.includes(curLang) ? ` · nicht auf ${LANG[curLang]}` : "";
      txt.append(el("p", "body-s variant", (j ? statusText(j) : langs) + (j ? "" : warn)));
      row.append(txt);
      return row;
    });
    box.replaceChildren(...rows);
  }

  $("#ani-langs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-lang]");
    if (!b) return;
    curLang = b.dataset.lang;
    $$("#ani-langs .pill").forEach((x) => x.classList.toggle("active", x === b));
    paintEpisodes();
  });

  async function laden(body, label) {
    if (modus === "live") return live(body, label);
    try {
      const r = await api("/api/anime/laden", { method: "POST", body: { slug: cur.slug, sprache: curLang, ...body } });
      toast(r.neu ? `${label}: ${r.neu} Folge${r.neu > 1 ? "n" : ""} in der Warteschlange – siehe „Downloads“` : "Schon alles in der Warteschlange");
      poll();
    } catch (e) { toast(e.message, true); }
  }
  async function live(body, label) {
    try {
      const r = await api("/api/anime/live", { method: "POST", body: { slug: cur.slug, sprache: curLang, ...body } });
      const teile = [];
      if (r.neu) teile.push(`${r.neu} Folge${r.neu > 1 ? "n" : ""} live in Jellyfin`);
      if (r.vorhanden) teile.push(`${r.vorhanden} schon da`);
      toast(`${label}: ${teile.join(", ") || "nichts Neues"} – Jellyfin liest gleich ein`);
      libDirty = true;
    } catch (e) { toast(e.message, true); }
  }
  $("#ani-load-season")?.addEventListener("click", () => laden({ staffeln: [curSeason] }, cur.staffeln.find((s) => s.nr === curSeason)?.name || "Staffel"));
  $("#ani-load-all")?.addEventListener("click", () => {
    const wie = modus === "live" ? "live in Jellyfin anlegen" : "laden";
    if (!confirm(`Wirklich alle ${cur.staffeln.length} Staffeln/Filme von „${cur.titel}“ auf ${LANG[curLang]} ${wie}?`)) return;
    laden({ staffeln: cur.staffeln.map((s) => s.nr) }, cur.titel);
  });
  $("#ani-load-picked")?.addEventListener("click", () => {
    const nrs = $$("#ani-episodes input:checked").map((c) => ({ staffel: curSeason, nr: Number(c.value) }));
    if (nrs.length) laden({ folgen: nrs }, "Auswahl");
  });

  // ---------- Downloads ----------
  let jobs = [];
  let letzteOffen = 0;
  let libDirty = true;
  const STATUS = { wartet: "Wartet", laeuft: "Lädt", fertig: "Fertig", vorhanden: "Schon vorhanden", fehler: "Fehler", abgebrochen: "Abgebrochen" };
  function statusText(j) {
    if (j.status === "laeuft") return `${j.meldung || "Lädt …"}${j.fortschritt ? ` · ${Math.round(j.fortschritt * 100)} %` : ""}${j.bytes ? ` · ${mb(j.bytes)}` : ""}`;
    if (j.status === "fehler") return `Fehler: ${j.meldung}`;
    return j.status === "fertig" && j.meldung ? j.meldung : STATUS[j.status] || j.status;
  }

  function paintJobs() {
    const box = $("#dl-box");
    const list = $("#dl-list");
    const sichtbar = jobs.filter((j) => j.status !== "abgebrochen");
    box.hidden = !sichtbar.length;
    $("#dl-empty").hidden = Boolean(sichtbar.length);
    const offen = sichtbar.filter((j) => ["wartet", "laeuft"].includes(j.status)).length;
    $("#dl-count").textContent = offen ? `(${offen})` : "";
    if (offen < letzteOffen) libDirty = true;  // etwas ist fertig geworden → „Geladen“ neu holen
    letzteOffen = offen;
    const fehler = sichtbar.filter((j) => j.status === "fehler").length;
    $("#dl-head").textContent = offen ? `Downloads · ${offen} offen` : "Downloads";
    $("#dl-retry").hidden = !fehler;
    // laufende zuerst, dann wartende, dann der Rest (neueste oben)
    const rang = { laeuft: 0, wartet: 1, fehler: 2, fertig: 3, vorhanden: 3 };
    const sorted = [...sichtbar].sort((a, b) => (rang[a.status] - rang[b.status]) || (a.status === "wartet" ? a.erstellt - b.erstellt : b.erstellt - a.erstellt));
    list.replaceChildren(...sorted.slice(0, 60).map((j) => {
      const row = el("div", "ep-row" + (["fertig", "vorhanden"].includes(j.status) ? " ready" : ""));
      row.append(el("div", "ep-num", j.staffel === 0 ? `F${pad(j.folge)}` : `S${pad(j.staffel)}E${pad(j.folge)}`));
      const txt = el("div", "txt");
      txt.append(el("p", "body-m", `${j.titel}${j.folgentitel ? " – " + j.folgentitel : ""}`));
      txt.append(el("p", "body-s variant", `${LANG[j.sprache] || ""} · ${statusText(j)}`));
      if (j.status === "laeuft") {
        const p = el("div", "progress" + (j.fortschritt ? "" : " indet"));
        const i = el("i");
        if (j.fortschritt) i.style.width = `${Math.round(j.fortschritt * 100)}%`;
        p.append(i);
        p.style.marginTop = "6px";
        txt.append(p);
      }
      row.append(txt);
      if (["wartet", "laeuft"].includes(j.status) || j.status === "fehler") {
        if (j.status === "fehler") {
          const r = el("button", "icon-btn");
          r.type = "button";
          r.title = "Nochmal";
          r.append(ms("refresh"));
          r.addEventListener("click", () => jobAktion("wiederholen", j.id));
          row.append(r);
        }
        const x = el("button", "icon-btn");
        x.type = "button";
        x.title = "Abbrechen";
        x.append(ms("close"));
        x.addEventListener("click", () => jobAktion("abbrechen", j.id));
        row.append(x);
      }
      return row;
    }));
  }

  async function jobAktion(aktion, id = "") {
    try { await api("/api/anime/job", { method: "POST", body: { aktion, id } }); poll(); } catch (e) { toast(e.message, true); }
  }
  $("#dl-retry").addEventListener("click", () => jobAktion("wiederholen"));
  $("#dl-clear").addEventListener("click", () => jobAktion("aufraeumen"));
  $("#live-neu")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const r = await api("/api/anime/live/neu", { method: "POST", body: {} });
      toast(r.neu ? `${r.neu} neue Folge${r.neu === 1 ? "" : "n"} – Jellyfin liest sie gleich ein` : "Keine neuen Folgen");
      if (r.neu) ladeLib();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  });

  let pollT;
  async function poll() {
    clearTimeout(pollT);
    try {
      const r = await api("/api/anime/status");
      jobs = r.jobs;
      const chip = $("#browser-chip");
      chip.replaceChildren(ms(r.browser.an ? "language" : "public_off"), r.browser.an ? "Browser" : "Direkt");
      chip.title = r.browser.text;
      chip.classList.toggle("ok", r.browser.an);
      paintJobs();
      if (dlg.open && cur) paintEpisodesKeepChecks();
      if (tab === "geladen" && libDirty) ladeLib();
    } catch (_) { /* nächster Versuch */ }
    const aktiv = jobs.some((j) => ["wartet", "laeuft"].includes(j.status));
    pollT = setTimeout(poll, document.hidden ? 15000 : aktiv ? 2000 : 10000);
  }
  function paintEpisodesKeepChecks() {
    if (!curEps.length) return;
    const checked = new Set($$("#ani-episodes input:checked").map((c) => c.value));
    paintEpisodes();
    $$("#ani-episodes input").forEach((c) => { c.checked = checked.has(c.value); });
  }

  // ---------- Reiter ----------
  let tab = "suche";
  function zeigeTab(name) {
    tab = ["suche", "downloads", "geladen"].includes(name) ? name : "suche";
    $$("[data-tab]").forEach((b) => { b.hidden = b.dataset.tab !== tab; });
    $$("#ani-tabs input").forEach((i) => { i.checked = i.value === tab; });
    if (location.hash.slice(1) !== tab) history.replaceState(null, "", tab === "suche" ? location.pathname : `#${tab}`);
    if (tab === "geladen") ladeLib();
  }
  $("#ani-tabs").addEventListener("change", (e) => zeigeTab(e.target.value));
  window.addEventListener("hashchange", () => zeigeTab(location.hash.slice(1)));

  // ---------- Geladen (fertige Anime: abspielen, löschen) ----------
  const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1).replace(".", ",")} GB` : mb(b) || "0 MB");
  const offenLib = new Set();
  let libNr = 0;
  async function ladeLib() {
    const nr = ++libNr;
    libDirty = false;
    const status = $("#lib-status");
    if (!$("#lib-list").children.length) status.textContent = "Lädt …";
    try {
      const r = await api("/api/anime/geladen");
      if (nr !== libNr) return;
      const ges = r.items.reduce((a, t) => a + t.groesse, 0);
      const folgen = r.items.reduce((a, t) => a + t.dateien.length, 0);
      status.textContent = !r.pool_da ? "Der Speicher ist gerade nicht eingehängt."
        : r.items.length ? `${r.items.length} Anime · ${folgen} Folgen · ${gb(ges)}` : "Noch nichts geladen.";
      $("#lib-list").replaceChildren(...r.items.map((t) => libKarte(t, r.loeschen)));
    } catch (e) {
      if (nr === libNr) status.textContent = e.message;
    }
    ladeLive(nr);
  }

  async function ladeLive(nr) {
    try {
      const r = await api("/api/anime/live");
      if (nr !== libNr) return;
      $("#live-box").hidden = !r.items.length;
      $("#live-list").replaceChildren(...r.items.map((g) => {
        const row = el("div", "ep-row");
        const ic = el("div", "ep-num");
        ic.append(ms("live_tv"));
        const txt = el("div", "txt");
        txt.append(el("p", "body-m", g.ordner));
        txt.append(el("p", "body-s variant", g.film ? "Film" : `${g.folgen} Folge${g.folgen === 1 ? "" : "n"}${g.staffeln.length ? " · " + g.staffeln.join(", ") : ""}`));
        row.append(ic, txt);
        if (r.loeschen) {
          const del = el("button", "icon-btn danger");
          del.type = "button";
          del.title = "Aus Jellyfin entfernen";
          del.append(ms("delete"));
          del.addEventListener("click", async () => {
            if (!confirm(`Live-Folgen von „${g.ordner}“ aus Jellyfin entfernen? Heruntergeladene Folgen bleiben.`)) return;
            try {
              const x = await api("/api/anime/live/entfernen", { method: "POST", body: { ordner: g.ordner } });
              toast(`${x.n} Live-Folge${x.n === 1 ? "" : "n"} entfernt`);
              ladeLib();
            } catch (e) { toast(e.message, true); }
          });
          row.append(del);
        }
        return row;
      }));
    } catch (_) { /* Live-Liste ist optional */ }
  }

  function libKarte(t, darfLoeschen) {
    const card = el("section", "card");
    const head = el("div", "card-head");
    head.style.cursor = "pointer";
    const pic = el("div", "sheet-poster");
    pic.style.cssText = "width:48px;height:68px;flex:none";
    if (t.poster) {
      const i = el("img");
      i.src = img(t.poster);
      i.alt = "";
      i.loading = "lazy";
      pic.append(i);
    } else {
      const ph = ms("animation");
      ph.classList.add("ph");
      pic.append(ph);
    }
    const txt = el("div", "");
    txt.style.cssText = "flex:1;min-width:0";
    const name = el("p", "title-m", `${t.name}${t.year ? ` (${t.year})` : ""}`);
    name.style.margin = "0";
    const busy = t.busy_count ? ` · ${t.busy_count} in Arbeit` : "";
    const info = el("p", "body-s variant", `${t.dateien.length} Folge${t.dateien.length === 1 ? "" : "n"} · ${gb(t.groesse)}${busy}`);
    info.style.margin = "2px 0 0";
    txt.append(name, info);
    const pfeil = ms(offenLib.has(t.id) ? "expand_less" : "expand_more");
    head.append(pic, txt);
    if (darfLoeschen) {
      const del = el("button", "icon-btn danger");
      del.type = "button";
      del.title = "Ganzen Anime löschen";
      del.append(ms("delete"));
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`„${t.name}“ komplett löschen (${t.dateien.length} Folgen, ${gb(t.groesse)})? Das geht nicht rückgängig.`)) return;
        let n = 0;
        for (const f of t.dateien) {
          try { await api(`/api/mediathek/datei/${f.id}`, { method: "DELETE" }); n++; } catch (err) { toast(err.message, true); break; }
        }
        if (n) toast(`${n} Folge${n > 1 ? "n" : ""} gelöscht`);
        ladeLib();
      });
      head.append(del);
    }
    head.append(pfeil);
    const body = el("div", "");
    body.hidden = !offenLib.has(t.id);
    body.style.marginTop = "12px";
    head.addEventListener("click", () => {
      body.hidden = !body.hidden;
      if (body.hidden) offenLib.delete(t.id); else offenLib.add(t.id);
      pfeil.textContent = body.hidden ? "expand_more" : "expand_less";
    });
    let staffel;
    for (const f of t.dateien) {
      if (f.staffel !== staffel) {
        staffel = f.staffel;
        const h = el("p", "title-s", staffel === 0 ? "Filme" : `Staffel ${staffel ?? "?"}`);
        h.style.margin = "12px 0 6px";
        body.append(h);
      }
      body.append(libFolge(t, f, darfLoeschen));
    }
    card.append(head, body);
    return card;
  }

  function libFolge(t, f, darfLoeschen) {
    const bereit = f.status === "bereit";
    const row = el("div", "ep-row" + (bereit ? " ready" : ""));
    row.append(el("div", "ep-num", f.staffel === 0 ? `F${pad(f.folge || 0)}` : `E${pad(f.folge || 0)}`));
    const txt = el("div", "txt");
    txt.append(el("p", "body-m", f.folgentitel || `Folge ${f.folge ?? "?"}`));
    const st = bereit ? "" : f.status === "fehler" ? ` · Fehler: ${f.fehler || ""}` : " · wird vorbereitet …";
    txt.append(el("p", "body-s variant", `${gb(f.groesse || 0)}${st}`));
    row.append(txt);
    if (bereit) {
      const play = el("button", "icon-btn");
      play.type = "button";
      play.title = "Abspielen";
      play.append(ms("play_arrow"));
      play.addEventListener("click", () => abspielen(t, f));
      row.append(play);
    }
    if (darfLoeschen) {
      const del = el("button", "icon-btn danger");
      del.type = "button";
      del.title = "Folge löschen";
      del.append(ms("delete"));
      del.addEventListener("click", async () => {
        if (!confirm(`Folge „${f.folgentitel || f.folge}“ löschen?`)) return;
        try { await api(`/api/mediathek/datei/${f.id}`, { method: "DELETE" }); toast("Gelöscht"); ladeLib(); } catch (e) { toast(e.message, true); }
      });
      row.append(del);
    }
    return row;
  }

  const playDlg = $("#dlg-play");
  const video = $("#play-video");
  function abspielen(t, f) {
    $("#play-title").textContent = `${t.name} · ${f.staffel === 0 ? `Film ${f.folge}` : `S${pad(f.staffel)}E${pad(f.folge)}`}`;
    video.src = `/api/mediathek/stream/${f.id}`;
    playDlg.showModal();
    video.play().catch(() => {});
  }
  playDlg.addEventListener("close", () => { video.pause(); video.removeAttribute("src"); video.load(); });

  zeigeTab(location.hash.slice(1));
  poll();
})();

