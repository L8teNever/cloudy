// cloudy: Anbindung der 3D-Karte ans Heimnetz.
// 1) Bildschirme folgen dem Online-Status der zugeordneten Geräte (/karte/einstellungen).
// 2) Jeder Raum hat eine eigene Adresse (z. B. /simonszimmer/) – Raum öffnen ändert die URL,
//    die URL direkt aufrufen öffnet den Raum, Zurück im Browser geht wieder zur Übersicht.
(function () {
  const BASE = (document.querySelector('meta[name="karte-base"]') || {}).content || '';

  // Raum-ID auf der Karte -> Adresse
  const ROOM_SLUGS = {
    'label-simon': 'simonszimmer',
    'label-arbeit': 'arbeitszimmer',
    'label-abstell': 'abstellkammer',
    'label-flur1': 'fluroben',
    'label-flur2': 'flurvorne',
    'label-bad': 'neuesbad',
    'label-martin': 'martinszimmer',
    'label-eingangsflur': 'eingangsflur',
    'label-kueche': 'kueche',
    'label-esszimmer': 'esszimmer',
    'label-wohnzimmer': 'wohnzimmer',
    'label-schlaf': 'schlafzimmer',
    'label-wc-w2': 'wc',
    'label-bad-w2': 'bad',
    'label-melanie': 'melanieszimmer'
  };
  const SLUG_ROOMS = Object.fromEntries(Object.entries(ROOM_SLUGS).map(([id, slug]) => [slug, id]));

  // ---------------------------------------------------------------- Raum- und Menü-Adressen
  // /simonszimmer/ = Raum, /simonszimmer/rollladen-fenster/ = Raum mit offenem Menü (Rollladen, LED, Internet).
  const LED_SLUGS = { simon: 'tisch-led', schlaf: 'decken-led' };
  const LED_ROOMS = { simon: 'label-simon', schlaf: 'label-schlaf' };
  const NET_SLUG = 'internet';

  const slugify = t => String(t || '').toLowerCase()
    .replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  const shutterSlug = sh => slugify(sh.shortLabel || sh.name || sh.id);
  const isVisible = id => Boolean(document.getElementById(id)?.classList.contains('visible'));

  function currentRoom() {
    return typeof activeRoomId !== 'undefined' ? activeRoomId : null;
  }

  // Welches Menü ist gerade offen? -> Teil der Adresse
  function currentPopupSlug() {
    const room = currentRoom();
    if (isVisible('net-popup') && openScreenId) {
      const tv = (window.screensRegistry || []).find(s => s.id === openScreenId);
      return tv && tv.roomId === room ? NET_SLUG : '';
    }
    if (isVisible('shutter-popup') && typeof activeShutterId !== 'undefined' && activeShutterId) {
      const sh = (window.shuttersRegistry || []).find(s => s.id === activeShutterId);
      return sh && sh.roomId === room ? shutterSlug(sh) : '';
    }
    if (isVisible('led-popup') && typeof activeLedTarget !== 'undefined' && LED_ROOMS[activeLedTarget] === room) {
      return LED_SLUGS[activeLedTarget] || '';
    }
    return '';
  }

  function currentUrl() {
    const room = currentRoom() && ROOM_SLUGS[currentRoom()];
    if (!room) return `${BASE}/`;
    const popup = currentPopupSlug();
    return popup ? `${BASE}/${room}/${popup}/` : `${BASE}/${room}/`;
  }

  function parsePath() {
    let path = location.pathname;
    if (BASE && path.startsWith(BASE)) path = path.slice(BASE.length);
    const [roomSlug = '', popupSlug = ''] = path.replace(/^\/+|\/+$/g, '').toLowerCase().split('/');
    return { roomId: SLUG_ROOMS[roomSlug] || null, popupSlug };
  }

  let applyingUrl = false;
  let depth = 0; // verschachtelte Aufrufe (z. B. Menü öffnen schließt erst das andere) nur einmal eintragen
  function syncUrl() {
    if (applyingUrl || depth > 0) return;
    const want = currentUrl();
    if (location.pathname !== want) history.pushState(null, '', want);
    const room = (typeof roomLabels !== 'undefined' ? roomLabels : []).find(r => r.id === currentRoom());
    document.title = room ? `${room.text} – Zuhause` : 'Zuhause – 3D Grundriss';
  }

  function track(name) {
    const orig = window[name];
    if (typeof orig !== 'function' || orig.__tracked) return;
    const wrapped = function () {
      depth++;
      try {
        return orig.apply(this, arguments);
      } finally {
        depth--;
        syncUrl();
      }
    };
    wrapped.__tracked = true;
    window[name] = wrapped;
  }

  function openPopupFromSlug(roomId, slug) {
    if (!slug) return false;
    if (slug === NET_SLUG) {
      const tv = (window.screensRegistry || []).find(s => s.kind === 'tv' && s.roomId === roomId);
      if (tv) { window.openNetPopup(tv.id); return true; }
      return false;
    }
    const led = Object.keys(LED_SLUGS).find(k => LED_SLUGS[k] === slug && LED_ROOMS[k] === roomId);
    if (led && window.openSimonLedPopup) { window.openSimonLedPopup(led); return true; }
    const sh = (window.shuttersRegistry || []).find(s => s.roomId === roomId && shutterSlug(s) === slug);
    if (sh && window.openShutterPopup) { window.openShutterPopup(sh.id); return true; }
    return false;
  }

  function applyUrl() {
    const { roomId, popupSlug } = parsePath();
    applyingUrl = true;
    try {
      if (roomId && roomId !== currentRoom()) window.enterRoomView(roomId);
      else if (!roomId && currentRoom()) window.exitRoomView();
      if (!roomId || currentPopupSlug() !== popupSlug) {
        if (window.closeAllPopups) window.closeAllPopups();
        if (roomId) openPopupFromSlug(roomId, popupSlug);
      }
    } finally {
      applyingUrl = false;
    }
    // Unbekannte Adresse (oder unbekanntes Menü) -> passende gültige Adresse
    if (location.pathname !== currentUrl()) history.replaceState(null, '', currentUrl());
    syncUrl();
  }

  function installRouting() {
    // Alle Wege, die einen Raum oder ein Menü öffnen oder schließen, laufen über diese Funktionen.
    ['enterRoomView', 'exitRoomView', 'setApartmentFilter', 'toggleCameraPerspective',
     'openShutterPopup', 'closeShutterPopup', 'openSimonLedPopup', 'closeSimonLedPopup',
     'openNetPopup', 'closeNetPopup', 'closeAllPopups'].forEach(track);
    window.addEventListener('popstate', applyUrl);
    window.addEventListener('load', () => setTimeout(applyUrl, 60));
  }

  // ---------------------------------------------------------------- Online-Status + Internet-Route
  let netInfo = { profiles: [], default: '', default_label: '', screens: {} };
  let timer = null;

  async function syncScreens() {
    try {
      const r = await fetch('/karte/status', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const screens = (data && data.screens) || {};
      let changed = false;
      assignedScreens = new Set(Object.keys(screens));
      (window.screensRegistry || []).forEach(scr => {
        if (!(scr.id in screens)) return;
        const on = Boolean(screens[scr.id]);
        if (scr.isOn !== on) { scr.isOn = on; changed = true; }
      });
      if (changed && typeof updateAllScreensVisuals === 'function') updateAllScreensVisuals();
      if (data && data.internet) { netInfo = data.internet; renderNet(); }
    } catch (e) { /* offline – Zustand bleibt */ }
  }
  function start() { if (!timer) { syncScreens(); timer = setInterval(syncScreens, 15000); } }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
  window.addEventListener('load', () => { if (!document.hidden) start(); });

  // ---------------------------------------------------------------- Internet-Button an jedem Fernseher
  const GLOBE = '<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="2"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5s-1.2 5.9-3.6 8.5c-2.4-2.6-3.6-5.4-3.6-8.5s1.2-5.9 3.6-8.5z" stroke="currentColor" stroke-width="1.8"/></svg>';
  const SHIELD = '<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><path d="M12 3l7.5 3v5.6c0 4.4-3.1 8.2-7.5 9.4-4.4-1.2-7.5-5-7.5-9.4V6L12 3z" fill="currentColor" fill-opacity=".18" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  let openScreenId = null;
  let busy = false;
  let note = null; // { text, kind: 'error'|'ok' }

  const esc = t => String(t == null ? '' : t).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tvs = () => (window.screensRegistry || []).filter(s => s.kind === 'tv');

  function badgeHtml(info) {
    if (!info) return `${GLOBE}<div class="gh-tile-text"><span class="gh-tile-title">Internet</span><span class="gh-tile-sub">Kein Gerät</span></div>`;
    const icon = info.kind ? SHIELD : GLOBE;
    const sub = info.mac ? (info.label || 'normal') : 'ohne MAC';
    return `${icon}<div class="gh-tile-text"><span class="gh-tile-title">Internet</span><span class="gh-tile-sub">${esc(sub)}</span></div>`;
  }

  function initNetBadges() {
    const layer = document.getElementById('net-layer');
    if (!layer) return;
    layer.innerHTML = '';
    tvs().forEach(scr => {
      const btn = document.createElement('div');
      btn.className = 'screen-badge net-badge';
      btn.id = `net-badge-${scr.id}`;
      btn.title = `${scr.name}: Internet über normal, WARP oder VPN`;
      btn.style.display = 'none';
      const open = () => openNetPopup(scr.id);
      if (typeof makeSmartButton === 'function') makeSmartButton(btn, { onShortClick: open, onLongPress: open });
      else btn.addEventListener('click', open);
      layer.appendChild(btn);
    });
    renderNet();
  }

  function renderNet() {
    tvs().forEach(scr => {
      const btn = document.getElementById(`net-badge-${scr.id}`);
      if (!btn) return;
      const info = netInfo.screens[scr.id];
      btn.classList.toggle('vpn', Boolean(info && info.kind === 'vpn'));
      btn.classList.toggle('warp', Boolean(info && info.kind === 'warp'));
      btn.classList.toggle('busy', busy && openScreenId === scr.id);
      const html = badgeHtml(info);
      if (btn.dataset.html !== html) { btn.innerHTML = html; btn.dataset.html = html; }
    });
    if (openScreenId) renderPopup();
  }

  function option(slot, title, sub, cls, current, disabled) {
    const active = slot === current;
    return `<button class="net-option ${cls}${active ? ' active' : ''}" ${disabled ? 'disabled' : ''} data-slot="${esc(slot)}">
      <span>${esc(title)}${sub ? `<br><span class="net-opt-sub">${esc(sub)}</span>` : ''}</span>
      ${active ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" style="flex-shrink:0"><path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>' : ''}</button>`;
  }

  function renderPopup() {
    const body = document.getElementById('net-popup-body');
    const scr = (window.screensRegistry || []).find(s => s.id === openScreenId);
    if (!body || !scr) return;
    document.getElementById('net-popup-title').textContent = scr.shortLabel || 'Fernseher';
    const info = netInfo.screens[scr.id];
    if (!info) {
      body.innerHTML = `<div class="net-device"><span class="net-dot"></span><div><div class="net-dev-name">${esc(scr.name)}</div><div class="net-dev-sub">Kein Gerät zugeordnet</div></div></div>
        <div class="net-note">Ordne dem Fernseher im cloudy-Dashboard unter <b>Einstellungen → 3D-Karte → Geräte zuordnen</b> ein Gerät zu, dann kannst du hier festlegen, wie es ins Internet geht.</div>`;
      return;
    }
    const lockedNote = info.locked ? `<div class="net-note">🔒 Festgelegt durch die Gruppe „${esc(info.locked)}“ – ändern geht nur dort.</div>` : '';
    const disabled = busy || Boolean(info.locked) || !info.mac;
    let html = `<div class="net-device"><span class="net-dot ${scr.isOn ? 'on' : ''}"></span><div><div class="net-dev-name">${esc(info.device)}</div>
      <div class="net-dev-sub">${scr.isOn ? 'Online' : 'Offline'} · jetzt: ${esc(info.mac ? info.label : 'ohne MAC')}</div></div></div>`;
    const cur = info.choice || '';
    html += '<div class="net-section">Internet über</div><div class="net-options">';
    if (netInfo.default) html += option('', 'Standard', `wie alle Geräte (${netInfo.default_label})`, '', cur, disabled);
    html += option('direct', 'Normal', 'direkt über die FRITZ!Box', '', netInfo.default ? cur : (cur || 'direct'), disabled);
    const warps = netInfo.profiles.filter(p => p.kind === 'warp');
    const vpns = netInfo.profiles.filter(p => p.kind === 'vpn');
    warps.forEach(p => { html += option(p.slot, p.label, p.ready ? 'Cloudflare WARP' : 'noch nicht eingerichtet', 'warp', cur, disabled || !p.ready); });
    vpns.forEach(p => { html += option(p.slot, p.label, p.ready ? 'VPN' : 'noch nicht eingerichtet', 'vpn', cur, disabled || !p.ready); });
    html += '</div>' + lockedNote;
    if (!info.mac) html += '<div class="net-note">Das Gerät hat keine MAC-Adresse, darum lässt sich die Route nicht umstellen.</div>';
    if (busy) html += '<div class="net-note">Wird umgestellt …</div>';
    else if (note) html += `<div class="net-note ${note.kind}">${esc(note.text)}</div>`;
    body.innerHTML = html;
    body.querySelectorAll('.net-option:not(:disabled)').forEach(b => b.addEventListener('click', () => setNet(scr.id, b.dataset.slot)));
  }

  async function setNet(screenId, slot) {
    if (busy) return;
    busy = true; note = null; renderNet();
    try {
      const r = await fetch('/karte/internet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ screen: screenId, slot })
      });
      let data = null;
      try { data = await r.json(); } catch (e) { /* kein JSON */ }
      if (r.status === 401) note = { kind: 'error', text: 'Dafür brauchst du das cloudy-Passwort.' };
      else if (!r.ok || !data || !data.ok) note = { kind: 'error', text: (data && (data.error || data.detail)) || 'Umstellen hat nicht geklappt.' };
      else {
        netInfo = data.internet;
        const info = netInfo.screens[screenId];
        note = { kind: 'ok', text: `Umgestellt auf ${info ? info.label : 'neu'} – greift in wenigen Sekunden.` };
      }
    } catch (e) {
      note = { kind: 'error', text: 'cloudy ist gerade nicht erreichbar.' };
    } finally {
      busy = false; renderNet();
    }
  }

  window.openNetPopup = function (screenId) {
    if (window.closeShutterPopup) window.closeShutterPopup();
    if (window.closeSimonLedPopup) window.closeSimonLedPopup();
    openScreenId = screenId; note = null;
    const pop = document.getElementById('net-popup');
    const bd = document.getElementById('popup-backdrop');
    const isModal = (navigator.maxTouchPoints > 0) || window.innerWidth <= 840;
    pop.classList.toggle('modal-center', isModal);
    renderPopup();
    pop.classList.add('visible');
    if (bd) bd.classList.toggle('visible', isModal);
    syncScreens();
  };

  window.closeNetPopup = function () {
    openScreenId = null;
    const pop = document.getElementById('net-popup');
    if (pop) pop.classList.remove('visible');
    if (typeof checkBackdrop === 'function') checkBackdrop();
  };

  const origCloseAll = window.closeAllPopups;
  window.closeAllPopups = function () {
    window.closeNetPopup();
    if (typeof origCloseAll === 'function') origCloseAll.apply(this, arguments);
  };
  // Beim Raumwechsel / Verlassen das Menü schließen
  ['enterRoomView', 'exitRoomView', 'setApartmentFilter'].forEach(name => {
    const orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = function (roomId) {
      const scr = (window.screensRegistry || []).find(s => s.id === openScreenId);
      if (openScreenId && !(name === 'enterRoomView' && scr && scr.roomId === roomId)) window.closeNetPopup();
      return orig.apply(this, arguments);
    };
  });
  window.addEventListener('keydown', e => { if (e.key === 'Escape') window.closeNetPopup(); });

  // Buttons jedes Bild an den Fernseher hängen (nur im Raum des Fernsehers sichtbar, wie die anderen Buttons)
  function placeNetBadges() {
    requestAnimationFrame(placeNetBadges);
    if (typeof camera === 'undefined' || typeof groups === 'undefined') return;
    const halfW = window.innerWidth / 2;
    const halfH = window.innerHeight / 2;
    const room = typeof activeRoomId !== 'undefined' ? activeRoomId : null;
    tvs().forEach(scr => {
      const btn = document.getElementById(`net-badge-${scr.id}`);
      if (!btn) return;
      if (!room || room !== scr.roomId) {
        if (btn.style.display !== 'none') btn.style.display = 'none';
        return;
      }
      const p = scr.worldPos.clone();
      p.y *= groups[scr.apt].walls.scale.y;
      p.project(camera);
      if (p.z > 1) { btn.style.display = 'none'; return; } // Fernseher hinter der Kamera
      const padX = Math.min(85, window.innerWidth * 0.2);
      const x = Math.max(padX, Math.min(window.innerWidth - padX, p.x * halfW + halfW));
      const y = Math.max(82, Math.min(window.innerHeight - 95, -p.y * halfH + halfH));
      btn.style.display = 'inline-flex';
      btn.style.left = `${x}px`;
      btn.style.top = `${y}px`;
    });
  }

  window.addEventListener('load', () => { initNetBadges(); requestAnimationFrame(placeNetBadges); });

  // ---------------------------------------------------------------- Home Assistant
  // Geräte, die in cloudy (Einstellungen → 3D-Karte → Home Assistant) einer Entität zugeordnet sind, schalten echt:
  // Bedienen auf der Karte -> /karte/ha -> Home Assistant; der echte Zustand kommt alle paar Sekunden zurück.
  let ha = { configured: false, devices: {} };
  let haApplying = false;
  let haTimer = null;
  const haHold = {};   // Gerät -> bis wann Rückmeldungen ignoriert werden (gerade selbst geschaltet)
  const haDelay = {};  // Gerät -> Timer zum Bündeln (Schieberegler)
  const LED_DEV = { simon: 'led-simon', schlaf: 'led-schlaf' };
  const ledObj = t => (t === 'schlaf' ? window.schlafCeilingLed : window.simonDeskLed);
  const linked = dev => Boolean(ha.devices[dev]);
  let assignedScreens = new Set();

  function haToast(text) {
    let el = document.getElementById('ha-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ha-toast';
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add('visible');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('visible'), 4500);
  }

  function haSend(device, action, value, delay = 0) {
    if (haApplying || !linked(device)) return;
    haHold[device] = Date.now() + 10000;
    clearTimeout(haDelay[device]);
    haDelay[device] = setTimeout(async () => {
      try {
        const r = await fetch('/karte/ha', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ device, action, value })
        });
        if (r.ok) {
          haHold[device] = Date.now() + 4000;
        } else {
          let data = null;
          try { data = await r.json(); } catch (e) { /* kein JSON */ }
          haHold[device] = 0;
          haToast((data && (data.error || data.detail)) || 'Home Assistant hat nicht reagiert.');
        }
      } catch (e) {
        haHold[device] = 0;
        haToast('cloudy ist gerade nicht erreichbar.');
      }
      setTimeout(syncHa, 1200);
    }, delay);
  }

  function after(name, fn) {
    const orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = function () {
      const res = orig.apply(this, arguments);
      try { fn.apply(this, arguments); } catch (e) { /* Karte läuft weiter */ }
      return res;
    };
  }

  after('setActiveShutterPercent', (pct, shId) => {
    const id = shId || (typeof activeShutterId !== 'undefined' ? activeShutterId : null);
    const sh = (window.shuttersRegistry || []).find(s => s.id === id);
    if (sh) haSend(sh.id, 'position', sh.targetPercent, 450);
  });
  after('toggleSimonRoomLight', () => haSend('light-simon', window.simonRoomLight.isOn ? 'on' : 'off'));
  after('toggleSchlafRoomLight', () => haSend('light-schlaf', window.schlafRoomLight.isOn ? 'on' : 'off'));
  after('toggleSimonPc', () => haSend('pc-simon', window.simonPc.isOn ? 'on' : 'off'));
  after('toggleKuecheSocket', () => haSend('plug-kueche', window.kuecheSocket.isOn ? 'on' : 'off'));
  const sendLed = extra => {
    const t = typeof activeLedTarget !== 'undefined' ? activeLedTarget : 'simon';
    const led = ledObj(t);
    if (!led) return;
    haSend(LED_DEV[t], led.isOn ? 'on' : 'off', led.isOn ? extra(led) : null, 300);
  };
  after('toggleSpecificLed', () => sendLed(() => null));
  after('toggleSimonDeskLed', () => sendLed(() => null));
  after('setSimonDeskLedColor', () => sendLed(led => ({ color: led.colorHex })));
  after('setSimonDeskLedBrightness', () => sendLed(led => ({ brightness: led.brightness })));

  function applyHa() {
    const now = Date.now();
    const d = ha.devices || {};
    const fresh = id => d[id] && d[id].available && !(haHold[id] > now);
    haApplying = true;
    try {
      (window.shuttersRegistry || []).forEach(sh => {
        const st = d[sh.id];
        if (fresh(sh.id) && !st.moving && typeof st.percent === 'number' && sh.targetPercent !== st.percent) sh.targetPercent = st.percent;
      });
      let lights = false;
      [['light-simon', window.simonRoomLight], ['light-schlaf', window.schlafRoomLight]].forEach(([id, obj]) => {
        if (obj && fresh(id) && obj.isOn !== d[id].on) { obj.isOn = d[id].on; lights = true; }
      });
      Object.entries(LED_DEV).forEach(([t, id]) => {
        const led = ledObj(t);
        if (!led || !fresh(id)) return;
        const st = d[id];
        if (led.isOn !== st.on) { led.isOn = st.on; lights = true; }
        if (st.brightness && led.brightness !== st.brightness) { led.brightness = st.brightness; lights = true; }
        if (st.color && String(led.colorHex).toLowerCase() !== st.color) { led.colorHex = st.color; led.colorName = 'Eigene Farbe'; lights = true; }
      });
      if (lights && typeof updateSimonLightVisuals === 'function') updateSimonLightVisuals();
      const pc = window.simonPc;
      if (pc && fresh('pc-simon') && pc.isOn !== d['pc-simon'].on) {
        pc.isOn = d['pc-simon'].on;
        (window.screensRegistry || []).forEach(scr => {
          if ((scr.id === 'mon-simon-1' || scr.id === 'mon-simon-2') && !assignedScreens.has(scr.id)) scr.isOn = pc.isOn;
        });
        if (typeof updateSimonPcVisuals === 'function') updateSimonPcVisuals();
        if (typeof updateAllScreensVisuals === 'function') updateAllScreensVisuals();
      }
      const sock = window.kuecheSocket;
      if (sock && fresh('plug-kueche') && sock.isOn !== d['plug-kueche'].on) {
        sock.isOn = d['plug-kueche'].on;
        if (typeof updateKuecheSocketVisuals === 'function') updateKuecheSocketVisuals();
      }
    } finally {
      haApplying = false;
    }
  }

  async function syncHa() {
    try {
      const r = await fetch('/karte/ha', { cache: 'no-store' });
      if (!r.ok) return;
      ha = await r.json();
      if (!ha || !ha.devices) ha = { configured: false, devices: {} };
      applyHa();
      if (!Object.keys(ha.devices).length) haStop(); // nichts verknüpft -> nicht weiter fragen
    } catch (e) { /* offline – Zustand bleibt */ }
  }
  function haStart() { if (!haTimer) { syncHa(); haTimer = setInterval(syncHa, 8000); } }
  function haStop() { if (haTimer) { clearInterval(haTimer); haTimer = null; } }
  document.addEventListener('visibilitychange', () => (document.hidden ? haStop() : haStart()));
  window.addEventListener('load', () => { if (!document.hidden) haStart(); });

  installRouting();
})();
