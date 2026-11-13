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

  // Hochladen: nacheinander, jede Datei als eigener PUT (große Dateien ohne Zwischenspeicher)
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

  async function starten(liste) {
    if (!liste.length || xhr) return;
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
