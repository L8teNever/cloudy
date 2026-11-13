// Streaming-Mediathek: Suche, Detailansicht, deutsche Anbieter, Upload-Wizard, Player & TV-Staging
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

  const imgProxy = (url) => (url ? `/api/mediathek/bild?url=${encodeURIComponent(url)}` : "");
  const fmtBytes = (n) => {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Number(n || 0);
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i < 2 ? v.toFixed(0) : v.toFixed(1)).replace(".", ",") + " " + u[i];
  };
  const fmtDur = (sec) => {
    if (!sec) return "";
    const m = Math.round(sec / 60);
    return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m} Min.`;
  };
  const fmtLeft = (ts) => {
    if (!ts) return "bis gelöscht";
    const diff = ts - Date.now() / 1000;
    if (diff <= 0) return "läuft ab";
    if (diff < 3600) return `noch ${Math.ceil(diff / 60)} Min.`;
    if (diff < 86400) return `noch ${Math.ceil(diff / 3600)} Std.`;
    return `noch ${Math.ceil(diff / 86400)} Tage`;
  };
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const ms = (name) => el("span", "ms", name);

  async function api(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET") headers["X-CSRF"] = CSRF;
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, { ...opts, headers });
    let data = {};
    try { data = await res.json(); } catch (_) { /* leer */ }
    if (!res.ok) throw new Error(data.fehler || `Fehler ${res.status}`);
    return data;
  }

  // ---------- Zustand ----------
  let library = [];
  let tvItems = [];
  let searchResults = null;
  let activeFilter = "all";
  let currentDetail = null;
  let currentDetailSeason = 1;
  let pollTimer = null;

  const gridEl = $("#stream-grid");
  const emptyEl = $("#stream-empty");
  const searchInput = $("#stream-search");
  const clearBtn = $("#stream-search-clear");
  const statusEl = $("#search-status");
  const tvBanner = $("#tv-banner");
  const tvListEl = $("#tv-list");

  // ---------- TV Banner ----------
  function renderTv() {
    tvBanner.hidden = tvItems.length === 0;
    tvListEl.replaceChildren();
    for (const item of tvItems) {
      const row = el("div", "tv-item");
      const icon = el("div", "shape round primary");
      icon.style.width = "38px";
      icon.style.height = "38px";
      icon.append(ms(item.art === "bild" ? "image" : "movie"));
      const txt = el("div", "txt");
      txt.append(el("p", "title-m", item.titel || item.ziel_pfad));
      let sub = "";
      if (item.status === "kopiere") sub = `Kopiere auf TV-Ordner … ${item.fortschritt || 0}%`;
      else if (item.status === "umwandlung") sub = "Wandle Video für TV um …";
      else if (item.status === "fehler") sub = `Fehler: ${item.fehler || "unbekannt"}`;
      else sub = `${fmtBytes(item.groesse)} · ${fmtLeft(item.ablauf_am)}`;
      txt.append(el("p", "body-s variant", sub));
      row.append(icon, txt);
      if (item.status === "kopiere" || item.status === "umwandlung") {
        const prog = el("div", "progress");
        prog.style.width = "72px";
        const bar = el("i");
        bar.style.width = `${Math.max(10, item.fortschritt || 10)}%`;
        prog.append(bar);
        row.append(prog);
      }
      const del = el("button", "icon-btn danger");
      del.type = "button";
      del.title = "Aus TV-Ordner entfernen";
      del.append(ms("close"));
      del.addEventListener("click", async () => {
        try {
          await api(`/api/tv/item/${item.id}`, { method: "DELETE" });
          toast("Aus „Jetzt schauen“ entfernt");
          await refreshLibrary();
        } catch (err) { toast(err.message, true); }
      });
      row.append(del);
      tvListEl.append(row);
    }
  }

  $("#tv-clear-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/tv/clear", { method: "POST", body: {} });
      toast("TV-Ordner geleert");
      await refreshLibrary();
    } catch (err) { toast(err.message, true); }
  });

  // ---------- Poster-Grid ----------
  function makeCard(item, isLocalLib = false) {
    const btn = el("button", "poster-card");
    btn.type = "button";
    const art = el("div", "poster-art");
    if (item.poster) {
      const img = el("img");
      img.src = imgProxy(item.poster);
      img.alt = item.name || "";
      img.loading = "lazy";
      art.append(img);
    } else {
      const ph = ms(item.type === "movie" ? "movie" : "tv");
      ph.classList.add("ph");
      art.append(ph);
    }

    const badges = el("div", "poster-badges");
    const left = el("span");
    const ready = isLocalLib ? (item.ready_count || 0) : (item.local?.ready_count || 0);
    const busy = isLocalLib ? (item.busy_count || 0) : (item.local?.busy_count || 0);
    if (ready > 0) {
      const b = el("span", "badge-pill local");
      b.append(ms("check_circle"), item.type === "movie" ? "Auf cloudy" : `${ready} Folgen`);
      left.append(b);
    } else if (busy > 0) {
      const b = el("span", "badge-pill busy");
      b.append(ms("sync"), "Wandelt um");
      left.append(b);
    }
    const right = el("span", "badge-pill", item.anime ? "Anime" : (item.type === "movie" ? "Film" : "Serie"));
    badges.append(left, right);
    art.append(badges);

    const body = el("div", "poster-body");
    body.append(el("p", "poster-title", item.name || "Unbenannt"));
    const parts = [];
    if (item.year) parts.push(item.year);
    if (item.provider && !isLocalLib) parts.push(item.provider === "tmdbmovie" ? "TMDB" : item.provider.toUpperCase());
    if (item.score) parts.push(`★ ${item.score}`);
    body.append(el("p", "poster-sub", parts.join(" · ") || "—"));

    btn.append(art, body);
    btn.addEventListener("click", () => openDetail(item.ref, item));
    return btn;
  }

  function renderGrid() {
    gridEl.replaceChildren();
    const filterFn = (it) => activeFilter === "all" || it.type === activeFilter;

    if (searchResults !== null) {
      const list = (searchResults.results || []).filter(filterFn);
      statusEl.hidden = false;
      const errs = (searchResults.errors || []).length ? ` · Hinweise: ${searchResults.errors.join("; ")}` : "";
      statusEl.textContent = `${list.length} Treffer${errs}`;
      emptyEl.hidden = list.length > 0;
      if (!list.length) {
        $("#stream-empty-title").textContent = "Keine Treffer gefunden";
        $("#stream-empty-sub").textContent = "Probier einen anderen Suchbegriff oder lade deine Datei direkt hoch.";
      }
      for (const item of list) gridEl.append(makeCard(item, false));
      return;
    }

    statusEl.hidden = true;
    const list = library.filter(filterFn);
    emptyEl.hidden = list.length > 0;
    if (!list.length) {
      $("#stream-empty-title").textContent = "Noch nichts in deiner Mediathek";
      $("#stream-empty-sub").textContent = "Such oben nach einem Film, einer Serie oder einem Anime – oder lad direkt eigene Videos hoch.";
    }
    for (const item of list) gridEl.append(makeCard(item, true));
  }

  async function refreshLibrary() {
    try {
      const data = await api("/api/mediathek/bibliothek");
      library = data.items || [];
      tvItems = data.tv || [];
      renderTv();
      if (searchResults === null) renderGrid();
      if (currentDetail && $("#dlg-detail").open) {
        const updated = library.find((x) => x.ref === currentDetail.ref);
        if (updated) {
          currentDetail.local = updated;
          renderDetailLocal();
        }
      }
      schedulePoll();
    } catch (_) { /* leise ignorieren */ }
  }

  function schedulePoll() {
    const hasBusyLib = library.some((t) => (t.busy_count || 0) > 0);
    const hasBusyTv = tvItems.some((t) => t.status === "kopiere" || t.status === "umwandlung");
    clearTimeout(pollTimer);
    if (hasBusyLib || hasBusyTv) {
      pollTimer = setTimeout(refreshLibrary, 2500);
    }
  }

  // ---------- Suche ----------
  let searchTimer = null;
  async function runSearch(q) {
    const query = q.trim();
    clearBtn.hidden = !query;
    if (!query) {
      searchResults = null;
      renderGrid();
      return;
    }
    statusEl.hidden = false;
    statusEl.textContent = `Suche nach „${query}“ …`;
    try {
      searchResults = await api(`/api/mediathek/suche?q=${encodeURIComponent(query)}`);
      renderGrid();
    } catch (err) {
      statusEl.textContent = err.message;
    }
  }

  searchInput?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(searchInput.value), 350);
  });
  searchInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      clearTimeout(searchTimer);
      runSearch(searchInput.value);
    }
  });
  clearBtn?.addEventListener("click", () => {
    searchInput.value = "";
    runSearch("");
  });

  $$("#filter-row .pill").forEach((b) => b.addEventListener("click", () => {
    $$("#filter-row .pill").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    activeFilter = b.dataset.filter;
    renderGrid();
  }));

  // ---------- Detail-Sheet ----------
  const dlgDetail = $("#dlg-detail");

  async function openDetail(ref, preview = null) {
    $("#det-title").textContent = preview?.name || "Lade …";
    $("#det-sub").textContent = "Lade Details …";
    $("#det-summary").textContent = "";
    $("#det-local-content").replaceChildren(el("p", "body-m variant", "Lade …"));
    $("#det-watch-content").replaceChildren(el("p", "body-m variant", "Lade …"));
    $("#det-cast-box").hidden = true;
    dlgDetail.showModal();

    try {
      const data = await api(`/api/mediathek/details?ref=${encodeURIComponent(ref)}`);
      currentDetail = { ref, item: data.item, local: data.local };
      currentDetailSeason = 1;
      renderDetail();
    } catch (err) {
      toast(err.message, true);
      dlgDetail.close();
    }
  }

  function renderDetail() {
    const { item, local } = currentDetail;
    const bg = $("#det-bg");
    if (item.backdrop || item.poster) {
      bg.src = imgProxy(item.backdrop || item.poster);
      bg.hidden = false;
    } else bg.hidden = true;

    const pBox = $("#det-poster");
    pBox.replaceChildren();
    if (item.poster) {
      const img = el("img");
      img.src = imgProxy(item.poster);
      img.alt = item.name || "";
      pBox.append(img);
    } else {
      const ph = ms(item.type === "movie" ? "movie" : "tv");
      ph.style.fontSize = "44px";
      pBox.append(ph);
    }

    const badges = $("#det-badges");
    badges.replaceChildren();
    badges.append(el("span", "chip info", item.anime ? "Anime" : (item.type === "movie" ? "Film" : "Serie")));
    if (item.year) badges.append(el("span", "chip", String(item.year)));
    if (item.runtime) badges.append(el("span", "chip", `${item.runtime} Min.`));
    for (const r of (item.ratings || []).slice(0, 2)) {
      badges.append(el("span", "chip ok", `${r.label} ${r.value}`));
    }

    $("#det-title").textContent = item.name || "Unbenannt";
    const subParts = [...(item.genres || []).slice(0, 4)];
    if (item.network) subParts.push(item.network);
    if (item.status) subParts.push(item.status);
    $("#det-sub").textContent = subParts.join(" · ");
    $("#det-summary").textContent = item.summary || "Keine Beschreibung vorhanden.";

    const links = $("#det-links");
    links.replaceChildren();
    if (item.trailer) {
      const a = el("a", "btn outlined");
      a.href = item.trailer;
      a.target = "_blank";
      a.rel = "noopener";
      a.append(ms("smart_display"), "Trailer");
      links.append(a);
    }
    if (item.url) {
      const a = el("a", "btn text");
      a.href = item.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.append(ms("open_in_new"), "Quelle");
      links.append(a);
    }

    renderDetailLocal();
    renderDetailWatch();
    renderDetailCast();
  }

  function renderDetailLocal() {
    const { item, local } = currentDetail;
    const files = local?.dateien || [];
    const chipBox = $("#det-local-chip");
    const box = $("#det-local-content");
    chipBox.replaceChildren();
    box.replaceChildren();

    const readyFiles = files.filter((f) => f.status === "bereit");
    if (readyFiles.length) {
      chipBox.append(el("span", "chip ok", item.type === "movie" ? "Verfügbar" : `${readyFiles.length} Folgen`));
    } else if (files.some((f) => f.status === "umwandlung")) {
      chipBox.append(el("span", "chip warn", "Wandelt um …"));
    } else {
      chipBox.append(el("span", "chip", "Nicht lokal"));
    }

    if (item.type === "movie") {
      const f = files[0];
      if (!f) {
        box.append(el("p", "body-m variant", "Dieser Film liegt noch nicht auf cloudy."));
        if ($("#btn-open-upload")) {
          const up = el("button", "btn tonal");
          up.type = "button";
          up.append(ms("upload"), "Diesen Film hochladen");
          up.addEventListener("click", () => { dlgDetail.close(); openWizardWithMeta(item); });
          box.append(up);
        }
        return;
      }
      box.append(makeLocalFileRow(f, item.name));
      return;
    }

    // Serie / Anime: Staffeln & Episoden
    const seasons = item.seasons?.length
      ? item.seasons.map((s) => s.n)
      : [...new Set([1, ...files.map((f) => f.staffel || 1)])].sort((a, b) => a - b);
    if (!seasons.includes(currentDetailSeason)) currentDetailSeason = seasons[0] || 1;

    const tabs = el("div", "filter-row");
    tabs.style.marginBottom = "12px";
    for (const sn of seasons) {
      const p = el("button", "pill" + (sn === currentDetailSeason ? " active" : ""), `Staffel ${sn}`);
      p.type = "button";
      p.addEventListener("click", () => { currentDetailSeason = sn; renderDetailLocal(); });
      tabs.append(p);
    }
    if ($("#btn-open-upload")) {
      const upS = el("button", "btn text");
      upS.type = "button";
      upS.append(ms("upload"), `Staffel ${currentDetailSeason} hochladen`);
      upS.addEventListener("click", () => { dlgDetail.close(); openWizardWithMeta(item, currentDetailSeason); });
      tabs.append(upS);
    }
    box.append(tabs);

    const eps = (item.episodes || []).filter((e) => e.season === currentDetailSeason);
    const localByEp = new Map();
    for (const f of files) {
      if ((f.staffel || 1) === currentDetailSeason) localByEp.set(f.folge || 0, f);
    }
    const maxNum = Math.max(eps.length ? Math.max(...eps.map((e) => e.number)) : 0, ...localByEp.keys(), 0);
    if (maxNum === 0) {
      box.append(el("p", "body-m variant", "Keine Episodenliste vorhanden – du kannst Folgen direkt hochladen."));
      return;
    }
    const epByNum = new Map(eps.map((e) => [e.number, e]));
    for (let n = 1; n <= maxNum; n++) {
      const epMeta = epByNum.get(n);
      const lf = localByEp.get(n);
      const row = el("div", "ep-row" + (lf?.status === "bereit" ? " ready" : ""));
      row.append(el("div", "ep-num", `E${String(n).padStart(2, "0")}`));
      const txt = el("div", "txt");
      const titleText = lf?.folgentitel || epMeta?.title || `Folge ${n}`;
      txt.append(el("p", "title-m", titleText));
      let sub = epMeta?.runtime ? `${epMeta.runtime} Min.` : "";
      if (lf) {
        if (lf.status === "bereit") sub = `Auf cloudy · ${fmtBytes(lf.groesse)}` + (lf.dauer ? ` · ${fmtDur(lf.dauer)}` : "");
        else if (lf.status === "umwandlung") sub = "Wird in MP4 umgewandelt …";
        else if (lf.status === "fehler") sub = `Fehler: ${lf.fehler}`;
      }
      txt.append(el("p", "body-s variant", sub || "Noch nicht auf cloudy"));
      row.append(txt);

      if (lf && lf.status === "bereit") {
        const playBtn = el("button", "icon-btn");
        playBtn.type = "button";
        playBtn.title = "Im Browser ansehen";
        playBtn.append(ms("play_arrow"));
        playBtn.addEventListener("click", () => openPlayer(lf.id, `${item.name} · S${String(currentDetailSeason).padStart(2, "0")}E${String(n).padStart(2, "0")}`));

        const tvBtn = el("button", "icon-btn");
        tvBtn.type = "button";
        tvBtn.title = "Auf TV (Jetzt schauen)";
        tvBtn.append(ms("tv"));
        tvBtn.addEventListener("click", () => stageStreamingFile(lf.id));

        const delBtn = el("button", "icon-btn danger");
        delBtn.type = "button";
        delBtn.title = "Löschen";
        delBtn.append(ms("delete"));
        delBtn.addEventListener("click", () => deleteLocalFile(lf.id));

        row.append(playBtn, tvBtn, delBtn);
      } else if (!lf && $("#btn-open-upload")) {
        const upBtn = el("button", "icon-btn");
        upBtn.type = "button";
        upBtn.title = "Folge hochladen";
        upBtn.append(ms("upload"));
        upBtn.addEventListener("click", () => {
          dlgDetail.close();
          openWizardWithMeta(item, currentDetailSeason, n, titleText);
        });
        row.append(upBtn);
      }
      box.append(row);
    }
  }

  function makeLocalFileRow(f, label) {
    const wrap = el("div");
    const info = el("p", "body-m");
    if (f.status === "bereit") {
      info.textContent = `${fmtBytes(f.groesse)}${f.dauer ? " · " + fmtDur(f.dauer) : ""} · ${f.rel_pfad}`;
      wrap.append(info);
      const acts = el("div", "actions start");
      const play = el("button", "btn");
      play.type = "button";
      play.append(ms("play_arrow"), "Im Browser ansehen");
      play.addEventListener("click", () => openPlayer(f.id, label));
      const tv = el("button", "btn tonal");
      tv.type = "button";
      tv.append(ms("tv"), "Auf TV (Jetzt schauen)");
      tv.addEventListener("click", () => stageStreamingFile(f.id));
      const del = el("button", "btn text");
      del.type = "button";
      del.style.color = "var(--error)";
      del.append(ms("delete"), "Löschen");
      del.addEventListener("click", () => deleteLocalFile(f.id));
      acts.append(play, tv, del);
      wrap.append(acts);
    } else if (f.status === "umwandlung") {
      info.textContent = "Wird gerade für Browser & TV in MP4 umgewandelt …";
      wrap.append(info);
    } else {
      info.textContent = `Fehler bei Umwandlung: ${f.fehler || "unbekannt"}`;
      info.style.color = "var(--error)";
      wrap.append(info);
      const del = el("button", "btn outlined");
      del.type = "button";
      del.append(ms("delete"), "Eintrag entfernen");
      del.addEventListener("click", () => deleteLocalFile(f.id));
      wrap.append(del);
    }
    return wrap;
  }

  function renderDetailWatch() {
    const { item } = currentDetail;
    const watch = item.watch || {};
    const list = watch.list || [];
    const jw = $("#det-jw-link");
    if (watch.link) { jw.href = watch.link; jw.hidden = false; } else jw.hidden = true;

    const box = $("#det-watch-content");
    box.replaceChildren();
    if (!list.length) {
      box.append(el("p", "body-m variant", "Keine deutschen Streaming-Anbieter gefunden."));
      return;
    }
    const groups = new Map();
    for (const w of list) {
      const t = w.type || "Streamen";
      if (!groups.has(t)) groups.set(t, []);
      groups.get(t).push(w);
    }
    for (const [grp, providers] of groups.entries()) {
      const gEl = el("div", "prov-group");
      gEl.append(el("span", "body-s variant", grp));
      const row = el("div", "prov-list");
      for (const p of providers) {
        const tag = p.url ? el("a", "prov-item") : el("span", "prov-item");
        if (p.url) { tag.href = p.url; tag.target = "_blank"; tag.rel = "noopener"; }
        if (p.logo) {
          const im = el("img");
          im.src = imgProxy(p.logo);
          im.alt = p.name;
          tag.append(im);
        }
        tag.append(document.createTextNode(p.name));
        row.append(tag);
      }
      gEl.append(row);
      box.append(gEl);
    }
  }

  function renderDetailCast() {
    const cast = currentDetail.item.cast || [];
    const box = $("#det-cast-box");
    const list = $("#det-cast-list");
    list.replaceChildren();
    box.hidden = cast.length === 0;
    for (const c of cast.slice(0, 12)) {
      const card = el("div", "card high");
      card.style.cssText = "padding:10px;border-radius:16px;min-width:110px;max-width:110px;margin:0;flex:none";
      if (c.image) {
        const img = el("img");
        img.src = imgProxy(c.image);
        img.alt = c.name;
        img.style.cssText = "width:90px;height:90px;object-fit:cover;border-radius:12px;display:block;margin:0 auto 6px";
        card.append(img);
      }
      const n = el("p", "body-s", c.name);
      n.style.cssText = "font-weight:600;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
      const r = el("p", "body-s variant", c.role || "");
      r.style.cssText = "margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
      card.append(n, r);
      list.append(card);
    }
  }

  async function stageStreamingFile(fileId) {
    try {
      await api(`/api/tv/stage-streaming/${fileId}`, { method: "POST" });
      toast("Wird in „Jetzt schauen“ für den TV bereitgestellt");
      await refreshLibrary();
    } catch (err) { toast(err.message, true); }
  }

  async function deleteLocalFile(fileId) {
    if (!confirm("Datei wirklich vom Speicher löschen?")) return;
    try {
      await api(`/api/mediathek/datei/${fileId}`, { method: "DELETE" });
      toast("Gelöscht");
      if (currentDetail) {
        const fresh = await api(`/api/mediathek/details?ref=${encodeURIComponent(currentDetail.ref)}`);
        currentDetail = { ref: currentDetail.ref, item: fresh.item, local: fresh.local };
        renderDetailLocal();
      }
      await refreshLibrary();
    } catch (err) { toast(err.message, true); }
  }

  // ---------- Video Player ----------
  const dlgPlayer = $("#dlg-player");
  const playerVid = $("#pl-video");
  let playerFileId = null;

  function openPlayer(fileId, title) {
    playerFileId = fileId;
    $("#pl-title").textContent = title || "Wiedergabe";
    playerVid.src = `/api/mediathek/stream/${fileId}`;
    dlgPlayer.showModal();
    playerVid.play().catch(() => {});
  }
  const closePlayer = () => {
    playerVid.pause();
    playerVid.removeAttribute("src");
    playerVid.load();
    dlgPlayer.close();
  };
  $("#pl-close")?.addEventListener("click", closePlayer);
  dlgPlayer?.addEventListener("cancel", closePlayer);
  $("#pl-tv-btn")?.addEventListener("click", () => { if (playerFileId) stageStreamingFile(playerFileId); });

  // ---------- Upload-Wizard ----------
  const dlgWiz = $("#dlg-wizard");
  const wizFileInput = $("#wiz-file-input");
  const wizSearchInput = $("#wiz-search");
  const wizMatchList = $("#wiz-match-list");
  const wizSelectedCard = $("#wiz-selected-card");
  const wizEpisodesBox = $("#wiz-step-episodes");
  const wizFileMapEl = $("#wiz-file-map");
  const wizStartBtn = $("#wiz-start");
  const wizProgBox = $("#wiz-progress-box");

  let wizFiles = []; // [{file, season, episode, episodeTitle}]
  let wizMeta = null;
  let wizSeasonEps = {}; // season -> episodes[]
  let wizXhr = null;
  let wizAborted = false;

  function parseEpClient(filename) {
    const stem = filename.replace(/\.[^.]+$/, "");
    let m = stem.match(/[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})/) || stem.match(/(\d{1,2})x(\d{1,3})/) || stem.match(/Staffel\s*(\d{1,2})\D+Folge\s*(\d{1,3})/i);
    if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
    m = stem.match(/(?:^|[\s._-])(?:[Ee][Pp]?|[Ff]olge|[Ee]pisode)\s*0*(\d{1,3})(?:$|[\s._-])/);
    if (m) return [null, parseInt(m[1], 10)];
    return [null, null];
  }

  function cleanGuessFromFile(filename) {
    return filename
      .replace(/\.[^.]+$/, "")
      .replace(/[Ss]\d{1,2}[\s._-]*[Ee]\d{1,3}.*/g, "")
      .replace(/\b(1080p|720p|2160p|4k|x264|x265|h264|hevc|webrip|bluray|german|deutsch)\b/gi, "")
      .replace(/[._]+/g, " ")
      .trim();
  }

  function resetWizard() {
    wizFiles = [];
    wizMeta = null;
    wizSeasonEps = {};
    wizFileInput.value = "";
    wizSearchInput.value = "";
    wizMatchList.replaceChildren();
    wizSelectedCard.hidden = true;
    wizEpisodesBox.hidden = true;
    wizProgBox.hidden = true;
    $("#wiz-picked-summary").hidden = true;
    $("#wiz-manual-fallback").hidden = true;
    updateWizStartState();
  }

  $("#btn-open-upload")?.addEventListener("click", () => { resetWizard(); dlgWiz.showModal(); });
  $("#btn-empty-upload")?.addEventListener("click", () => { resetWizard(); dlgWiz.showModal(); });

  function openWizardWithMeta(meta, season = 1, episode = null, epTitle = "") {
    resetWizard();
    wizMeta = meta;
    if (meta.episodes?.length) {
      for (const ep of meta.episodes) {
        (wizSeasonEps[ep.season] = wizSeasonEps[ep.season] || []).push(ep);
      }
    }
    $("#wiz-bulk-season").value = String(season || 1);
    renderWizSelected();
    dlgWiz.showModal();
    if (episode) wizPresetEp = { season, episode, epTitle };
  }
  let wizPresetEp = null;

  $("#wiz-drop")?.addEventListener("click", () => wizFileInput.click());
  wizFileInput?.addEventListener("change", () => {
    const files = [...wizFileInput.files];
    if (!files.length) return;
    let fallbackEp = wizPresetEp?.episode || 1;
    const bulkS = parseInt($("#wiz-bulk-season").value || "1", 10) || 1;
    wizFiles = files.map((f, idx) => {
      const [s, e] = parseEpClient(f.name);
      const season = s || wizPresetEp?.season || bulkS;
      const episode = e || (idx === 0 && wizPresetEp?.episode ? wizPresetEp.episode : fallbackEp++);
      return { file: f, season, episode, episodeTitle: idx === 0 && wizPresetEp?.epTitle ? wizPresetEp.epTitle : "" };
    });
    wizPresetEp = null;
    const sum = $("#wiz-picked-summary");
    sum.hidden = false;
    sum.textContent = `${wizFiles.length} ${wizFiles.length === 1 ? "Datei" : "Dateien"} gewählt (${fmtBytes(wizFiles.reduce((a, b) => a + b.file.size, 0))})`;
    if (!wizMeta) {
      const guess = cleanGuessFromFile(files[0].name);
      if (guess && !wizSearchInput.value) {
        wizSearchInput.value = guess;
        searchForWizard(guess);
      }
    }
    renderWizFileMap();
    updateWizStartState();
  });

  let wizSearchTimer = null;
  wizSearchInput?.addEventListener("input", () => {
    clearTimeout(wizSearchTimer);
    wizSearchTimer = setTimeout(() => searchForWizard(wizSearchInput.value), 350);
  });

  async function searchForWizard(q) {
    const query = q.trim();
    wizMatchList.replaceChildren();
    $("#wiz-manual-fallback").hidden = !query;
    if (!query) return;
    wizMatchList.append(el("p", "body-s variant", "Suche …"));
    try {
      const res = await api(`/api/mediathek/suche?q=${encodeURIComponent(query)}`);
      wizMatchList.replaceChildren();
      const hits = res.results || [];
      if (!hits.length) {
        wizMatchList.append(el("p", "body-s variant", "Kein Online-Treffer gefunden."));
        return;
      }
      for (const hit of hits.slice(0, 12)) {
        const b = el("button", "match-item");
        b.type = "button";
        if (hit.poster) {
          const im = el("img");
          im.src = imgProxy(hit.poster);
          im.alt = hit.name;
          b.append(im);
        }
        const txt = el("div", "txt");
        txt.style.flex = "1";
        txt.append(el("p", "title-m", hit.name));
        txt.append(el("p", "body-s variant", `${hit.type === "movie" ? "Film" : (hit.anime ? "Anime" : "Serie")}${hit.year ? " · " + hit.year : ""}`));
        b.append(txt);
        b.addEventListener("click", async () => {
          wizMeta = hit;
          try {
            const det = await api(`/api/mediathek/details?ref=${encodeURIComponent(hit.ref)}`);
            wizMeta = det.item;
            wizSeasonEps = {};
            for (const ep of (wizMeta.episodes || [])) {
              (wizSeasonEps[ep.season] = wizSeasonEps[ep.season] || []).push(ep);
            }
          } catch (_) { /* ok mit Basisdaten */ }
          renderWizSelected();
          renderWizFileMap();
          updateWizStartState();
        });
        wizMatchList.append(b);
      }
    } catch (err) {
      wizMatchList.replaceChildren(el("p", "body-s", err.message));
    }
  }

  $("#wiz-use-custom")?.addEventListener("click", () => {
    const q = wizSearchInput.value.trim();
    if (!q) return;
    const typ = wizFiles.length > 1 ? "show" : (confirm("Ist das ein Film? (Abbrechen = Serie)") ? "movie" : "show");
    wizMeta = { ref: "", type: typ, name: q, year: null };
    renderWizSelected();
    renderWizFileMap();
    updateWizStartState();
  });

  function renderWizSelected() {
    if (!wizMeta) { wizSelectedCard.hidden = true; return; }
    wizSelectedCard.hidden = false;
    wizSelectedCard.replaceChildren();
    const row = el("div");
    row.style.cssText = "display:flex;align-items:center;gap:12px";
    const info = el("div");
    info.style.flex = "1";
    info.append(el("p", "title-m", wizMeta.name));
    info.append(el("p", "body-s variant", `${wizMeta.type === "movie" ? "Film" : (wizMeta.anime ? "Anime" : "Serie")}${wizMeta.year ? " · " + wizMeta.year : ""}`));
    const chg = el("button", "btn text", "Ändern");
    chg.type = "button";
    chg.addEventListener("click", () => { wizMeta = null; renderWizSelected(); renderWizFileMap(); updateWizStartState(); });
    row.append(info, chg);
    wizSelectedCard.append(row);
    wizMatchList.replaceChildren();
  }

  $("#wiz-bulk-season")?.addEventListener("change", () => {
    const s = Math.max(1, parseInt($("#wiz-bulk-season").value || "1", 10) || 1);
    for (const item of wizFiles) item.season = s;
    renderWizFileMap();
  });

  function renderWizFileMap() {
    const isShow = wizMeta && wizMeta.type !== "movie";
    wizEpisodesBox.hidden = !isShow || wizFiles.length === 0;
    wizFileMapEl.replaceChildren();
    if (!isShow || !wizFiles.length) return;
    wizFiles.forEach((item) => {
      const r = el("div", "file-map-row");
      const nameEl = el("span", "body-s", item.file.name);
      nameEl.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
      const sIn = el("input");
      sIn.type = "number"; sIn.min = "1"; sIn.max = "99"; sIn.value = String(item.season); sIn.title = "Staffel";
      sIn.addEventListener("change", () => { item.season = Math.max(1, parseInt(sIn.value || "1", 10)); });
      const eIn = el("input");
      eIn.type = "number"; eIn.min = "1"; eIn.max = "999"; eIn.value = String(item.episode); eIn.title = "Folge";
      eIn.addEventListener("change", () => { item.episode = Math.max(1, parseInt(eIn.value || "1", 10)); });
      r.append(nameEl, sIn, eIn);
      wizFileMapEl.append(r);
    });
  }

  function updateWizStartState() {
    wizStartBtn.disabled = !(wizFiles.length > 0 && wizMeta);
  }

  function uploadOneStreaming(item, ersetzen = false) {
    return new Promise((resolve) => {
      const eps = wizSeasonEps[item.season] || [];
      const epMatch = eps.find((e) => e.number === item.episode);
      const epTitle = item.episodeTitle || epMatch?.title || "";
      const p = new URLSearchParams({
        ref: wizMeta.ref || "",
        name: wizMeta.name || "",
        type: wizMeta.type || "show",
        dateiname: item.file.name,
      });
      if (wizMeta.year) p.set("year", String(wizMeta.year));
      if (wizMeta.type !== "movie") {
        p.set("staffel", String(item.season));
        p.set("folge", String(item.episode));
        if (epTitle) p.set("folgentitel", epTitle);
      }
      if (ersetzen) p.set("ersetzen", "1");

      wizXhr = new XMLHttpRequest();
      wizXhr.open("PUT", `/api/mediathek/hochladen?${p}`);
      wizXhr.setRequestHeader("X-CSRF", CSRF);
      wizXhr.upload.onprogress = (ev) => item.onprogress?.(ev.loaded);
      wizXhr.onload = () => {
        let data = {};
        try { data = JSON.parse(wizXhr.responseText); } catch (_) {}
        resolve({ status: wizXhr.status, data });
      };
      wizXhr.onerror = () => resolve({ status: 0, data: { fehler: "Verbindung unterbrochen" } });
      wizXhr.onabort = () => resolve({ status: -1, data: { fehler: "Abgebrochen" } });
      wizXhr.send(item.file);
    });
  }

  $("#wiz-abort")?.addEventListener("click", () => { wizAborted = true; wizXhr?.abort(); });

  wizStartBtn?.addEventListener("click", async () => {
    if (!wizFiles.length || !wizMeta || wizXhr) return;
    wizAborted = false;
    wizStartBtn.disabled = true;
    wizProgBox.hidden = false;

    const total = wizFiles.reduce((s, i) => s + i.file.size, 0) || 1;
    let doneBytes = 0;
    let okCount = 0;
    let convCount = 0;
    let replaceAll = null;

    for (const [idx, item] of wizFiles.entries()) {
      if (wizAborted) break;
      $("#wiz-prog-title").textContent = `Hochladen ${idx + 1} von ${wizFiles.length}`;
      item.onprogress = (loaded) => {
        $("#wiz-prog-bar").style.width = `${Math.min(100, ((doneBytes + loaded) / total) * 100)}%`;
        $("#wiz-prog-text").textContent = `${item.file.name} · ${fmtBytes(doneBytes + loaded)} von ${fmtBytes(total)}`;
      };
      item.onprogress(0);
      let res = await uploadOneStreaming(item, false);
      if (res.status === 409) {
        if (replaceAll === null) replaceAll = confirm(`„${item.file.name}“ gibt es in der Mediathek schon. Ersetzen?`);
        if (replaceAll) res = await uploadOneStreaming(item, true);
      }
      wizXhr = null;
      doneBytes += item.file.size;
      if (res.status === 200) {
        okCount++;
        if (res.data.status === "umwandlung") convCount++;
      } else if (res.status !== -1) {
        toast(res.data.fehler || "Upload fehlgeschlagen", true);
        break;
      }
    }
    wizXhr = null;
    updateWizStartState();
    if (okCount > 0) {
      dlgWiz.close();
      toast(convCount ? `${okCount} hochgeladen (${convCount} wird in MP4 umgewandelt)` : `${okCount} in Mediathek gespeichert`);
      await refreshLibrary();
    }
  });

  // ---------- Einstellungen ----------
  const dlgSet = $("#dlg-settings");
  async function openSettings() {
    dlgSet.showModal();
    try {
      const s = await api("/api/mediathek/einstellungen");
      const tmdbIn = $("#set-tmdb");
      tmdbIn.value = s.has_tmdb_key ? "__KEEP__" : "";
      $("#set-tmdb-hint").textContent = s.has_tmdb_key
        ? `Key gespeichert (${s.tmdb_key_hinweis}). Leer lassen zum Löschen oder neuen eingeben.`
        : "Für Filme & deutsche Streaming-Anbieter (tmdb.org, kostenlos).";
      $("#set-streaming").value = s.streaming_ordner || "Streaming";
      $("#set-medien").value = s.medien_ordner || "Medien";
      $("#set-temp").value = s.temp_ordner || "Jetzt schauen";
      $("#set-vhours").value = String(s.video_ablauf_stunden ?? 6);
      $("#set-idays").value = String(s.bild_ablauf_tage ?? 7);
      const qset = new Set(s.quellen || []);
      $$("#settings-form input[name=q]").forEach((cb) => { cb.checked = qset.has(cb.value); });
    } catch (err) { toast(err.message, true); }
  }
  $("#btn-settings")?.addEventListener("click", openSettings);
  $("#link-tmdb-settings")?.addEventListener("click", (e) => { e.preventDefault(); openSettings(); });

  $("#settings-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const quellen = $$("#settings-form input[name=q]:checked").map((cb) => cb.value);
    if (!quellen.length) { toast("Mindestens eine Quelle aktiv lassen", true); return; }
    try {
      await api("/api/mediathek/einstellungen", {
        method: "POST",
        body: {
          tmdb_key: $("#set-tmdb").value.trim(),
          streaming_ordner: $("#set-streaming").value.trim(),
          medien_ordner: $("#set-medien").value.trim(),
          temp_ordner: $("#set-temp").value.trim(),
          video_ablauf_stunden: parseInt($("#set-vhours").value || "6", 10),
          bild_ablauf_tage: parseInt($("#set-idays").value || "7", 10),
          quellen,
        },
      });
      dlgSet.close();
      toast("Einstellungen gespeichert");
      location.reload();
    } catch (err) { toast(err.message, true); }
  });

  // Start
  refreshLibrary();
})();
