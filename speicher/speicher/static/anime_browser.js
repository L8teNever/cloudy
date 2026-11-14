// Anime: Live-Ansicht des digitalen Browsers (Screencast + Maus/Tastatur über WebSocket)
(() => {
  const $ = (s) => document.querySelector(s);
  const bild = $("#br-bild");
  if (!bild) return;
  const status = $("#br-status");
  const W = 1280, H = 800;
  let ws = null, nr = 0, offen = false, zu = false;

  const zeige = (text, icon, fehler) => {
    status.textContent = "";
    const i = document.createElement("span");
    i.className = "ms";
    i.textContent = icon;
    status.append(i, text);
    status.classList.toggle("err", Boolean(fehler));
  };
  const senden = (method, params = {}) => {
    if (!offen) return;
    ws.send(JSON.stringify({ id: ++nr, method, params }));
    return nr;
  };

  function verbinden() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/browser/ws`);
    ws.onopen = () => {
      offen = true;
      zeige("Live", "videocam");
      senden("Page.enable");
      senden("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
      senden("Page.bringToFront");
      senden("Page.startScreencast", { format: "jpeg", quality: 60, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
      adresse();
    };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.fehler) { zeige(m.fehler, "error", true); return; }
      if (m.method === "Page.screencastFrame") {
        bild.src = "data:image/jpeg;base64," + m.params.data;
        senden("Page.screencastFrameAck", { sessionId: m.params.sessionId });
      } else if (m.method === "Page.frameNavigated" && !m.params.frame.parentId) {
        $("#br-url").value = m.params.frame.url;
      } else if (m.result && m.result.result && typeof m.result.result.value === "string" && m.id === urlId) {
        $("#br-url").value = m.result.result.value;
      }
    };
    ws.onclose = () => {
      offen = false;
      if (zu) return;
      zeige("Getrennt – verbinde neu …", "sync_problem", true);
      setTimeout(verbinden, 2000);
    };
  }
  let urlId = 0;
  const adresse = () => { urlId = senden("Runtime.evaluate", { expression: "location.href", returnByValue: true }); };

  // --- Adressleiste und Navigation ---
  $("#br-go").addEventListener("submit", (e) => {
    e.preventDefault();
    let u = $("#br-url").value.trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) u = "https://" + u;
    senden("Page.navigate", { url: u });
  });
  $("#br-reload").addEventListener("click", () => senden("Page.reload"));
  const verlauf = (schritt) => {
    const id = senden("Page.getNavigationHistory");
    const h = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.id !== id) return;
      ws.removeEventListener("message", h);
      const r = m.result || {};
      const e = (r.entries || [])[(r.currentIndex || 0) + schritt];
      if (e) senden("Page.navigateToHistoryEntry", { entryId: e.id });
    };
    ws.addEventListener("message", h);
  };
  $("#br-back").addEventListener("click", () => verlauf(-1));
  $("#br-fwd").addEventListener("click", () => verlauf(1));

  // --- Maus / Touch ---
  const pos = (e) => {
    const r = bild.getBoundingClientRect();
    const w = bild.naturalWidth || W, h = bild.naturalHeight || H;
    return { x: Math.round((e.clientX - r.left) * (w / r.width)), y: Math.round((e.clientY - r.top) * (h / r.height)) };
  };
  const knopf = (e) => (e.button === 2 ? "right" : e.button === 1 ? "middle" : "left");
  const maus = (type, e, extra = {}) => senden("Input.dispatchMouseEvent", { type, ...pos(e), button: "none", ...extra });
  let touch = null, letzterMove = 0;
  bild.addEventListener("contextmenu", (e) => e.preventDefault());
  bild.addEventListener("pointerdown", (e) => {
    bild.focus();
    bild.setPointerCapture(e.pointerId);
    if (e.pointerType === "touch") { touch = { x: e.clientX, y: e.clientY, bewegt: false, e }; return; }
    maus("mousePressed", e, { button: knopf(e), clickCount: 1 });
  });
  bild.addEventListener("pointermove", (e) => {
    if (touch) {
      const dx = e.clientX - touch.x, dy = e.clientY - touch.y;
      if (Math.abs(dx) + Math.abs(dy) > 6) touch.bewegt = true;
      if (touch.bewegt) {
        maus("mouseWheel", touch.e, { deltaX: -dx * 2, deltaY: -dy * 2 });
        touch.x = e.clientX; touch.y = e.clientY;
      }
      return;
    }
    const jetzt = performance.now();
    if (jetzt - letzterMove < 40) return;
    letzterMove = jetzt;
    maus("mouseMoved", e, { button: e.buttons ? "left" : "none", buttons: e.buttons });
  });
  bild.addEventListener("pointerup", (e) => {
    if (touch) {
      if (!touch.bewegt) {
        maus("mousePressed", touch.e, { button: "left", clickCount: 1 });
        maus("mouseReleased", touch.e, { button: "left", clickCount: 1 });
      }
      touch = null;
      return;
    }
    maus("mouseReleased", e, { button: knopf(e), clickCount: 1 });
  });
  bild.addEventListener("wheel", (e) => {
    e.preventDefault();
    maus("mouseWheel", e, { deltaX: e.deltaX, deltaY: e.deltaY });
  }, { passive: false });

  // --- Tastatur ---
  const SONDER = {
    Enter: 13, Backspace: 8, Tab: 9, Escape: 27, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
    Home: 36, End: 35, PageUp: 33, PageDown: 34,
  };
  const mod = (e) => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
  bild.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey) return; // Browser-Kürzel (Einfügen: Textfeld darunter)
    const sonder = SONDER[e.key];
    if (!sonder && e.key.length !== 1) return;
    e.preventDefault();
    senden("Input.dispatchKeyEvent", {
      type: "keyDown", key: e.key, code: e.code, modifiers: mod(e),
      windowsVirtualKeyCode: sonder || e.key.toUpperCase().charCodeAt(0),
      text: e.key === "Enter" ? "\r" : sonder ? undefined : e.key,
    });
  });
  bild.addEventListener("keyup", (e) => {
    const sonder = SONDER[e.key];
    if (!sonder && e.key.length !== 1) return;
    e.preventDefault();
    senden("Input.dispatchKeyEvent", {
      type: "keyUp", key: e.key, code: e.code, modifiers: mod(e),
      windowsVirtualKeyCode: sonder || e.key.toUpperCase().charCodeAt(0),
    });
  });
  $("#br-text").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("#br-eingabe").value;
    if (t) senden("Input.insertText", { text: t });
    $("#br-eingabe").value = "";
  });
  $("#br-enter").addEventListener("click", () => {
    const p = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 };
    senden("Input.dispatchKeyEvent", { type: "keyDown", text: "\r", ...p });
    senden("Input.dispatchKeyEvent", { type: "keyUp", ...p });
  });

  window.addEventListener("beforeunload", () => { zu = true; });
  verbinden();
})();
