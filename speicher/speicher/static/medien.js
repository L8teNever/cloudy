// Medien & TV: Eigene Videos & Fotos durchsuchen, hochladen, mehrfach auswählen und nach „Jetzt schauen“ kopieren
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

  const fmtBytes = (n) => {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Number(n || 0);
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i < 2 ? v.toFixed(0) : v.toFixed(1)).replace(".", ",") + " " + u[i];
  };
  const fmtLeft = (ts) => {
    if (!ts) return "bis gelöscht";
    const diff = ts - Date.now() / 1000;
    if (diff <= 0) return "läuft ab";
    if (diff < 3600) return `noch ${Math.ceil(diff / 60)} Min.`;
    if (diff < 86400) return `noch ${Math.ceil(diff / 3600)} Std.`;
    return `noch ${Math.ceil(diff / 86400)} Tage`;
  };

  async function api(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET") headers["X-CSRF"] = CSRF;
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, { ...opts, headers });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(data.fehler || `Fehler ${res.status}`);
    return data;
  }

  // ---------- Zustand ----------
  let mediaItems = [];
  let tvItems = [];
  let kindFilter = "all";
  let shareFilter = "";
  const selectedImages = new Set(); // pool_rel
  let pollTimer = null;

  const tvBanner = $("#tv-banner");
  const tvListEl = $("#tv-list");
  const gridEl = $("#med-grid");
  const emptyEl = $("#med-empty");
  const shareSelect = $("#med-share-filter");
  const selBar = $("#sel-bar");

  // ---------- TV-Banner ----------
  function renderTv() {
    tvBanner.hidden = tvItems.length === 0;
    tvListEl.replaceChildren();
    for (const item of tvItems) {
      const row = el("div", "tv-item");
      const icon = el("div", "shape round " + (item.art === "bild" ? "tertiary" : "primary"));
      icon.style.cssText = "width:38px;height:38px";
      icon.append(ms(item.art === "bild" ? "image" : "movie"));
      const txt = el("div", "txt");
      txt.append(el("p", "title-m", item.titel || item.ziel_pfad));
      let sub = "";
      if (item.status === "kopiere") sub = `Kopiere in TV-Ordner … ${item.fortschritt || 0}%`;
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
      del.title = "Aus „Jetzt schauen“ entfernen";
      del.append(ms("close"));
      del.addEventListener("click", async () => {
        try {
          await api(`/api/tv/item/${item.id}`, { method: "DELETE" });
          toast("Aus TV-Ordner entfernt");
          await refreshMedia();
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
      await refreshMedia();
    } catch (err) { toast(err.message, true); }
  });

  // ---------- Galerie ----------
  function renderSelectionBar() {
    const cnt = selectedImages.size;
    selBar.hidden = cnt === 0;
    $("#sel-count").textContent = cnt === 1 ? "1 Bild ausgewählt" : `${cnt} Bilder ausgewählt`;
  }

  function renderGrid() {
    gridEl.replaceChildren();
    emptyEl.hidden = mediaItems.length > 0;
    const tvSources = new Set(tvItems.map((x) => x.quelle_pfad));

    for (const item of mediaItems) {
      const card = el("div", "gal-card" + (selectedImages.has(item.pool_rel) ? " selected" : ""));
      const url = `/api/medien/datei?rel=${encodeURIComponent(item.pool_rel)}`;

      if (item.kind === "image") {
        const img = el("img");
        img.src = url;
        img.alt = item.name;
        img.loading = "lazy";
        card.append(img);

        const chk = el("button", "gal-check");
        chk.type = "button";
        chk.title = "Auswählen";
        chk.append(ms("check"));
        chk.addEventListener("click", (e) => {
          e.stopPropagation();
          if (selectedImages.has(item.pool_rel)) selectedImages.delete(item.pool_rel);
          else selectedImages.add(item.pool_rel);
          card.classList.toggle("selected", selectedImages.has(item.pool_rel));
          renderSelectionBar();
        });
        card.append(chk);

        if (tvSources.has(item.pool_rel)) {
          const b = el("span", "gal-badge");
          b.append(ms("tv"), "TV");
          card.append(b);
        }
      } else {
        const vid = el("video");
        vid.src = url + "#t=0.5";
        vid.muted = true;
        vid.preload = "metadata";
        card.append(vid);

        const badge = el("span", "gal-badge");
        badge.append(ms("movie"), tvSources.has(item.pool_rel) ? "Auf TV" : "Video");
        card.append(badge);

        const tvBtn = el("button", "gal-tv");
        tvBtn.type = "button";
        tvBtn.append(ms("tv"), "TV");
        tvBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          await stageVideo(item);
        });
        card.append(tvBtn);
      }

      card.append(el("div", "gal-cap", item.name));
      card.addEventListener("click", () => {
        if (item.kind === "image" && selectedImages.size > 0) {
          if (selectedImages.has(item.pool_rel)) selectedImages.delete(item.pool_rel);
          else selectedImages.add(item.pool_rel);
          card.classList.toggle("selected", selectedImages.has(item.pool_rel));
          renderSelectionBar();
        } else {
          openPreview(item);
        }
      });
      gridEl.append(card);
    }
    renderSelectionBar();
  }

  async function stageVideo(item) {
    try {
      await api("/api/tv/stage-video", {
        method: "POST",
        body: { pool_rel: item.pool_rel, titel: item.name.replace(/\.[^.]+$/, "") },
      });
      toast(`„${item.name}“ wird für den TV bereitgestellt`);
      await refreshMedia();
    } catch (err) { toast(err.message, true); }
  }

  $("#sel-send-tv")?.addEventListener("click", async () => {
    if (!selectedImages.size) return;
    const days = parseInt($("#sel-days").value || "7", 10);
    try {
      const res = await api("/api/tv/stage-images", {
        method: "POST",
        body: { pool_rels: [...selectedImages], ablauf_tage: days },
      });
      selectedImages.clear();
      toast(`${res.count} Bilder in „Jetzt schauen“ kopiert`);
      await refreshMedia();
    } catch (err) { toast(err.message, true); }
  });
  $("#sel-clear")?.addEventListener("click", () => { selectedImages.clear(); renderGrid(); });

  async function refreshMedia() {
    try {
      const p = new URLSearchParams({ kind: kindFilter });
      if (shareFilter) p.set("share", shareFilter);
      const data = await api(`/api/medien/liste?${p}`);
      mediaItems = data.items || [];
      tvItems = data.tv || [];
      const shares = data.shares || [];
      if (shares.length > 1) {
        shareSelect.hidden = false;
        const cur = shareSelect.value;
        shareSelect.replaceChildren(el("option", "", "Alle Ordner"));
        shareSelect.firstChild.value = "";
        for (const s of shares) {
          const o = el("option", "", s);
          o.value = s;
          shareSelect.append(o);
        }
        shareSelect.value = cur;
      } else {
        shareSelect.hidden = true;
      }
      renderTv();
      renderGrid();
      clearTimeout(pollTimer);
      if (tvItems.some((t) => t.status === "kopiere" || t.status === "umwandlung")) {
        pollTimer = setTimeout(refreshMedia, 2500);
      }
    } catch (_) {}
  }

  $$("#med-filters .pill").forEach((b) => b.addEventListener("click", () => {
    $$("#med-filters .pill").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    kindFilter = b.dataset.kind;
    refreshMedia();
  }));
  shareSelect?.addEventListener("change", () => {
    shareFilter = shareSelect.value;
    refreshMedia();
  });

  // ---------- Vorschau-Lightbox / Video-Player ----------
  const dlgPrev = $("#dlg-preview");
  const pvImg = $("#pv-img");
  const pvVid = $("#pv-vid");
  let pvItem = null;

  function openPreview(item) {
    pvItem = item;
    $("#pv-title").textContent = item.name;
    const url = `/api/medien/datei?rel=${encodeURIComponent(item.pool_rel)}`;
    if (item.kind === "image") {
      pvVid.pause();
      pvVid.hidden = true;
      pvImg.src = url;
      pvImg.hidden = false;
    } else {
      pvImg.hidden = true;
      pvVid.src = url;
      pvVid.hidden = false;
      pvVid.play().catch(() => {});
    }
    dlgPrev.showModal();
  }
  const closePreview = () => {
    pvVid.pause();
    pvVid.removeAttribute("src");
    pvVid.load();
    pvImg.removeAttribute("src");
    dlgPrev.close();
  };
  $("#pv-close")?.addEventListener("click", closePreview);
  dlgPrev?.addEventListener("cancel", closePreview);
  $("#pv-tv-btn")?.addEventListener("click", async () => {
    if (!pvItem) return;
    if (pvItem.kind === "video") await stageVideo(pvItem);
    else {
      try {
        await api("/api/tv/stage-images", {
          method: "POST",
          body: { pool_rels: [pvItem.pool_rel], ablauf_tage: parseInt($("#sel-days").value || "7", 10) },
        });
        toast("Bild in „Jetzt schauen“ kopiert");
        await refreshMedia();
      } catch (err) { toast(err.message, true); }
    }
  });

  // ---------- Direkt-Upload (Video & Bilder) ----------
  const upBox = $("#med-upload-box");
  let activeXhr = null;
  let aborted = false;

  $("#btn-up-video")?.addEventListener("click", () => $("#up-video-in").click());
  $("#btn-up-bilder")?.addEventListener("click", () => $("#up-bilder-in").click());
  $("#med-up-abort")?.addEventListener("click", () => { aborted = true; activeXhr?.abort(); });

  function putFile(file, params) {
    return new Promise((resolve) => {
      activeXhr = new XMLHttpRequest();
      activeXhr.open("PUT", `/api/medien/hochladen?${params}`);
      activeXhr.setRequestHeader("X-CSRF", CSRF);
      activeXhr.upload.onprogress = (ev) => file._onprog?.(ev.loaded);
      activeXhr.onload = () => {
        let data = {};
        try { data = JSON.parse(activeXhr.responseText); } catch (_) {}
        resolve({ status: activeXhr.status, data });
      };
      activeXhr.onerror = () => resolve({ status: 0, data: { fehler: "Verbindung unterbrochen" } });
      activeXhr.onabort = () => resolve({ status: -1, data: { fehler: "Abgebrochen" } });
      activeXhr.send(file);
    });
  }

  async function runUploads(files, extraParams) {
    if (!files.length || activeXhr) return;
    aborted = false;
    upBox.hidden = false;
    const total = files.reduce((s, f) => s + f.size, 0) || 1;
    let doneBytes = 0;
    let ok = 0;
    for (const [idx, f] of files.entries()) {
      if (aborted) break;
      $("#med-up-title").textContent = `Hochladen ${idx + 1} von ${files.length}`;
      f._onprog = (loaded) => {
        $("#med-up-bar").style.width = `${Math.min(100, ((doneBytes + loaded) / total) * 100)}%`;
        $("#med-up-text").textContent = `${f.name} · ${fmtBytes(doneBytes + loaded)} von ${fmtBytes(total)}`;
      };
      f._onprog(0);
      const p = new URLSearchParams({ dateiname: f.name, ziel: "tv", ...extraParams });
      const res = await putFile(f, p);
      activeXhr = null;
      doneBytes += f.size;
      if (res.status === 200) ok++;
      else if (res.status !== -1) {
        toast(res.data.fehler || "Upload fehlgeschlagen", true);
        break;
      }
    }
    upBox.hidden = true;
    if (ok > 0) {
      toast(ok === 1 ? "In Medien gespeichert & für TV bereitgestellt" : `${ok} Dateien in „Jetzt schauen“ bereitgestellt`);
      await refreshMedia();
    }
  }

  $("#up-video-in")?.addEventListener("change", () => {
    const files = [...$("#up-video-in").files];
    $("#up-video-in").value = "";
    runUploads(files, {});
  });
  $("#up-bilder-in")?.addEventListener("change", () => {
    const files = [...$("#up-bilder-in").files];
    $("#up-bilder-in").value = "";
    const days = $("#up-img-days")?.value || "7";
    runUploads(files, { ablauf_tage: days });
  });

  refreshMedia();
})();
