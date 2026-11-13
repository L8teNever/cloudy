// Dateien-Seite: Hochladen (Knopf, Ordner, Ziehen & Ablegen) mit Fortschritt, Umbenennen-Dialog.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const box = $("#upload");
  if (!box) return;

  // Umbenennen: einen Dialog für alle Einträge befüllen
  const dlgUm = $("#dlg-umbenennen");
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-umbenennen]");
    if (!b) return;
    $("[name=pfad]", dlgUm).value = b.dataset.umbenennen;
    const input = $("[name=neu]", dlgUm);
    input.value = b.dataset.name;
    dlgUm.showModal();
    const punkt = b.dataset.name.lastIndexOf(".");
    input.focus();
    input.setSelectionRange(0, punkt > 0 ? punkt : b.dataset.name.length);
  });

  // Auswahl: Dateien oder ganzer Ordner (am Handy gibt es nur Dateien)
  const dlgUp = $("#dlg-upload");
  const ordnerGeht = "webkitdirectory" in document.createElement("input") && !/Android|iPhone|iPad/i.test(navigator.userAgent);
  $("[data-upload]").addEventListener("click", () => (ordnerGeht ? dlgUp.showModal() : $("#up-dateien").click()));
  $$("[data-pick]").forEach((b) => b.addEventListener("click", () => { dlgUp.close(); document.getElementById(b.dataset.pick).click(); }));
  $$("#up-dateien, #up-ordner").forEach((inp) => inp.addEventListener("change", () => {
    starten([...inp.files].map((f) => ({ file: f, pfad: f.webkitRelativePath || f.name })));
    inp.value = "";
  }));

  // Ziehen & Ablegen, auch ganze Ordner
  const hint = $(".drop-hint");
  let tiefe = 0;
  const istDatei = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", (e) => { if (istDatei(e)) { tiefe++; hint.hidden = false; } });
  addEventListener("dragleave", () => { if (--tiefe <= 0) { tiefe = 0; hint.hidden = true; } });
  addEventListener("dragover", (e) => { if (istDatei(e)) e.preventDefault(); });
  addEventListener("drop", async (e) => {
    if (!istDatei(e)) return;
    e.preventDefault();
    tiefe = 0;
    hint.hidden = true;
    const eintraege = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    const liste = [];
    if (eintraege.length) {
      for (const en of eintraege) await sammeln(en, "", liste);
    } else {
      [...e.dataTransfer.files].forEach((f) => liste.push({ file: f, pfad: f.name }));
    }
    starten(liste);
  });

  async function sammeln(entry, prefix, liste) {
    if (entry.isFile) {
      const file = await new Promise((ok, err) => entry.file(ok, err));
      liste.push({ file, pfad: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let teil;
      do {
        teil = await new Promise((ok, err) => reader.readEntries(ok, err));
        for (const k of teil) await sammeln(k, prefix + entry.name + "/", liste);
      } while (teil.length);
    }
  }

  // Hochladen: wenige Dateien nacheinander, jede als eigener PUT (große Dateien ohne Zwischenspeicher)
  const titel = $("[data-upload-titel]", box);
  const balken = $("[data-upload-balken]", box);
  const text = $("[data-upload-text]", box);
  let xhr = null;
  let abgebrochen = false;
  $("[data-upload-abbrechen]", box).addEventListener("click", () => { abgebrochen = true; if (xhr) xhr.abort(); });
  addEventListener("beforeunload", (e) => { if (xhr) e.preventDefault(); });

  const fmt = (n) => {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return (i < 2 ? n.toFixed(0) : n.toFixed(1)).replace(".", ",") + " " + u[i];
  };

  function senden(item, ersetzen) {
    return new Promise((resolve) => {
      xhr = new XMLHttpRequest();
      const url = box.dataset.ziel + "/" + item.pfad.split("/").map(encodeURIComponent).join("/") + (ersetzen ? "?ersetzen=1" : "");
      xhr.open("PUT", url);
      xhr.setRequestHeader("X-CSRF", box.dataset.csrf);
      xhr.upload.onprogress = (ev) => item.onprogress(ev.loaded);
      xhr.onload = () => {
        let msg = "";
        try { msg = JSON.parse(xhr.responseText).fehler || ""; } catch (_) { /* leer */ }
        resolve({ status: xhr.status, msg });
      };
      xhr.onerror = () => resolve({ status: 0, msg: "Verbindung unterbrochen" });
      xhr.onabort = () => resolve({ status: -1, msg: "Abgebrochen" });
      xhr.send(item.file);
    });
  }

  // ---------- Viele Dateien als ein Paket (TAR) ----------
  // Der Browser liest dafür nichts in den Speicher: das Paket ist ein Blob aus Kopfdaten und den Dateien selbst.
  // Der Server entpackt es beim Empfang direkt in den Ordner.
  const AB_ANZAHL = 10;
  const enc = new TextEncoder();
  const NULL1K = new Uint8Array(1024);

  function oktal(buf, pos, len, wert) {
    const t = wert.toString(8).padStart(len - 1, "0");
    for (let i = 0; i < len - 1; i++) buf[pos + i] = t.charCodeAt(i);
    buf[pos + len - 1] = 0;
  }
  function kopf(name, groesse, typ, mtime) {
    const h = new Uint8Array(512);
    const n = enc.encode(name).slice(0, 99);
    h.set(n, 0);
    oktal(h, 100, 8, 0o644);
    oktal(h, 108, 8, 0);
    oktal(h, 116, 8, 0);
    oktal(h, 124, 12, groesse < 0o77777777777 ? groesse : 0);
    oktal(h, 136, 12, Math.floor(mtime / 1000));
    h.fill(32, 148, 156);
    h[156] = typ.charCodeAt(0);
    h.set(enc.encode("ustar\0" + "00"), 257);
    let summe = 0;
    for (const b of h) summe += b;
    const c = summe.toString(8).padStart(6, "0");
    for (let i = 0; i < 6; i++) h[148 + i] = c.charCodeAt(i);
    h[154] = 0; h[155] = 32;
    return h;
  }
  function paxEintrag(key, wert) {
    const rest = enc.encode(` ${key}=${wert}\n`).length;
    let len = rest + String(rest).length;
    if (String(len).length !== String(rest).length) len = rest + String(len).length;
    return `${len} ${key}=${wert}\n`;
  }
  const auffuellen = (n) => new Uint8Array((512 - (n % 512)) % 512);

  function paket(liste) {
    const teile = [];
    liste.forEach((item, i) => {
      const pax = enc.encode(paxEintrag("path", item.pfad) + paxEintrag("size", String(item.file.size)));
      teile.push(kopf(`PaxHeaders/${i}`, pax.length, "x", Date.now()), pax, auffuellen(pax.length));
      teile.push(kopf(`datei${i}`, item.file.size, "0", item.file.lastModified || Date.now()), item.file, auffuellen(item.file.size));
    });
    teile.push(NULL1K);
    return new Blob(teile, { type: "application/x-tar" });
  }

  function paketSenden(blob, ersetzen, onprogress) {
    return new Promise((resolve) => {
      xhr = new XMLHttpRequest();
      xhr.open("PUT", box.dataset.paket + (ersetzen ? "?ersetzen=1" : ""));
      xhr.setRequestHeader("X-CSRF", box.dataset.csrf);
      xhr.setRequestHeader("Content-Type", "application/x-tar");
      xhr.upload.onprogress = (ev) => onprogress(ev.loaded);
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* leer */ }
        resolve({ status: xhr.status, data, msg: data.fehler && !Array.isArray(data.fehler) ? data.fehler : "" });
      };
      xhr.onerror = () => resolve({ status: 0, data: {}, msg: "Verbindung unterbrochen" });
      xhr.onabort = () => resolve({ status: -1, data: {}, msg: "Abgebrochen" });
      xhr.send(blob);
    });
  }

  async function alsPaket(liste) {
    abgebrochen = false;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const blob = paket(liste);
    const gesamt = blob.size || 1;
    titel.textContent = `${liste.length} Dateien als ein Paket`;
    const fortschritt = (geladen) => {
      balken.style.width = Math.min(100, (geladen / gesamt) * 100) + "%";
      text.textContent = `${fmt(geladen)} von ${fmt(gesamt)}` + (geladen >= gesamt ? " · wird entpackt …" : "");
    };
    fortschritt(0);
    let r = await paketSenden(blob, false, fortschritt);
    let ok = r.data.ok || 0;
    const fehler = [...(Array.isArray(r.data.fehler) ? r.data.fehler : [])];
    const doppelt = r.data.uebersprungen || [];
    if (r.status === 200 && doppelt.length && !abgebrochen) {
      xhr = null;
      if (confirm(`${doppelt.length} ${doppelt.length === 1 ? "Datei gibt" : "Dateien gibt"} es schon. Ersetzen?`)) {
        const nochmal = liste.filter((i) => doppelt.includes(i.pfad));
        const blob2 = paket(nochmal);
        titel.textContent = `${nochmal.length} Dateien ersetzen`;
        const r2 = await paketSenden(blob2, true, (g) => {
          balken.style.width = Math.min(100, (g / (blob2.size || 1)) * 100) + "%";
          text.textContent = `${fmt(g)} von ${fmt(blob2.size)}`;
        });
        ok += r2.data.ok || 0;
        if (r2.status !== 200) fehler.push(r2.msg || "Fehler " + r2.status);
      }
    }
    xhr = null;
    const params = new URLSearchParams();
    if (r.status === -1) params.set("fehler", `Abgebrochen, ${ok} hochgeladen`);
    else if (r.status !== 200) params.set("fehler", `${ok} hochgeladen, Fehler: ${r.msg || "Fehler " + r.status}`);
    else if (fehler.length) params.set("fehler", `${ok} hochgeladen, ${fehler.length} nicht: ${fehler.slice(0, 3).join(" · ")}`);
    else params.set("ok", ok === 1 ? "1 Datei hochgeladen" : `${ok} Dateien hochgeladen`);
    location.href = location.pathname + "?" + params;
  }

  async function starten(liste) {
    if (!liste.length || xhr) return;
    if (liste.length >= AB_ANZAHL && box.dataset.paket) return alsPaket(liste);
    abgebrochen = false;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const gesamt = liste.reduce((s, i) => s + i.file.size, 0) || 1;
    let fertig = 0;
    let ok = 0;
    const fehler = [];
    let alleErsetzen = null;
    for (const [n, item] of liste.entries()) {
      if (abgebrochen) break;
      titel.textContent = `Hochladen ${n + 1} von ${liste.length}`;
      item.onprogress = (geladen) => {
        balken.style.width = Math.min(100, ((fertig + geladen) / gesamt) * 100) + "%";
        text.textContent = `${item.pfad} · ${fmt(fertig + geladen)} von ${fmt(gesamt)}`;
      };
      item.onprogress(0);
      let r = await senden(item, false);
      if (r.status === 409 && !r.msg.includes("ist ein Ordner")) {
        if (alleErsetzen === null) {
          alleErsetzen = confirm(`„${item.pfad}“ gibt es schon. Ersetzen?` + (liste.length > 1 ? "\n(Gilt auch für alle weiteren doppelten Dateien.)" : ""));
        }
        if (alleErsetzen) r = await senden(item, true);
      }
      xhr = null;
      fertig += item.file.size;
      if (r.status === 200) ok++;
      else if (r.status !== -1 && !(r.status === 409 && alleErsetzen === false)) fehler.push(`${item.pfad}: ${r.msg || "Fehler " + r.status}`);
      if (r.status === 507) break;
    }
    const params = new URLSearchParams();
    if (fehler.length) params.set("fehler", `${ok} hochgeladen, ${fehler.length} nicht: ${fehler.slice(0, 3).join(" · ")}`);
    else if (abgebrochen) params.set("fehler", `Abgebrochen, ${ok} hochgeladen`);
    else params.set("ok", ok === 1 ? "1 Datei hochgeladen" : `${ok} Dateien hochgeladen`);
    location.href = location.pathname + "?" + params;
  }
})();
