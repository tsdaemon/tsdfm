// ----------------------------------------------------------------- visualizer
//
// Self-contained: a hand-rolled spectrum painted on #viz-bg, a <canvas> layered
// behind the On Air panel's content so the bars fill the space around the small
// album art and short titles. One AnalyserNode taps the page's <audio> element.
// A pure side effect - it reads the palette off <html data-theme> and the picked
// style off localStorage, and never touches app state. The style picker sits in
// the panel's button row.
//
// createMediaElementSource() routes the <audio> element through the Web Audio
// graph. On a cross-origin stream WITHOUT CORS that graph outputs nothing but
// zeroes - which silences playback itself, not just the analyser. So it only
// runs when the stream is readable:
//   - same-origin (prod: app and /radio.mp3 share a host), or
//   - cross-origin but Icecast is sending credentialed-CORS headers, which the
//     server signals by rendering crossorigin="use-credentials" onto the
//     <audio> tag (ICECAST_CORS_ORIGIN set - the local-dev :8080/:6491 split).
// If that opt-in is set but Icecast doesn't actually honour it, the element
// errors immediately; the CORS guard drops crossOrigin and reconnects plain so
// audio still works and the visualizer just stays inert.

(() => {
  const $ = (id) => document.getElementById(id);
  const radioAudio = $("radio-audio");
  if (!radioAudio) return;

  const VIZ_KEY = "tsdfm_viz";       // stored value: "off" or a style key
  const VIZ_BARS = 64;

  const VIZ_STYLES = [
    { key: "bars", label: "Bars" },
    { key: "mirror", label: "Mirror" },
    { key: "blocks", label: "Blocks" },
    { key: "radial", label: "Radial" },
    { key: "wave", label: "Waveform" },
  ];

  const vizCanvas = $("viz-bg");

  const vizSameOrigin = (() => {
    try {
      const src = radioAudio.getAttribute("src") || "";
      return !!src && new URL(src, location.href).origin === location.origin;
    } catch { return false; }
  })();
  const vizCredentialed = radioAudio.crossOrigin === "use-credentials";
  const vizCanUse = vizSameOrigin || vizCredentialed;

  // Cross-origin + crossOrigin opt-in: don't trust the tap until playback has
  // actually started, and bail out cleanly if the CORS handshake fails.
  let vizCorsOk = !(vizCredentialed && !vizSameOrigin);
  if (!vizCorsOk) {
    const done = () => {
      radioAudio.removeEventListener("error", onErr);
      radioAudio.removeEventListener("playing", onOk);
    };
    const onErr = () => {
      done();
      radioAudio.removeAttribute("crossorigin");
      radioAudio.load();
      if (radioAudio.paused) radioAudio.play().catch(() => {});
    };
    const onOk = () => { done(); vizCorsOk = true; armVisualizer(); };
    radioAudio.addEventListener("error", onErr);
    radioAudio.addEventListener("playing", onOk);
  }

  let vizCtx = null;         // AudioContext
  let vizAnalyser = null;
  let vizSource = null;      // MediaElementAudioSourceNode - created once, ever
  let vizG = null;           // canvas 2d context
  let vizFreq = null;        // Uint8Array, frequency bins
  let vizWave = null;        // Uint8Array, time-domain samples
  const vizBarEase = new Float32Array(VIZ_BARS);   // per-bar decay so drops glide
  const vizLevels = new Float32Array(VIZ_BARS);    // reused scratch, refilled per frame
  let vizRaf = 0;
  let vizLast = 0;          // last drawn frame timestamp, for the ~30fps cap
  let vizCurrentStyle = "bars";  // cached so the frame loop never hits localStorage
  let vizW = 0, vizH = 0;
  let vizColors = { panel: "#10212c", accent: "#fe7f2d", text: "#eaecf0" };

  // "off" or one of VIZ_STYLES. Default: a style, unless the viewer asked for
  // less motion.
  function vizSetting() {
    let v = null;
    try { v = localStorage.getItem(VIZ_KEY); } catch {}
    if (v === "off" || VIZ_STYLES.some((s) => s.key === v)) return v;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "off" : "bars";
  }

  function readVizColors() {
    const cs = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
    vizColors = {
      panel: pick("--panel", "#10212c"),
      accent: pick("--accent", "#fe7f2d"),
      text: pick("--text", "#eaecf0"),
    };
  }

  function resizeVizCanvas() {
    const r = vizCanvas.getBoundingClientRect();
    // Crisp, but keep the backing store bounded (DPR <= 1.5, long edge <= 2400)
    // so it can't balloon on a hidpi ultrawide.
    const cssW = Math.max(1, r.width), cssH = Math.max(1, r.height);
    const dpr = Math.min(1.5, window.devicePixelRatio || 1);
    const scale = dpr * Math.min(1, 2400 / (Math.max(cssW, cssH) * dpr));
    vizW = Math.round(cssW * scale);
    vizH = Math.round(cssH * scale);
    vizCanvas.width = vizW;
    vizCanvas.height = vizH;
    vizG.setTransform(1, 0, 0, 1, 0, 0);
  }

  // Log-spaced bar amplitudes in 0..1. Music energy piles up at the low end, so
  // even bar spacing wastes most of the width; a log map spreads it out.
  function vizBarLevels() {
    const bins = vizAnalyser.frequencyBinCount;
    const nyquist = vizCtx.sampleRate / 2;
    const minHz = 30, maxHz = Math.min(16000, nyquist);
    for (let i = 0; i < VIZ_BARS; i++) {
      const lo = minHz * Math.pow(maxHz / minHz, i / VIZ_BARS);
      const hi = minHz * Math.pow(maxHz / minHz, (i + 1) / VIZ_BARS);
      const a = Math.round((lo / nyquist) * bins);
      const b = Math.max(a + 1, Math.round((hi / nyquist) * bins));
      let peak = 0;
      for (let j = a; j < b && j < bins; j++) if (vizFreq[j] > peak) peak = vizFreq[j];
      const target = peak / 255;
      // Fast attack, slow release, so bars snap up but glide back down.
      vizBarEase[i] = target > vizBarEase[i] ? target : vizBarEase[i] * 0.88 + target * 0.12;
      vizLevels[i] = vizBarEase[i];
    }
    return vizLevels;
  }

  function drawBars(g, levels, mirror) {
    const gap = 2;
    const bw = (vizW - gap * (VIZ_BARS - 1)) / VIZ_BARS;
    const base = mirror ? vizH / 2 : vizH;
    const grad = g.createLinearGradient(0, vizH, 0, 0);
    grad.addColorStop(0, vizColors.panel);
    grad.addColorStop(0.5, vizColors.accent);
    grad.addColorStop(1, vizColors.text);
    g.fillStyle = grad;
    for (let i = 0; i < VIZ_BARS; i++) {
      const h = Math.max(1, levels[i] * (mirror ? vizH / 2 : vizH) * 0.95);
      const x = i * (bw + gap);
      g.fillRect(x, base - h, bw, h);
      if (mirror) g.fillRect(x, base, bw, h);
    }
  }

  function drawBlocks(g, levels) {
    const gap = 2, cell = 6, cgap = 2;
    const bw = (vizW - gap * (VIZ_BARS - 1)) / VIZ_BARS;
    for (let i = 0; i < VIZ_BARS; i++) {
      const lit = Math.round((levels[i] * vizH * 0.95) / (cell + cgap));
      const x = i * (bw + gap);
      for (let c = 0; c < lit; c++) {
        const y = vizH - (c + 1) * (cell + cgap) + cgap;
        const t = (c * (cell + cgap)) / vizH;
        g.fillStyle = t > 0.7 ? vizColors.text : vizColors.accent;
        g.globalAlpha = t > 0.7 ? 1 : 0.5 + t * 0.6;
        g.fillRect(x, y, bw, cell);
      }
    }
    g.globalAlpha = 1;
  }

  // Compact starburst centred in the band - sized off the (short) canvas height
  // so quiet vs loud is actually legible instead of everything blasting past the
  // edges. One beginPath/stroke for all 64 spokes.
  function drawRadial(g, levels) {
    const r0 = vizH * 0.08;
    const rMax = vizH * 0.72;
    g.save();
    g.translate(vizW / 2, vizH / 2);
    g.strokeStyle = vizColors.accent;
    g.lineWidth = Math.max(1.5, (2 * Math.PI * r0) / VIZ_BARS);
    g.globalAlpha = 0.8;
    g.beginPath();
    for (let i = 0; i < VIZ_BARS; i++) {
      const ang = (i / VIZ_BARS) * Math.PI * 2 - Math.PI / 2;
      const len = r0 + levels[i] * (rMax - r0);
      const cos = Math.cos(ang), sin = Math.sin(ang);
      g.moveTo(cos * r0, sin * r0);
      g.lineTo(cos * len, sin * len);
    }
    g.stroke();
    g.restore();
  }

  // Amplified so real music swings toward the edges, with a light area fill down
  // to the base so it occupies the frame rather than sitting as a thin band.
  // Decimated (step 4) and capped-resolution - the raw 2048-point fill was the hang.
  function drawWave(g) {
    const n = vizWave.length, step = 4, mid = vizH / 2, gain = 2.8;
    g.beginPath();
    for (let i = 0; i < n; i += step) {
      const x = (i / (n - 1)) * vizW;
      const dev = ((vizWave[i] - 128) / 128) * mid * gain;
      const y = mid + Math.max(-mid, Math.min(mid, dev));
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    }
    g.strokeStyle = vizColors.accent;
    g.lineWidth = 1.5;
    g.globalAlpha = 0.8;
    g.stroke();
    g.lineTo(vizW, vizH);
    g.lineTo(0, vizH);
    g.closePath();
    g.globalAlpha = 0.14;
    g.fillStyle = vizColors.accent;
    g.fill();
    g.globalAlpha = 1;
  }

  function vizFrame(ts) {
    vizRaf = requestAnimationFrame(vizFrame);
    // ~30fps is plenty for this and halves the per-frame cost.
    if (vizLast && ts - vizLast < 33) return;
    vizLast = ts;
    try {
      vizG.clearRect(0, 0, vizW, vizH);
      if (vizCurrentStyle === "wave") {
        vizAnalyser.getByteTimeDomainData(vizWave);
        drawWave(vizG);
        return;
      }
      vizAnalyser.getByteFrequencyData(vizFreq);
      const levels = vizBarLevels();
      if (vizCurrentStyle === "mirror") drawBars(vizG, levels, true);
      else if (vizCurrentStyle === "blocks") drawBlocks(vizG, levels);
      else if (vizCurrentStyle === "radial") drawRadial(vizG, levels);
      else drawBars(vizG, levels, false);
    } catch (err) {
      // A bad frame must never take the page down with it - stop and bow out.
      console.warn("visualizer frame failed, stopping:", err);
      vizStop();
    }
  }

  function vizStart() {
    if (!vizRaf && vizAnalyser) vizFrame();
  }

  function vizStop() {
    if (vizRaf) cancelAnimationFrame(vizRaf);
    vizRaf = 0;
    if (vizW) vizG.clearRect(0, 0, vizW, vizH);
  }

  // Reconcile the running loop AND the picker UI with the stored setting. Safe to
  // call before the audio graph exists (initVisualizer() calls it again once the
  // analyser is live).
  function applyVizSetting() {
    const setting = vizSetting();
    const style = VIZ_STYLES.find((s) => s.key === setting);
    const name = $("viz-trigger-name");
    if (name) name.textContent = style ? style.label : "Off";
    const menu = $("viz-menu");
    if (menu) menu.querySelectorAll(".viz-opt").forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.viz === setting)));

    if (setting === "off") { vizStop(); return; }
    if (vizCurrentStyle !== setting) { vizCurrentStyle = setting; vizBarEase.fill(0); }
    if (vizAnalyser) { resizeVizCanvas(); vizStart(); }
  }

  function closeVizMenu() {
    const menu = $("viz-menu");
    if (menu && !menu.hidden) {
      menu.hidden = true;
      $("viz-trigger").setAttribute("aria-expanded", "false");
    }
  }

  // Custom picker in the On Air button row, styled like the theme picker. Built
  // once at load - usable before the first gesture.
  function buildVizMenu() {
    const picker = $("viz-picker"), menu = $("viz-menu"), trigger = $("viz-trigger");
    if (!picker || !menu || !trigger || !vizCanUse || !vizCanvas) return;

    menu.innerHTML = [{ key: "off", label: "Off" }, ...VIZ_STYLES]
      .map((o) => `<button type="button" class="viz-opt" role="option" data-viz="${o.key}">${o.label}</button>`)
      .join("");
    menu.querySelectorAll(".viz-opt").forEach((btn) => {
      btn.addEventListener("click", () => {
        try { localStorage.setItem(VIZ_KEY, btn.dataset.viz); } catch {}
        closeVizMenu();
        applyVizSetting();
      });
    });
    trigger.addEventListener("click", () => {
      const open = menu.hidden;
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#viz-picker")) closeVizMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeVizMenu();
    });

    picker.hidden = false;
    applyVizSetting();
  }

  function initVisualizer() {
    if (vizSource) return;  // createMediaElementSource is one-shot per element
    if (!window.AudioContext || !vizCanvas || !vizCanUse || !vizCorsOk) return;
    try {
      vizCtx = new AudioContext();
      vizSource = vizCtx.createMediaElementSource(radioAudio);
      vizAnalyser = vizCtx.createAnalyser();
      vizAnalyser.fftSize = 2048;
      vizAnalyser.smoothingTimeConstant = 0.82;
      // The element must still reach the speakers: source -> analyser -> output.
      vizSource.connect(vizAnalyser);
      vizAnalyser.connect(vizCtx.destination);
    } catch (err) {
      console.warn("visualizer unavailable:", err);
      return;
    }
    vizG = vizCanvas.getContext("2d");
    vizFreq = new Uint8Array(vizAnalyser.frequencyBinCount);
    vizWave = new Uint8Array(vizAnalyser.fftSize);

    readVizColors();
    new MutationObserver(readVizColors).observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme"],
    });
    // The canvas tracks the On Air panel, not the viewport - a ResizeObserver
    // catches panel reflow and the room<->library show/hide.
    new ResizeObserver(() => { if (vizRaf) resizeVizCanvas(); }).observe(vizCanvas);

    resizeVizCanvas();
    applyVizSetting();
  }

  // The AudioContext (and its media-element tap) can only start on a user
  // gesture, so hold off until the first interaction AND until any pending CORS
  // check has cleared (vizCorsOk). Either trigger calls armVisualizer(); it acts
  // only once both are true, and is a no-op after the first successful init.
  let vizGestured = false;
  function armVisualizer() {
    if (!vizGestured || !vizCorsOk) return;
    initVisualizer();
    if (!vizCtx) return;
    if (vizCtx.state === "suspended") vizCtx.resume();
    for (const ev of ["pointerdown", "keydown", "touchstart"]) {
      document.removeEventListener(ev, onVizGesture);
    }
  }
  function onVizGesture() {
    vizGestured = true;
    armVisualizer();
  }
  if (vizCanUse && vizCanvas) {
    for (const ev of ["pointerdown", "keydown", "touchstart"]) {
      document.addEventListener(ev, onVizGesture, { passive: true });
    }
  }

  buildVizMenu();
})();
