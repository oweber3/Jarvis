// Jarvis phone app: the orb, the shared conversation, desktop confirmations and quick actions.
// Talks only to the PC that served it. See src/jarvis/remote/remote.spec.md.
"use strict";

(() => {
  const TOKEN_KEY = "jarvis.phone.token";
  const SPEAK_KEY = "jarvis.phone.speak";
  const POLL_ABORT_MS = 40000;
  const BACKOFF_START_MS = 1000;
  const BACKOFF_MAX_MS = 15000;

  // -- storage (may be unavailable in private browsing) -------------------------------

  const storage = {
    get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* not remembered */ } },
    remove(key) { try { window.localStorage.removeItem(key); } catch (e) { /* nothing to forget */ } },
  };

  const $ = (id) => document.getElementById(id);

  // -- orb (a port of desktop_app/orb_widget.py) ---------------------------------------

  const css = getComputedStyle(document.documentElement);
  const palette = (name) => css.getPropertyValue(`--orb-${name}`).trim();
  const hexToRgb = (hex) => {
    const n = parseInt(hex.replace("#", ""), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const rgba = (rgb, a) => `rgba(${rgb[0] | 0}, ${rgb[1] | 0}, ${rgb[2] | 0}, ${Math.min(1, Math.max(0, a))})`;
  const blend = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const approach = (value, target, rate, dt) => value + (target - value) * (1 - Math.exp(-rate * dt));
  const TAU = Math.PI * 2;

  // glow, ring speed, scan speed, scan strength, bar gain, ripples, colour, accent, label
  const look = (glow, ring, scan, scanStrength, barGain, ripples, colour, accent, label) =>
    ({ glow, ring, scan, scanStrength, barGain, ripples, colour: hexToRgb(palette(colour)), accent: hexToRgb(palette(accent)), label });
  const LOOKS = {
    offline: look(0.08, 2, 0, 0.0, 0.0, false, "slate-dark", "slate", "offline"),
    idle: look(0.28, 8, 14, 0.10, 0.0, false, "cyan", "sky", "system online"),
    listening: look(0.70, 16, 20, 0.15, 1.0, true, "cyan", "blue-light", "listening"),
    thinking: look(0.55, 30, 220, 1.0, 0.0, false, "indigo", "sky", "thinking"),
    speaking: look(0.92, 20, 26, 0.20, 1.0, false, "cyan-light", "blue", "speaking"),
    dictating: look(0.75, 14, 18, 0.15, 1.0, true, "green-light", "green", "dictating"),
  };
  // Jarvis's assistant states (jarvis/assistant_state.py) to orb looks.
  const STATE_TO_ORB = {
    asleep: "offline",
    idle: "idle",
    listening: "listening",
    thinking: "thinking",
    speaking: "speaking",
    dictating: "dictating",
    dictation_processing: "thinking",
  };
  const NUM_BARS = 56;
  const RIPPLE_PERIOD_S = 1.6;
  const RIPPLE_LIFE_S = 2.2;
  const FRAME_MS = 32;

  class Orb {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "offline";
      this.glow = 0; this.ringAngle = 0; this.scanAngle = 0; this.scanStrength = 0; this.pulse = 0;
      this.bars = new Array(NUM_BARS).fill(0);
      this.ripples = []; this.time = 0; this.rippleClock = 0;
      this.ringSpeed = 0; this.scanSpeed = 0; this.speechLevel = 0;
      this.colour = LOOKS.offline.colour.slice(); this.accent = LOOKS.offline.accent.slice();
      this.motion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0.3 : 1;
      this.last = 0; this.frame = null;
      this.tick = this.tick.bind(this);
    }

    setState(state) { this.state = LOOKS[state] ? state : "offline"; }

    start() {
      if (this.frame === null) { this.last = performance.now(); this.frame = requestAnimationFrame(this.tick); }
    }

    stop() {
      if (this.frame !== null) { cancelAnimationFrame(this.frame); this.frame = null; }
    }

    tick(now) {
      if (now - this.last < FRAME_MS) {  // about 30 frames a second is plenty, and kinder to the battery
        this.frame = requestAnimationFrame(this.tick);
        return;
      }
      const dt = Math.min(Math.max((now - this.last) / 1000, 0), 0.1);
      this.last = now;
      this.step(dt * this.motion);
      this.draw();
      this.frame = requestAnimationFrame(this.tick);
    }

    step(dt) {
      const l = LOOKS[this.state];
      this.time += dt;
      this.glow = Math.min(1, Math.max(0, approach(this.glow, l.glow, 4, dt)));
      this.ringSpeed = approach(this.ringSpeed, l.ring, 3, dt);
      this.scanSpeed = approach(this.scanSpeed, l.scan, 3, dt);
      this.ringAngle = (this.ringAngle + this.ringSpeed * dt) % 360;
      this.scanAngle += this.scanSpeed * dt;
      this.scanStrength = approach(this.scanStrength, l.scanStrength, 5, dt);

      // The phone has no audio level: speaking uses the desktop orb's synthetic speech envelope.
      let level = 0;
      if (this.state === "speaking") {
        const syllables = 0.5 + 0.5 * Math.sin(this.time * 9) * Math.sin(this.time * 2.3 + 1);
        level = 0.25 + 0.55 * Math.max(0, syllables);
      } else if (this.state === "listening" || this.state === "dictating") {
        level = 0.18 + 0.12 * Math.sin(this.time * 2.1);
      }
      level = Math.min(1, Math.max(0, level)) * l.barGain;
      this.speechLevel = approach(this.speechLevel, level, 14, dt);
      this.pulse = approach(this.pulse, this.speechLevel, 10, dt);
      for (let i = 0; i < NUM_BARS; i++) {
        const phase = (i / NUM_BARS) * TAU;
        const shape = 0.55 + 0.25 * Math.sin(phase * 3 + this.time * 5) + 0.2 * Math.sin(phase * 7 - this.time * 8);
        const target = Math.min(1, Math.max(0, this.speechLevel * shape));
        const rate = target > this.bars[i] ? 22 : 7;
        this.bars[i] = Math.min(1, Math.max(0, approach(this.bars[i], target, rate, dt)));
      }
      this.ripples = this.ripples.map((age) => age + dt).filter((age) => age < RIPPLE_LIFE_S);
      if (l.ripples) {
        if (!this.ripples.length && this.rippleClock === 0) this.ripples.push(0);
        this.rippleClock += dt;
        if (this.rippleClock >= RIPPLE_PERIOD_S) { this.rippleClock -= RIPPLE_PERIOD_S; this.ripples.push(0); }
      } else {
        this.rippleClock = 0;
      }
      const t = 1 - Math.exp(-6 * Math.min(dt, 0.1));
      this.colour = blend(this.colour, l.colour, t);
      this.accent = blend(this.accent, l.accent, t);
    }

    resize() {
      const ratio = window.devicePixelRatio || 1;
      const w = Math.round(this.canvas.clientWidth * ratio);
      const h = Math.round(this.canvas.clientHeight * ratio);
      if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
      return ratio;
    }

    draw() {
      const ratio = this.resize();
      const ctx = this.ctx;
      const w = this.canvas.width / ratio;
      const h = this.canvas.height / ratio;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (w < 8 || h < 8) return;
      const cx = w / 2, cy = h / 2, r = Math.max(10, Math.min(w, h) / 2 * 0.82);
      const c = this.colour, a = this.accent, glow = this.glow;

      // Backdrop glow.
      let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 1.5);
      g.addColorStop(0, rgba(c, 0.10 + 0.14 * glow));
      g.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, r * 1.5, 0, TAU); ctx.fill();

      // Listening ripples.
      ctx.lineWidth = 1.4;
      for (const age of this.ripples) {
        const t = age / RIPPLE_LIFE_S;
        ctx.strokeStyle = rgba(c, 0.45 * (1 - t));
        ctx.beginPath(); ctx.arc(cx, cy, r * (0.42 + 0.58 * t), 0, TAU); ctx.stroke();
      }

      // Outer tick ring.
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate((this.ringAngle * Math.PI) / 180);
      for (let i = 0; i < 72; i++) {
        const ang = (i / 72) * TAU;
        const long = i % 6 === 0;
        ctx.strokeStyle = rgba(a, (long ? 0.55 : 0.25) * (0.35 + 0.65 * glow));
        ctx.lineWidth = long ? 1.4 : 1;
        const ri = long ? r * 0.955 : r * 0.975;
        ctx.beginPath();
        ctx.moveTo(Math.cos(ang) * ri, Math.sin(ang) * ri);
        ctx.lineTo(Math.cos(ang) * r, Math.sin(ang) * r);
        ctx.stroke();
      }
      ctx.restore();

      // Guide rings and the counter-rotating dashed ring.
      ctx.strokeStyle = rgba(c, 0.18 + 0.2 * glow);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.88, 0, TAU); ctx.stroke();
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.5, 0, TAU); ctx.stroke();
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate((-this.ringAngle * 1.6 * Math.PI) / 180);
      ctx.setLineDash([2.4, 6]);
      ctx.strokeStyle = rgba(a, 0.25 + 0.3 * glow);
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(0, 0, r * 0.69, 0, TAU); ctx.stroke();
      ctx.restore();
      ctx.setLineDash([]);

      // Radial bars.
      const base = r * 0.54, span = r * 0.3;
      ctx.lineCap = "round";
      ctx.lineWidth = Math.max(1.2, r * 0.012);
      for (let i = 0; i < NUM_BARS; i++) {
        const level = this.bars[i];
        const ang = (i / NUM_BARS) * TAU - Math.PI / 2;
        const length = r * 0.02 + span * level;
        ctx.strokeStyle = rgba(c, (0.22 + 0.7 * level) * (0.4 + 0.6 * glow));
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * base, cy + Math.sin(ang) * base);
        ctx.lineTo(cx + Math.cos(ang) * (base + length), cy + Math.sin(ang) * (base + length));
        ctx.stroke();
      }

      // Scanning arcs (thinking).
      const s = this.scanStrength;
      if (s >= 0.02) {
        const arcs = [[70, 3, 0.9], [28, 2, 0.6], [14, 1.5, 0.4]];
        arcs.forEach(([spanDeg, width, alpha], k) => {
          const start = -this.scanAngle * (1 + 0.5 * k) + k * 120;
          ctx.strokeStyle = rgba(a, alpha * s);
          ctx.lineWidth = width;
          ctx.beginPath();
          ctx.arc(cx, cy, r * 0.78, (-start * Math.PI) / 180, (-(start + spanDeg) * Math.PI) / 180, true);
          ctx.stroke();
        });
      }

      // Core.
      const coreR = r * (0.3 + 0.05 * this.pulse + 0.012 * Math.sin(this.time * 1.6));
      g = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR * 2.4);
      g.addColorStop(0, rgba(c, 0.55 * glow));
      g.addColorStop(0.45, rgba(c, 0.18 * glow));
      g.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, coreR * 2.4, 0, TAU); ctx.fill();
      const fx = cx - coreR * 0.25, fy = cy - coreR * 0.3;
      g = ctx.createRadialGradient(fx, fy, 0, fx, fy, coreR * 1.2);
      g.addColorStop(0, rgba(blend(c, [255, 255, 255], 0.55), 0.25 + 0.7 * glow));
      g.addColorStop(0.6, rgba(c, 0.15 + 0.5 * glow));
      g.addColorStop(1, rgba(c, 0.05 + 0.2 * glow));
      ctx.fillStyle = g;
      ctx.strokeStyle = rgba(a, 0.35 + 0.5 * glow);
      ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, TAU); ctx.fill(); ctx.stroke();
    }
  }

  // -- app state ---------------------------------------------------------------------

  const app = {
    token: storage.get(TOKEN_KEY),
    rev: -1,
    after: 0,
    connected: null,
    speak: storage.get(SPEAK_KEY) === "1",
    myQueries: new Map(),    // query id -> echo element
    spokenQueries: new Set(),
    seenEntries: new Set(),
    quickActions: [],
    allowConfirm: true,
    confirmation: null,
    confirmDeadline: 0,
    wakePoll: null,
    pollAbort: null,
    repoll: false,
  };

  const orb = new Orb($("orb"));

  class Unpaired extends Error {}

  async function api(method, path, body, signal) {
    const headers = {};
    if (app.token) headers.Authorization = `Bearer ${app.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method, headers, signal, cache: "no-store", credentials: "omit",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (response.status === 401 && path !== "/api/pair") {
      forgetPairing("This phone is no longer paired with Jarvis. Pair it again.");
      throw new Unpaired();
    }
    return { status: response.status, data };
  }

  // -- views ----------------------------------------------------------------------------

  function showPairing(message) {
    $("pair").hidden = false;
    $("chat").hidden = true;
    $("menu-button").hidden = true;
    $("mode-badge").hidden = true;
    $("pc-stats").hidden = true;
    $("pair-message").textContent = message || "";
    if (!$("pair-name").value) $("pair-name").value = guessDeviceName();
    orb.setState("idle");
    setOrbLabel("not paired", "idle");
  }

  function showChat() {
    $("pair").hidden = true;
    $("chat").hidden = false;
    $("menu-button").hidden = false;
  }

  function guessDeviceName() {
    const ua = navigator.userAgent || "";
    if (/iPhone/.test(ua)) return "iPhone";
    if (/iPad/.test(ua)) return "iPad";
    if (/Android/.test(ua)) return "Android phone";
    return "Phone";
  }

  function setOrbLabel(text, look) {
    const label = $("orb-label");
    label.textContent = `● ${text}`.toUpperCase();
    label.className = `orb-label ${look || ""}`;
  }

  function setConnected(connected) {
    if (app.connected === connected) return;
    app.connected = connected;
    $("link-dot").className = `brand-dot ${connected ? "online" : "offline"}`;
    if (!connected) {
      orb.setState("offline");
      setOrbLabel("can't reach the PC", "offline");
      $("pc-stats").hidden = true;
    }
  }

  const timeFormat = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" });
  const formatTime = (seconds) => timeFormat.format(new Date(seconds * 1000));

  function nearBottom() {
    const t = $("transcript");
    return t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  }

  function scrollToEnd() {
    const t = $("transcript");
    requestAnimationFrame(() => { t.scrollTop = t.scrollHeight; });
  }

  function messageElement(role, text, seconds, pending) {
    if (role === "notice") {
      const el = document.createElement("p");
      el.className = "notice";
      el.textContent = text;
      return el;
    }
    const wrap = document.createElement("div");
    wrap.className = `msg ${role === "user" ? "user" : "assistant"}${pending ? " pending" : ""}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = pending ? "Sending…" : formatTime(seconds);
    wrap.append(bubble, meta);
    return wrap;
  }

  function addEntries(entries) {
    if (!entries.length) return;
    const stick = nearBottom();
    const list = $("entries");
    for (const entry of entries) {
      if (app.seenEntries.has(entry.id)) continue;
      app.seenEntries.add(entry.id);
      list.append(messageElement(entry.role, entry.text, entry.ts, false));
      app.after = Math.max(app.after, entry.id);
    }
    $("empty").hidden = list.children.length > 0;
    if (stick) scrollToEnd();
  }

  function addEcho(text) {
    const el = messageElement("user", text, Date.now() / 1000, true);
    $("echoes").append(el);
    $("empty").hidden = true;
    scrollToEnd();
    return el;
  }

  function showTyping(show) {
    let el = $("typing");
    if (show && !el) {
      el = document.createElement("div");
      el.id = "typing";
      el.className = "msg assistant";
      const bubble = document.createElement("div");
      bubble.className = "bubble typing";
      bubble.setAttribute("aria-label", "Jarvis is thinking");
      bubble.append(document.createElement("span"), document.createElement("span"), document.createElement("span"));
      el.append(bubble);
      $("echoes").append(el);
      scrollToEnd();
    } else if (!show && el) {
      el.remove();
    }
  }

  const MODE_NAMES = { local: "", claude: "☁ Claude", codex: "☁ ChatGPT (Codex)" };

  function renderMode(mode) {
    const badge = $("mode-badge");
    const name = mode && Object.prototype.hasOwnProperty.call(MODE_NAMES, mode.mode) ? MODE_NAMES[mode.mode] : "";
    badge.textContent = name;
    badge.hidden = !name;
  }

  function renderStats(pc) {
    const el = $("pc-stats");
    if (!pc) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent = `PC · CPU ${Math.round(pc.cpu)}% · Memory ${Math.round(pc.ram)}%`;
  }

  function renderQuickActions(actions) {
    const key = JSON.stringify(actions || []);
    if (key === JSON.stringify(app.quickActions)) return;
    app.quickActions = actions || [];
    const row = $("quick-actions");
    row.replaceChildren();
    for (const action of app.quickActions) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = action;
      chip.addEventListener("click", () => send(action));
      row.append(chip);
    }
    row.hidden = app.quickActions.length === 0;
  }

  function renderConfirmation(confirmation) {
    const card = $("confirm-card");
    if (!confirmation || !app.allowConfirm) {
      app.confirmation = null;
      card.hidden = true;
      $("app").classList.remove("confirming");
      return;
    }
    $("app").classList.add("confirming");
    if (!app.confirmation || app.confirmation.id !== confirmation.id) {
      app.confirmDeadline = Date.now() + confirmation.expires_in * 1000;
      if (navigator.vibrate) navigator.vibrate(120);
    }
    app.confirmation = confirmation;
    $("confirm-what").textContent = [confirmation.action, confirmation.target].filter(Boolean).join(" ");
    $("confirm-consequence").textContent = confirmation.consequence || "";
    $("confirm-tool").textContent = `Tool: ${confirmation.tool}`;
    $("confirm-approve").disabled = false;
    $("confirm-deny").disabled = false;
    card.hidden = false;
    tickConfirmTimer();
  }

  function tickConfirmTimer() {
    if (!app.confirmation) return;
    const left = Math.max(0, Math.round((app.confirmDeadline - Date.now()) / 1000));
    $("confirm-timer").textContent = left > 0 ? `${left}s left` : "expiring";
  }

  function speakReply(text) {
    if (!app.speak || !text || !("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    } catch (e) { /* the phone could not speak */ }
  }

  function updateComposer() {
    const pending = app.myQueries.size > 0;
    $("send").hidden = pending;
    $("stop").hidden = !pending;
    showTyping(pending);
    for (const chip of $("quick-actions").children) chip.disabled = pending;
  }

  function applySnapshot(data) {
    app.rev = data.rev;
    app.allowConfirm = data.allow_confirm !== false;
    addEntries(data.entries || []);
    const queries = data.queries || {};
    for (const [id, echo] of Array.from(app.myQueries.entries())) {
      const query = queries[String(id)];
      if (query && query.status === "pending") continue;
      echo.remove();
      app.myQueries.delete(id);
      if (query && query.status === "done" && !app.spokenQueries.has(id)) {
        app.spokenQueries.add(id);
        speakReply(query.reply);
      }
    }
    updateComposer();
    const look = STATE_TO_ORB[data.state] || "offline";
    orb.setState(look);
    setOrbLabel(LOOKS[look].label, look);
    renderMode(data.mode);
    renderStats(data.pc);
    renderQuickActions(data.quick_actions);
    renderConfirmation(data.confirmation);
  }

  // -- polling ------------------------------------------------------------------------------

  const sleep = (ms) => new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    app.wakePoll = () => { clearTimeout(timer); resolve(); };
  });

  function wakeNow() {
    if (app.wakePoll) { const wake = app.wakePoll; app.wakePoll = null; wake(); }
  }

  function repollNow() {
    // Answers land in the conversation at once: restart the long poll instead of waiting it out.
    if (app.pollAbort) { app.repoll = true; app.pollAbort.abort(); }
    wakeNow();
  }

  async function pollLoop() {
    let backoff = BACKOFF_START_MS;
    while (app.token) {
      if (document.hidden) { await sleep(60000); continue; }
      const controller = new AbortController();
      app.pollAbort = controller;
      const timer = setTimeout(() => controller.abort(), POLL_ABORT_MS);
      try {
        const { status, data } = await api("GET", `/api/poll?after=${app.after}&rev=${app.rev}`, undefined, controller.signal);
        if (status !== 200 || !data) throw new Error(`poll ${status}`);
        setConnected(true);
        backoff = BACKOFF_START_MS;
        applySnapshot(data);
      } catch (e) {
        if (e instanceof Unpaired) return;
        if (app.repoll) { app.repoll = false; continue; }
        setConnected(false);
        await sleep(backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      } finally {
        clearTimeout(timer);
        if (app.pollAbort === controller) app.pollAbort = null;
      }
    }
  }

  // -- actions ---------------------------------------------------------------------------------

  function unlockSpeech() {
    // iOS speaks only after a tap; an empty utterance during the tap unlocks later replies.
    if (app.speak && "speechSynthesis" in window) {
      try { window.speechSynthesis.speak(new SpeechSynthesisUtterance("")); } catch (e) { /* ignore */ }
    }
  }

  async function send(raw) {
    const text = (raw || "").trim();
    if (!text || app.myQueries.size > 0) return;
    unlockSpeech();
    const echo = addEcho(text);
    try {
      const { status, data } = await api("POST", "/api/chat", { text });
      if (status === 202 && data) {
        app.myQueries.set(data.query_id, echo);
        updateComposer();
      } else {
        echo.remove();
        if (status === 400) addLocalNotice("That message can't be sent.");
        else if (status !== 409 && status !== 503) addLocalNotice("Jarvis couldn't take that request.");
        if (raw !== undefined && $("input").value === "") restoreInput(text);
      }
    } catch (e) {
      echo.remove();
      if (e instanceof Unpaired) return;
      addLocalNotice("Couldn't reach Jarvis. Check the connection to your PC.");
      restoreInput(text);
    }
    repollNow();
  }

  function restoreInput(text) {
    if (app.quickActions.includes(text)) return;
    $("input").value = text;
    autosize();
  }

  function addLocalNotice(text) {
    // In the main list, so it keeps its place when later messages arrive.
    $("entries").append(messageElement("notice", text, Date.now() / 1000, false));
    $("empty").hidden = true;
    scrollToEnd();
  }

  async function answerConfirmation(approve) {
    const confirmation = app.confirmation;
    if (!confirmation) return;
    $("confirm-approve").disabled = true;
    $("confirm-deny").disabled = true;
    try {
      const { status } = await api("POST", "/api/confirm", { id: confirmation.id, approve });
      if (status === 409) addLocalNotice("That request was already answered or has expired.");
      else if (status !== 200) addLocalNotice("Jarvis couldn't take that answer.");
      else addLocalNotice(approve ? "Approved." : "Denied.");
    } catch (e) {
      if (!(e instanceof Unpaired)) addLocalNotice("Couldn't reach Jarvis. Answer on the PC instead.");
    }
    app.confirmation = null;
    $("confirm-card").hidden = true;
    $("app").classList.remove("confirming");
    repollNow();
  }

  function forgetPairing(message) {
    app.token = null;
    storage.remove(TOKEN_KEY);
    app.rev = -1; app.after = 0;
    app.myQueries.clear();
    app.seenEntries.clear();
    $("entries").replaceChildren();
    $("echoes").replaceChildren();
    $("empty").hidden = false;
    closeSheet();
    showPairing(message);
  }

  async function pair(event) {
    event.preventDefault();
    const code = $("pair-code").value.replace(/\D/g, "");
    const name = $("pair-name").value.trim() || guessDeviceName();
    if (code.length !== 6) { $("pair-message").textContent = "Enter the six-digit code from your PC."; return; }
    $("pair-submit").disabled = true;
    $("pair-message").textContent = "";
    try {
      const { status, data } = await api("POST", "/api/pair", { code, name });
      if (status === 200 && data && data.token) {
        app.token = data.token;
        storage.set(TOKEN_KEY, data.token);
        $("pair-code").value = "";
        startPaired();
      } else if (status === 403) {
        $("pair-message").textContent = "That code didn't work. Check it, or press Pair a phone on the PC for a new one.";
      } else {
        $("pair-message").textContent = "Jarvis couldn't pair right now. Try again.";
      }
    } catch (e) {
      $("pair-message").textContent = "Couldn't reach Jarvis. Is this phone on the same network as your PC?";
    } finally {
      $("pair-submit").disabled = false;
    }
  }

  async function unpair() {
    if (!window.confirm("Unpair this phone from Jarvis?")) return;
    try { await api("POST", "/api/unpair"); } catch (e) { /* forgotten on the phone either way */ }
    forgetPairing("This phone is unpaired. Pair it again any time.");
  }

  // -- menu sheet --------------------------------------------------------------------------------

  function openSheet() {
    $("sheet").hidden = false;
    $("sheet-backdrop").hidden = false;
    $("menu-button").setAttribute("aria-expanded", "true");
    $("speak-toggle").focus();
  }

  function closeSheet() {
    $("sheet").hidden = true;
    $("sheet-backdrop").hidden = true;
    $("menu-button").setAttribute("aria-expanded", "false");
  }

  // -- composer ---------------------------------------------------------------------------------

  function autosize() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  }

  function startPaired() {
    showChat();
    app.connected = null;
    setOrbLabel("connecting", "offline");
    pollLoop();
  }

  function wire() {
    $("pair-form").addEventListener("submit", pair);
    $("pair-code").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/\D/g, "").slice(0, 6); });

    $("composer").addEventListener("submit", (e) => {
      e.preventDefault();
      if (app.myQueries.size > 0) return;  // one request at a time; keep what was typed
      const input = $("input");
      const text = input.value;
      input.value = "";
      autosize();
      send(text);
    });
    $("input").addEventListener("input", autosize);
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        $("composer").requestSubmit();
      }
    });
    $("input").addEventListener("focus", () => $("app").classList.add("keyboard"));
    $("input").addEventListener("blur", () => setTimeout(() => $("app").classList.remove("keyboard"), 150));
    $("stop").addEventListener("click", async () => {
      try { await api("POST", "/api/stop"); } catch (e) { /* the poll shows the outcome */ }
      repollNow();
    });

    $("confirm-approve").addEventListener("click", () => answerConfirmation(true));
    $("confirm-deny").addEventListener("click", () => answerConfirmation(false));

    $("menu-button").addEventListener("click", openSheet);
    $("sheet-close").addEventListener("click", closeSheet);
    $("sheet-backdrop").addEventListener("click", closeSheet);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
    $("speak-toggle").checked = app.speak;
    $("speak-toggle").addEventListener("change", (e) => {
      app.speak = e.target.checked;
      storage.set(SPEAK_KEY, app.speak ? "1" : "0");
      unlockSpeech();
    });
    $("unpair").addEventListener("click", unpair);

    $("orb").addEventListener("click", scrollToEnd);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { orb.stop(); } else { orb.start(); wakeNow(); }
    });
    setInterval(tickConfirmTimer, 1000);
  }

  wire();
  orb.start();
  if (app.token) startPaired(); else showPairing();
})();
