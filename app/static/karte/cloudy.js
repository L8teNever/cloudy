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
    document.body.classList.toggle('in-room', Boolean(room));
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
  function tick() { syncScreens(); syncMein(); }
  function start() { if (!timer) { tick(); timer = setInterval(tick, 15000); } }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
  window.addEventListener('load', () => { if (!document.hidden) start(); });

  // ---------------------------------------------------------------- Internet-Button an jedem Fernseher
  const GLOBE = '<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="2"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5s-1.2 5.9-3.6 8.5c-2.4-2.6-3.6-5.4-3.6-8.5s1.2-5.9 3.6-8.5z" stroke="currentColor" stroke-width="1.8"/></svg>';
  const SHIELD = '<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><path d="M12 3l7.5 3v5.6c0 4.4-3.1 8.2-7.5 9.4-4.4-1.2-7.5-5-7.5-9.4V6L12 3z" fill="currentColor" fill-opacity=".18" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  let openScreenId = null;
  let meinOpen = false;   // Menü „Mein Gerät“ aus dem Header (nutzt dasselbe Popup wie die Fernseher)
  let mein = null;        // Antwort von /karte/mein
  let stackOpen = null;   // Buttons im offenen Sammel-Pop-up (Handy)
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
    if (stackOpen) return renderStackPopup();
    if (meinOpen) return renderMein();
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

  // ---------------------------------------------------------------- Eigenes Profil im Header
  function renderMeinBtn() {
    const btn = document.getElementById('mein-profil-btn');
    const label = document.getElementById('mein-profil-label');
    if (!btn || !label) return;
    label.textContent = mein && mein.known ? mein.label : 'Internet';
    btn.classList.toggle('vpn', Boolean(mein && mein.kind === 'vpn'));
    btn.classList.toggle('warp', Boolean(mein && mein.kind === 'warp'));
    btn.classList.toggle('dns', Boolean(mein && mein.kind === 'dns'));
    btn.classList.toggle('busy', busy && meinOpen);
  }

  async function syncMein() {
    try {
      const r = await fetch('/karte/mein', { cache: 'no-store' });
      if (!r.ok) return;
      mein = await r.json();
      renderMeinBtn();
      if (meinOpen && !busy) renderMein();
    } catch (e) { /* offline – Anzeige bleibt */ }
  }

  function renderMein() {
    const body = document.getElementById('net-popup-body');
    if (!body) return;
    document.getElementById('net-popup-title').textContent = 'Mein Internet';
    if (!mein) { body.innerHTML = '<div class="net-note">Wird geladen …</div>'; return; }
    if (!mein.known) {
      body.innerHTML = `<div class="net-device"><span class="net-dot"></span><div><div class="net-dev-name">Gerät nicht erkannt</div><div class="net-dev-sub">${esc(mein.ip || 'keine Adresse')}</div></div></div>
        <div class="net-note">Das eigene Profil lässt sich nur im Heimnetz wählen. Bist du im WLAN, warte kurz oder verbinde dich neu.</div>`;
      return;
    }
    const disabled = busy || Boolean(mein.locked);
    const cur = mein.choice || '';
    let html = `<div class="net-device"><span class="net-dot on"></span><div><div class="net-dev-name">${esc(mein.device)}</div>
      <div class="net-dev-sub">Dieses Gerät · jetzt: ${esc(mein.label)}</div></div></div>
      <div class="net-section">Internet über</div><div class="net-options">`;
    if (mein.show_standard) html += option('', 'Standard', `wie alle Geräte (${mein.default_label})`, '', cur, disabled);
    mein.options.forEach(p => {
      const cls = p.kind === 'vpn' || p.kind === 'warp' ? p.kind : '';
      const sub = p.kind === 'direct' ? 'direkt über die FRITZ!Box'
        : !p.ready ? 'noch nicht eingerichtet'
        : p.kind === 'vpn' ? 'VPN' : p.kind === 'warp' ? 'Cloudflare WARP' : 'DNS-Profil';
      html += option(p.slot, p.label, sub, cls, cur, disabled || !p.ready);
    });
    html += '</div>';
    if (mein.locked) html += `<div class="net-note">🔒 Festgelegt durch die Gruppe „${esc(mein.locked)}“ – ändern geht nur dort.</div>`;
    if (busy) html += '<div class="net-note">Wird umgestellt …</div>';
    else if (note) html += `<div class="net-note ${note.kind}">${esc(note.text)}</div>`;
    body.innerHTML = html;
    body.querySelectorAll('.net-option:not(:disabled)').forEach(b => b.addEventListener('click', () => setMein(b.dataset.slot)));
  }

  async function setMein(slot) {
    if (busy) return;
    busy = true; note = null; renderMeinBtn(); renderMein();
    try {
      const r = await fetch('/karte/mein', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ slot })
      });
      let data = null;
      try { data = await r.json(); } catch (e) { /* kein JSON */ }
      if (!r.ok || !data || !data.ok) note = { kind: 'error', text: (data && (data.error || data.detail)) || 'Umstellen hat nicht geklappt.' };
      else {
        mein = data.mein;
        note = { kind: 'ok', text: `Umgestellt auf ${mein.label} – greift in wenigen Sekunden.` };
      }
    } catch (e) {
      note = { kind: 'error', text: 'cloudy ist gerade nicht erreichbar.' };
    } finally {
      busy = false; renderMeinBtn(); if (meinOpen) renderMein();
    }
  }

  function showNetPopup() {
    const pop = document.getElementById('net-popup');
    const bd = document.getElementById('popup-backdrop');
    const isModal = (navigator.maxTouchPoints > 0) || window.innerWidth <= 840;
    pop.classList.toggle('modal-center', isModal);
    renderPopup();
    pop.classList.add('visible');
    if (bd) bd.classList.toggle('visible', isModal);
  }

  window.openMeinPopup = function () {
    if (window.toggleRoomHeaderDropdown) window.toggleRoomHeaderDropdown(null, false);
    if (window.closeShutterPopup) window.closeShutterPopup();
    if (window.closeSimonLedPopup) window.closeSimonLedPopup();
    openScreenId = null; meinOpen = true; stackOpen = null; note = null;
    showNetPopup();
    syncMein();
  };

  window.openNetPopup = function (screenId) {
    if (window.closeShutterPopup) window.closeShutterPopup();
    if (window.closeSimonLedPopup) window.closeSimonLedPopup();
    openScreenId = screenId; meinOpen = false; stackOpen = null; note = null;
    showNetPopup();
    syncScreens();
  };

  window.closeNetPopup = function () {
    openScreenId = null; meinOpen = false; stackOpen = null;
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
      if ((openScreenId || meinOpen || stackOpen) && !(name === 'enterRoomView' && scr && scr.roomId === roomId)) window.closeNetPopup();
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

  // ---------------------------------------------------------------- Gestapelte Buttons am Handy
  // Liegen Buttons im Raum zu dicht, zeigt das Handy einen Sammel-Button; das Pop-up schaltet sie einzeln.
  const STACK_W = 140, STACK_H = 48, STACK_TICK = 200;
  const STACK_REACH = { shutter: 28, power: 3 }; // erlaubter Abstand in px, damit gleiche Geräte zusammengehen
  const STACK_ICON = '<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><path d="M12 3.5l8.5 4.6-8.5 4.6-8.5-4.6L12 3.5z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M3.5 12.2l8.5 4.6 8.5-4.6M3.5 16.2l8.5 4.6 8.5-4.6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const stacks = new Map(); // Mitglieder-IDs -> Sammel-Button
  let stackPairs = new Set();
  let stackAt = 0;
  let stackSig = '';

  const badgeKind = el => el.classList.contains('shutter-badge') ? 'shutter' : el.classList.contains('net-badge') ? 'net' : 'power';
  const pairKey = (a, b) => [a.id, b.id].sort().join('|');
  const nodeGap = (a, b) => Math.max(Math.abs(a.x - b.x) - (a.w + b.w) / 2, Math.abs(a.y - b.y) - (a.h + b.h) / 2);
  const wasStacked = (a, b) => a.els.some(x => b.els.some(y => stackPairs.has(pairKey(x, y))));

  function stackNode(els) {
    const padX = STACK_W / 2 + 4;
    const x = els.reduce((s, e) => s + e._sx, 0) / els.length;
    const y = els.reduce((s, e) => s + e._sy, 0) / els.length;
    return { els, x: Math.max(padX, Math.min(window.innerWidth - padX, x)), y, w: STACK_W, h: STACK_H };
  }

  // Sammel-Button ein Stück weg vom Nachbarn schieben; geht das nicht, gehören beide zusammen.
  function shiftStack(a, b, nodes) {
    const needX = (a.w + b.w) / 2 + 4 - Math.abs(a.x - b.x);
    const needY = (a.h + b.h) / 2 + 4 - Math.abs(a.y - b.y);
    const alongX = needX <= needY;
    const need = alongX ? needX : needY;
    if (need > 34) return false;
    const dir = (alongX ? a.x - b.x : a.y - b.y) >= 0 ? 1 : -1;
    const moved = { ...a, x: alongX ? a.x + dir * need : a.x, y: alongX ? a.y : a.y + dir * need };
    const padX = a.w / 2 + 4;
    if (moved.x < padX || moved.x > window.innerWidth - padX || moved.y < 82 || moved.y > window.innerHeight - 40) return false;
    if (nodes.some(n => n !== a && n !== b && nodeGap(moved, n) < 3)) return false;
    a.x = moved.x; a.y = moved.y;
    return true;
  }

  function computeStacks(els) {
    els.forEach(el => {
      el._sx = parseFloat(el.style.left) || 0;
      el._sy = parseFloat(el.style.top) || 0;
      el._kind = badgeKind(el);
    });
    // 1) Gleichartiges, das nah beieinander liegt
    const parent = els.map((_, i) => i);
    const find = i => (parent[i] === i ? i : (parent[i] = find(parent[i])));
    const box = el => ({ x: el._sx, y: el._sy, w: el.offsetWidth, h: el.offsetHeight });
    for (let i = 0; i < els.length; i++) {
      for (let j = i + 1; j < els.length; j++) {
        const reach = STACK_REACH[els[i]._kind];
        if (reach === undefined || els[i]._kind !== els[j]._kind) continue;
        if (nodeGap(box(els[i]), box(els[j])) <= reach + (stackPairs.has(pairKey(els[i], els[j])) ? 12 : 0)) parent[find(i)] = find(j);
      }
    }
    const byRoot = new Map();
    els.forEach((el, i) => { const r = find(i); byRoot.set(r, [...(byRoot.get(r) || []), el]); });
    let nodes = [...byRoot.values()].map(g => (g.length > 1 ? stackNode(g) : { els: g, ...box(g[0]) }));
    // 2) Was sich dann noch überdeckt: Sammel-Button verschieben oder zusammenlegen
    for (let round = 0; round < 16; round++) {
      let hit = null;
      for (let i = 0; i < nodes.length && !hit; i++) {
        for (let j = i + 1; j < nodes.length && !hit; j++) {
          if (nodeGap(nodes[i], nodes[j]) < 3 + (wasStacked(nodes[i], nodes[j]) ? 12 : 0)) hit = [nodes[i], nodes[j]];
        }
      }
      if (!hit) break;
      const [a, b] = hit;
      const free = !wasStacked(a, b) && ((a.els.length > 1 && shiftStack(a, b, nodes)) || (b.els.length > 1 && shiftStack(b, a, nodes)));
      if (!free) nodes = [...nodes.filter(n => n !== a && n !== b), stackNode([...a.els, ...b.els])];
    }
    return nodes.filter(n => n.els.length > 1);
  }

  function clearStacks() {
    stacks.forEach(s => s.btn.remove());
    stacks.clear();
    stackPairs = new Set();
    document.querySelectorAll('[data-stacked]').forEach(el => el.removeAttribute('data-stacked'));
  }

  const memberText = (el, cls) => (el.querySelector(cls) || {}).textContent || '';

  function stackLabel(els) {
    const on = els.filter(el => el.classList.contains('on')).length;
    if (els.every(el => el._kind === 'shutter')) {
      const subs = [...new Set(els.map(el => memberText(el, '.gh-tile-sub')))];
      return { title: `${els.length} Rollläden`, sub: subs.length === 1 ? subs[0] : 'gemischt', on };
    }
    return { title: `${els.length} Geräte`, sub: `${on} an`, on };
  }

  function renderStackBtn(btn, els) {
    const { title, sub, on } = stackLabel(els);
    btn.classList.toggle('on', on > 0);
    const html = `${STACK_ICON}<div class="gh-tile-text"><span class="gh-tile-title">${esc(title)}</span><span class="gh-tile-sub">${esc(sub)}</span></div>`;
    if (btn.dataset.html !== html) { btn.innerHTML = html; btn.dataset.html = html; }
  }

  function applyStacks(nodes) {
    const keep = new Set();
    const hidden = new Set();
    const pairs = new Set();
    nodes.forEach(n => {
      n.els.sort((a, b) => a.id.localeCompare(b.id));
      const key = n.els.map(el => el.id).join('|');
      keep.add(key);
      let s = stacks.get(key);
      if (!s) {
        const btn = document.createElement('div');
        btn.className = 'screen-badge badge-stack';
        s = { btn, els: n.els };
        btn.addEventListener('click', e => { e.stopPropagation(); window.openStackPopup(s.els); });
        document.getElementById('shutters-layer').appendChild(btn);
        stacks.set(key, s);
      }
      s.els = n.els;
      s.btn.style.left = `${n.x}px`;
      s.btn.style.top = `${n.y}px`;
      renderStackBtn(s.btn, n.els);
      n.els.forEach((a, i) => { hidden.add(a); n.els.slice(i + 1).forEach(b => pairs.add(pairKey(a, b))); });
    });
    stacks.forEach((s, key) => { if (!keep.has(key)) { s.btn.remove(); stacks.delete(key); } });
    document.querySelectorAll('[data-stacked]').forEach(el => { if (!hidden.has(el)) el.removeAttribute('data-stacked'); });
    hidden.forEach(el => el.setAttribute('data-stacked', ''));
    stackPairs = pairs;
  }

  function updateStacks(now) {
    requestAnimationFrame(updateStacks);
    if (now - stackAt < STACK_TICK) return;
    stackAt = now;
    if (stackOpen) refreshStackPopup();
    const room = typeof activeRoomId !== 'undefined' ? activeRoomId : null;
    const moving = typeof camAnim !== 'undefined' && camAnim; // während der Kamerafahrt nichts umsortieren
    if (!room || moving || window.innerWidth > 840) {
      if (stacks.size || document.querySelector('[data-stacked]')) clearStacks();
      return;
    }
    const els = [...document.querySelectorAll('#shutters-layer > :not(.badge-stack), #net-layer > *')]
      .filter(el => el.style.display === 'inline-flex');
    applyStacks(computeStacks(els));
  }

  function stackRowHtml(el, i) {
    const kind = badgeKind(el);
    const iconEl = el.querySelector('.gh-tile-icon, .shutter-mini-win');
    let icon = '';
    if (iconEl) {
      const copy = iconEl.cloneNode(true);
      copy.removeAttribute('id');
      copy.querySelectorAll('[id]').forEach(n => n.removeAttribute('id')); // IDs bleiben beim Original auf der Karte
      icon = copy.outerHTML;
    }
    const sub = memberText(el, '.gh-tile-sub');
    const actions = el._actions || {};
    const on = kind === 'shutter' ? parseInt(sub, 10) <= 20 : el.classList.contains('on');
    const tune = kind !== 'net' && actions.onLongPress
      ? `<button class="gh-icon-btn stack-tune" data-tune="${i}" title="Einstellen" aria-label="Einstellen"><svg viewBox="0 0 24 24" fill="none"><path d="M4 7h9M18 7h2M4 17h2M11 17h9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="15.5" cy="7" r="2.5" stroke="currentColor" stroke-width="2.2"/><circle cx="8.5" cy="17" r="2.5" stroke="currentColor" stroke-width="2.2"/></svg></button>`
      : '';
    const end = kind === 'net'
      ? '<svg class="stack-chevron" viewBox="0 0 24 24" fill="none"><path d="M9.5 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
      : `<span class="stack-switch${on ? ' on' : ''}"><span></span></span>`;
    return `<div class="stack-row${on && kind !== 'net' ? ' on' : ''}">
      <button class="stack-main" data-run="${i}" ${kind === 'net' ? '' : `role="switch" aria-checked="${on}"`}>
        <span class="stack-icon">${icon}</span>
        <span class="stack-text"><span class="stack-title">${esc(memberText(el, '.gh-tile-title'))}</span><span class="stack-sub">${esc(sub)}</span></span>
        ${end}
      </button>${tune}</div>`;
  }

  function renderStackPopup() {
    const body = document.getElementById('net-popup-body');
    if (!body || !stackOpen) return;
    document.getElementById('net-popup-title').textContent = stackLabel(stackOpen).title;
    stackSig = stackOpen.map((el, i) => stackRowHtml(el, i)).join('');
    body.innerHTML = `<div class="stack-list">${stackSig}</div>`;
    body.querySelectorAll('[data-run]').forEach(b => b.addEventListener('click', () => {
      const el = stackOpen[Number(b.dataset.run)];
      if (el && el._actions && el._actions.onShortClick) el._actions.onShortClick();
      if (stackOpen) refreshStackPopup();
    }));
    body.querySelectorAll('[data-tune]').forEach(b => b.addEventListener('click', () => {
      const el = stackOpen[Number(b.dataset.tune)];
      window.closeNetPopup();
      if (el && el._actions && el._actions.onLongPress) el._actions.onLongPress(true);
    }));
  }

  // Nur neu zeichnen, wenn sich etwas geändert hat (sonst gehen Fingertipps verloren)
  function refreshStackPopup() {
    if (stackOpen.map((el, i) => stackRowHtml(el, i)).join('') !== stackSig) renderStackPopup();
  }

  window.openStackPopup = function (els) {
    if (window.closeShutterPopup) window.closeShutterPopup();
    if (window.closeSimonLedPopup) window.closeSimonLedPopup();
    openScreenId = null; meinOpen = false; note = null; stackOpen = [...els];
    showNetPopup();
  };

  window.addEventListener('load', () => requestAnimationFrame(updateStacks));

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
