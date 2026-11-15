/* LoRaQ web display viewer. Polls the server snapshot and renders it like a LoRaQ receiver. */
(function () {
  'use strict';

  var COLOURS = { W: '#ffffff', R: '#ff1e1e', G: '#1eff1e', Y: '#ffe600', B: '#00a0ff', O: '#ff8200' };
  var FLASH_MS = 2000;
  var FLASH_HZ = 3;
  var EFFECT_PLAY_WINDOW_MS = 8000;   // effects older than this when first seen are not replayed
  var REPORTER_STALE_MS = 120000;     // pushers heartbeat every ~30 s
  var STATION_LIST_MS = 3600000;      // devices not heard for this long are left out of the station list
  var OFFLINE_AFTER_MS = 15000;
  var POLL_VISIBLE_MS = 1000;
  var POLL_HIDDEN_MS = 5000;

  var body = document.body;
  var key = body.getAttribute('data-key');
  var api = body.getAttribute('data-api');
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    screen: $('screen'), status: $('status'), share: $('share'), sound: $('sound'),
    stations: $('stations'), stationsDetail: $('stations-detail'), stationsList: $('stations-list'),
    stationsLast: $('stations-last'),
    area1: $('area1'), a1text: $('a1-text'), a1timer: $('a1-timer'),
    area2: $('area2'), a2text: $('a2-text'), a2timer: $('a2-timer')
  };

  // ---------- Clock sync: server_ms ~= performance.now() + offset ----------
  var samples = [];
  var offset = null;

  function addClockSample(serverNow, t0, t1) {
    var rtt = t1 - t0;
    var off = serverNow + rtt / 2 - t1;
    // performance.now() pauses while the device sleeps, so drop samples taken before a jump.
    if (offset !== null && Math.abs(off - offset) > Math.max(1000, rtt)) samples = [];
    samples.push({ rtt: rtt, off: off });
    if (samples.length > 15) samples.shift();
    var best = samples[0];
    for (var i = 1; i < samples.length; i++) if (samples[i].rtt < best.rtt) best = samples[i];
    offset = best.off;
  }

  function serverNow() {
    return offset === null ? Date.now() : performance.now() + offset;
  }

  // ---------- Layout ----------
  var layout = { W: 0, H: 0, portrait: false, a1min: 12, a1max: 48 };
  var fitKey = '';

  function px(v) { return Math.round(v) + 'px'; }

  function applyLayout() {
    var W = el.screen.clientWidth || window.innerWidth;
    var H = el.screen.clientHeight || window.innerHeight;
    if (W === layout.W && H === layout.H) return;
    var portrait = H > W * 1.1;
    var compact = !portrait && H < 420;
    var bar = Math.min(56, Math.max(44, H * 0.065));
    var panel = H * (portrait ? 0.30 : 0.235);
    var a2 = portrait ? Math.min(H * 0.06, W * 0.085) : H * 0.078;
    var style = document.documentElement.style;
    style.setProperty('--W', px(W));
    style.setProperty('--H', px(H));
    style.setProperty('--bar', px(bar));
    style.setProperty('--panel', px(panel));
    style.setProperty('--a2', px(a2));
    style.setProperty('--a2label', px(Math.max(10, H * (portrait ? 0.024 : 0.035))));
    body.classList.toggle('portrait', portrait);
    body.classList.toggle('compact', compact);
    layout = { W: W, H: H, portrait: portrait,
               a1min: Math.max(12, H * 0.035), a1max: Math.max(14, portrait ? Math.min(H * 0.18, W * 0.2) : H * 0.18) };
    fitKey = '';
    fitArea1();
  }

  function a1Fits() {
    return el.a1text.scrollWidth <= el.a1text.clientWidth + 1 && el.area1.scrollHeight <= el.area1.clientHeight + 1;
  }

  // Largest font that fits, preferring whole-word wrapping, then allowing breaks inside words.
  function fitArea1() {
    var key = [layout.W, layout.H, el.a1text.textContent, !el.a1timer.hidden].join('|');
    if (key === fitKey) return;
    fitKey = key;
    var passes = [false, true];
    for (var p = 0; p < passes.length; p++) {
      el.a1text.classList.toggle('break-any', passes[p]);
      var lo = Math.floor(layout.a1min), hi = Math.floor(layout.a1max), best = 0;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        el.area1.style.fontSize = mid + 'px';
        if (a1Fits()) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      if (best) { el.area1.style.fontSize = best + 'px'; return; }
    }
    el.area1.style.fontSize = Math.floor(layout.a1min) + 'px';
  }

  // ---------- Rendering ----------
  var snap = null;
  var flash = null;   // {until: perf ms, start: perf ms, colour}
  var lastEffectId = null;
  var firstSnapshot = true;
  var visibleSince = document.hidden ? null : performance.now();

  function fmt(seconds) {
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function remaining(zone) {
    // Matches the receiver: duration - whole seconds elapsed, never below zero.
    return Math.max(0, Math.ceil((zone.ends_at - serverNow()) / 1000));
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function renderZones() {
    var z1 = snap && snap.zones && snap.zones['1'];
    var z2 = snap && snap.zones && snap.zones['2'];

    var t1 = z1 ? z1.text : '';
    el.area1.style.color = COLOURS[(z1 && z1.colour) || 'W'];
    setText(el.a1text, t1);
    var has1 = !!(z1 && z1.ends_at !== null && z1.ends_at !== undefined);
    if (el.a1timer.hidden === has1) el.a1timer.hidden = !has1;
    if (has1) setText(el.a1timer, fmt(remaining(z1)));
    fitArea1();

    var show2 = !!(z2 && z2.text);
    if (show2) {
      el.area2.style.color = COLOURS[z2.colour] || COLOURS.W;
      var t2 = z2.text.replace(/\s*\n\s*/g, ' ');
      setText(el.a2text, t2);
      var has2 = z2.ends_at !== null && z2.ends_at !== undefined;
      if (el.a2timer.hidden === has2) el.a2timer.hidden = !has2;
      if (has2) setText(el.a2timer, fmt(remaining(z2)));
    }
    el.area2.classList.toggle('shown', show2);
  }

  function renderFlash(now) {
    var on = false;
    if (flash) {
      if (now >= flash.until) {
        flash = null;
      } else {
        on = Math.floor((now - flash.start) / 1000 * FLASH_HZ * 2) % 2 === 0;
      }
    }
    if (on) body.style.setProperty('--flash', COLOURS[flash.colour] || COLOURS.W);
    body.classList.toggle('strobe', on);
  }

  function frame() {
    renderZones();
    renderFlash(performance.now());
    requestAnimationFrame(frame);
  }

  function handleEffect(effect) {
    if (!effect) {
      // Server clears the effect when Area 1 is cleared; stop any running flash.
      flash = null;
      lastEffectId = null;
      return;
    }
    if (effect.id === lastEffectId) return;
    lastEffectId = effect.id;
    if (firstSnapshot || serverNow() - effect.at > EFFECT_PLAY_WINDOW_MS) return;
    if (effect.flash) flash = { start: performance.now(), until: performance.now() + FLASH_MS, colour: effect.colour };
    if (effect.sound && !document.hidden && visibleSince !== null && offset !== null
            && effect.at >= visibleSince + offset) playSound(effect.sound);
  }

  // ---------- Status ----------
  var lastOk = 0;
  var failures = 0;

  var statusDetail = '';

  function setStatus(cls, label, detail) {
    el.status.className = 'pill ' + cls;
    setText(el.status, label);
    statusDetail = detail;
  }

  function ago(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + ' s ago';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    return Math.floor(s / 3600) + ' h ago';
  }

  function addRow(list, cls, nameText, infoText) {
    var li = document.createElement('li');
    li.className = cls;
    var name = document.createElement('span');
    name.className = 'stations-name';
    name.textContent = nameText;
    var info = document.createElement('span');
    info.className = 'stations-info';
    info.textContent = infoText;
    li.appendChild(name);
    li.appendChild(info);
    list.appendChild(li);
  }

  function renderStations() {
    if (el.stations.hidden) return;
    setText(el.stationsDetail, statusDetail);
    var now = serverNow();
    var reporters = (snap && snap.reporters ? snap.reporters : [])
      .filter(function (r) { return now - r.last_seen <= STATION_LIST_MS; })
      .sort(function (a, b) { return b.last_seen - a.last_seen; });
    var list = el.stationsList;
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!reporters.length) {
      addRow(list, 'stations-none', snap ? 'No devices heard in the last hour.' : 'Waiting for the server.', '');
    }
    reporters.forEach(function (r) {
      var age = now - r.last_seen;
      var live = age <= REPORTER_STALE_MS;
      // A queue count is only current while the device is still reporting.
      addRow(list, live ? 'live' : 'stale', r.name,
             ago(age) + (live && r.queue_depth > 0 ? ' \u00b7 ' + r.queue_depth + ' queued' : ''));
    });
    var src = snap && snap.source;
    setText(el.stationsLast, src ? 'Last cue from ' + src.station + ', ' + ago(now - src.at) + '.' : '');
    el.stationsLast.hidden = !src;
  }

  function showStations(open) {
    el.stations.hidden = !open;
    el.status.setAttribute('aria-expanded', open ? 'true' : 'false');
    renderStations();
  }

  el.status.addEventListener('click', function (e) {
    e.stopPropagation();
    showStations(el.stations.hidden);
  });
  el.stations.addEventListener('click', function (e) { e.stopPropagation(); });
  document.addEventListener('click', function () { if (!el.stations.hidden) showStations(false); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !el.stations.hidden) showStations(false);
  });

  if (typeof navigator.share === 'function') {
    el.share.hidden = false;
    el.share.addEventListener('click', function (e) {
      e.stopPropagation();
      navigator.share({ title: document.title, url: window.location.href }).catch(function (error) {
        if (error.name !== 'AbortError') console.error('Could not share the viewer link:', error);
      });
    });
  }

  function updateStatus() {
    var now = performance.now();
    if (failures > 0 && (!lastOk || now - lastOk > OFFLINE_AFTER_MS)) {
      setStatus('pill-off', 'OFFLINE', 'Cannot reach the server. Timers keep running from the last update.');
    } else if (failures > 0) {
      setStatus('pill-wait', 'RECONNECTING', 'Retrying the server.');
    } else if (!snap) {
      setStatus('pill-wait', 'CONNECTING', 'Contacting the server.');
    } else {
      var newest = 0;
      (snap.reporters || []).forEach(function (r) { newest = Math.max(newest, r.last_seen); });
      if (!newest || serverNow() - newest > REPORTER_STALE_MS) {
        setStatus('pill-stale', 'NO UPDATE', 'No transmitter or receiver has contacted the server recently.');
      } else {
        setStatus('pill-live', 'LIVE', 'Cues are reaching the server via:');
      }
    }
    renderStations();
  }

  // ---------- Polling ----------
  var pollTimer = null;
  var dead = false;

  function showMessage(text) {
    var m = document.createElement('div');
    m.className = 'message';
    m.textContent = text;
    el.screen.appendChild(m);
  }

  function schedulePoll(ms) {
    clearTimeout(pollTimer);
    if (!dead) pollTimer = setTimeout(poll, ms);
  }

  function pollInterval() {
    return document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS;
  }

  function poll() {
    var url = api + '?k=' + encodeURIComponent(key) + (snap ? '&v=' + snap.v : '');
    var controller = window.AbortController ? new AbortController() : null;
    var timeout = setTimeout(function () { if (controller) controller.abort(); }, 8000);
    var t0 = performance.now();
    fetch(url, { cache: 'no-store', credentials: 'omit', signal: controller ? controller.signal : undefined })
      .then(function (res) {
        if (res.status === 404) {
          dead = true;
          showMessage('This display link is not valid.');
          throw new Error('unknown show');
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        var t1 = performance.now();
        if (typeof data.now === 'number') addClockSample(data.now, t0, t1);
        if (!data.same) {
          snap = data;
          if (data.title && document.title !== data.title) document.title = data.title;
          handleEffect(data.effect);
        }
        // An unchanged reply means nothing stale to swallow, so live effects can play from now on.
        firstSnapshot = false;
        lastOk = t1;
        failures = 0;
        updateStatus();
        schedulePoll(pollInterval());
      })
      .catch(function () {
        if (dead) return;
        failures++;
        updateStatus();
        schedulePoll(Math.min(10000, 1000 * Math.pow(2, Math.min(failures - 1, 4))));
      })
      .then(function () { clearTimeout(timeout); });
  }

  // ---------- Sound (Web Audio, same tones as loraq_audio.py) ----------
  var audio = null;
  var soundOn = false;
  var PREF = 'loraq-sound';

  function getPref() {
    try { return localStorage.getItem(PREF); } catch (e) { return null; }
  }
  function setPref(v) {
    try { localStorage.setItem(PREF, v); } catch (e) { /* private mode */ }
  }

  function ensureAudio() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    if (!audio) audio = new Ctx();
    if (audio.state === 'suspended') audio.resume();
    return audio;
  }

  function tone(ctx, start, freq, dur, type, level) {
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    var ramp = 0.005;
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(level, start + ramp);
    gain.gain.setValueAtTime(level, start + dur - ramp);
    gain.gain.linearRampToValueAtTime(0, start + dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + dur + 0.02);
  }

  function playSound(name) {
    if (!soundOn || !audio || audio.state !== 'running') return;
    var t = audio.currentTime + 0.02;
    if (name === 'buzzer') tone(audio, t, 220, 0.6, 'square', 0.25);
    else if (name === 'beep') tone(audio, t, 1000, 0.2, 'sine', 0.6);
    else if (name === 'double-beep') { tone(audio, t, 1000, 0.15, 'sine', 0.6); tone(audio, t + 0.25, 1000, 0.15, 'sine', 0.6); }
  }

  function updateSoundUi() {
    el.sound.setAttribute('aria-pressed', soundOn ? 'true' : 'false');
    el.sound.title = soundOn ? 'Sound on' : 'Sound off';
  }

  function enableSound(on) {
    soundOn = on;
    setPref(on ? 'on' : 'off');
    if (on && ensureAudio()) {
      // A short silent tone unlocks audio on iOS.
      tone(audio, audio.currentTime, 1000, 0.02, 'sine', 0.0001);
    }
    updateSoundUi();
  }

  el.sound.addEventListener('click', function (e) { e.stopPropagation(); enableSound(!soundOn); });
  // Browsers only allow audio after a user gesture, so a remembered "on" resumes on the first tap.
  document.addEventListener('pointerdown', function () {
    if (soundOn && (!audio || audio.state !== 'running')) ensureAudio();
  });
  soundOn = getPref() === 'on';
  updateSoundUi();

  // ---------- Wake lock ----------
  var wakeLock = null;
  function requestWakeLock() {
    if (!('wakeLock' in navigator) || document.hidden || wakeLock) return;
    navigator.wakeLock.request('screen').then(function (lock) {
      wakeLock = lock;
      lock.addEventListener('release', function () { wakeLock = null; });
    }).catch(function () {});
  }

  document.addEventListener('pointerdown', requestWakeLock);

  // On returning to the page, resync the clock and treat the next snapshot like a fresh load so any
  // effect sent while away is marked seen without sounding or flashing.
  function onResume() {
    visibleSince = document.hidden ? null : performance.now();
    if (document.hidden) return;
    samples = [];
    firstSnapshot = true;
    requestWakeLock();
    schedulePoll(0);
  }
  document.addEventListener('visibilitychange', onResume);
  window.addEventListener('pageshow', onResume);
  document.addEventListener('resume', onResume);

  // ---------- Start ----------
  if (window.ResizeObserver) new ResizeObserver(applyLayout).observe(el.screen);
  window.addEventListener('resize', applyLayout);
  window.addEventListener('orientationchange', function () { setTimeout(applyLayout, 200); });
  applyLayout();
  requestWakeLock();
  setInterval(updateStatus, 5000);
  requestAnimationFrame(frame);
  poll();
})();
