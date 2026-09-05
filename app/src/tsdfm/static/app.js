// Unidirectional data flow, deliberately:
//
//   server message ─▶ setState() ─▶ render(state) ─▶ DOM
//   user action    ─▶ send()     ─▶ server
//
// Renderers read `state` and nothing else - never the DOM, never a stashed previous
// value - so the screen is always a pure function of the last known state. Actions
// never touch the DOM. This replaced a pile of cross-talking globals where a stale
// flag in one handler could contradict what another had just drawn.

// ---------------------------------------------------------------- pure helpers

const PLACEHOLDER_ART = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">' +
  '<rect width="24" height="24" fill="#262834"/>' +
  '<path d="M12 3V13.55C11.41 13.21 10.73 13 10 13C7.79 13 6 14.79 6 17S7.79 21 10 21 14 19.21 14 17V7H18V3H12Z" fill="#8b8fa3"/>' +
  '</svg>'
);

// Inline MDI (pictogrammers.com/library/mdi) icons - kept as raw SVG rather than
// pulling in an icon font/CDN, so the app has no external asset deps.
const ICONS = {
  volumeOff: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M12,4L9.91,6.09L12,8.18M4.27,3L3,4.27L7.73,9H3V15H7L12,20V13.27L16.25,17.53C15.58,18.04 14.83,18.46 14,18.7V20.77C15.38,20.45 16.63,19.82 17.68,18.96L19.73,21L21,19.73L12,10.73M19,12C19,12.94 18.8,13.82 18.46,14.64L19.97,16.15C20.62,14.91 21,13.5 21,12C21,7.72 18,4.14 14,3.23V5.29C16.89,6.15 19,8.83 19,12M16.5,12C16.5,10.23 15.5,8.71 14,7.97V10.18L16.45,12.63C16.5,12.43 16.5,12.21 16.5,12Z" /></svg>',
  volumeHigh: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z" /></svg>',
  close: '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z" /></svg>',
};

const AVATAR_EMOJI = [
  "😀", "😎", "🤠", "🥳", "🤖", "👽", "🐶", "🐱",
  "🦊", "🐼", "🐵", "🦁", "🐸", "🐙", "🦄", "🐧",
  "🍕", "🎧", "🎸", "⚡", "🔥", "🌟", "👑", "💀",
];

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function setArt(imgEl, url) {
  imgEl.onerror = () => {
    imgEl.onerror = null;
    imgEl.src = PLACEHOLDER_ART;
  };
  imgEl.src = url || PLACEHOLDER_ART;
}

function trackRow(track) {
  const li = document.createElement("li");
  const img = document.createElement("img");
  img.className = "art art-sm";
  img.alt = "";
  setArt(img, track.art_url);
  li.appendChild(img);
  const label = document.createElement("span");
  label.textContent = `${track.artist} – ${track.title}`;
  li.appendChild(label);
  return li;
}

// ------------------------------------------------------------------- identity

const INVITE_KEY = "tsdfm_invite";
const CLIENT_ID_KEY = "tsdfm_client_id";
const NAME_KEY = "tsdfm_name";
const AVATAR_KEY = "tsdfm_avatar";

function resolveInvite() {
  const url = new URL(location.href);
  const fromUrl = url.searchParams.get("invite");
  if (fromUrl) {
    localStorage.setItem(INVITE_KEY, fromUrl);
    url.searchParams.delete("invite");
    history.replaceState({}, "", url.pathname + url.search);
  }
  return localStorage.getItem(INVITE_KEY);
}

function resolveClientId() {
  let id = localStorage.getItem(CLIENT_ID_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(CLIENT_ID_KEY, id);
  }
  return id;
}

const invite = resolveInvite();
const clientId = resolveClientId();

// --------------------------------------------------------------------- state

const state = {
  // "need-invite" | "naming" | "connecting" | "live" | "disconnected"
  phase: "connecting",
  me: {
    id: null,
    name: localStorage.getItem(NAME_KEY) || "",
    avatar: localStorage.getItem(AVATAR_KEY) || "",
  },
  draftAvatar: AVATAR_EMOJI[Math.floor(Math.random() * AVATAR_EMOJI.length)],
  editingProfile: false,

  nowPlaying: null,      // server's record, plus on_air/remaining
  remainingAt: 0,        // when `remaining` was received, for smooth local ticking
  djs: [],
  listeners: [],
  chat: [],
  logs: [],
  votes: { skip: 0, skipNeeded: 1, like: 0 },
  search: { status: "", error: "", results: [] },
};

function setState(patch) {
  Object.assign(state, patch);
  render();
}

// Derived, never stored: the countdown interpolated between server ticks. The
// server remains the authority - every `progress` message resets the baseline.
function remainingNow() {
  const np = state.nowPlaying;
  if (!np || !np.on_air || np.remaining == null) return null;
  return Math.max(0, np.remaining - (Date.now() - state.remainingAt) / 1000);
}

function isDj() {
  return state.djs.some((d) => d.id === state.me.id);
}

function myQueue() {
  return state.djs.find((d) => d.id === state.me.id)?.queue || [];
}

// --------------------------------------------------------------------- render

// Private to the render layer: lets list renderers animate only genuinely new rows
// instead of replaying every entrance on unrelated state changes. Never read by
// anything outside render().
const memo = { djQueueLengths: {}, myQueueLength: 0, chatCount: 0, logCount: 0 };

function render() {
  renderOverlay();
  if (state.phase !== "live") return;
  renderProfile();
  renderNowPlaying();
  renderDjBooth();
  renderMyQueue();
  renderListeners();
  renderChat();
  renderLogs();
  renderSearch();
  renderVotes();
  syncAudio();
}

function renderOverlay() {
  const overlay = $("join-overlay");
  const box = $("join-box");
  const app = $("app");

  if (state.phase === "live") {
    overlay.style.display = "none";
    app.classList.add("visible");
    return;
  }
  app.classList.remove("visible");
  overlay.style.display = "flex";

  if (state.phase === "naming") {
    // Only (re)build the form when it isn't already up, or typing would be wiped
    // out from under the user on every render.
    if (!$("name-input")) {
      box.innerHTML = `<strong>tsdfm</strong>
        <div class="avatar-grid" id="join-avatar-grid"></div>
        <input id="name-input" placeholder="Your name" maxlength="30" />
        <button id="name-submit" type="button">Join</button>`;
      $("name-submit").addEventListener("click", submitName);
      $("name-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitName();
      });
    }
    renderAvatarGrid($("join-avatar-grid"), state.draftAvatar, (emoji) =>
      setState({ draftAvatar: emoji })
    );
    return;
  }

  const messages = {
    "need-invite": "You need an invite link to join - ask the host for one.",
    connecting: "Connecting…",
    disconnected: "Disconnected - reconnecting…",
  };
  box.innerHTML = `<strong>tsdfm</strong><p id="join-message">${messages[state.phase]}</p>`;
}

function renderAvatarGrid(container, selected, onPick) {
  container.innerHTML = AVATAR_EMOJI.map(
    (emoji) =>
      `<button type="button" class="avatar-choice${emoji === selected ? " selected" : ""}" data-emoji="${emoji}">${emoji}</button>`
  ).join("");
  container.querySelectorAll(".avatar-choice").forEach((btn) => {
    btn.addEventListener("click", () => onPick(btn.dataset.emoji));
  });
}

function renderProfile() {
  $("profile-avatar").textContent = state.me.avatar || "🙂";
  $("profile-name").textContent = state.me.name;
  $("profile-display").style.display = state.editingProfile ? "none" : "flex";
  $("profile-edit-form").style.display = state.editingProfile ? "flex" : "none";
  if (state.editingProfile && !$("profile-name-input").value) {
    $("profile-name-input").value = state.me.name;
    renderAvatarGrid($("profile-avatar-grid"), state.draftAvatar, (emoji) =>
      setState({ draftAvatar: emoji })
    );
  }
}

function renderNowPlaying() {
  const np = state.nowPlaying;
  const title = $("np-title");
  const meta = $("np-meta");
  const art = $("np-art");
  const liveDot = $("live-dot");
  const pos = $("np-position");
  const fill = $("np-progress-fill");

  if (!np) {
    title.textContent = "Nothing playing";
    meta.textContent = "Step up to DJ and queue a track to get things started.";
    art.style.display = "none";
    liveDot.style.display = "none";
    pos.textContent = "";
    fill.style.width = "0%";
    return;
  }

  title.textContent = np.title;
  art.style.display = "block";
  setArt(art, np.art_url);

  if (!np.on_air) {
    // Liquidsoap has the request but hasn't started decoding it. Showing a running
    // clock here is the lie that made the room look broken.
    meta.textContent = `${np.artist} — cueing up…`;
    liveDot.style.display = "none";
    pos.textContent = "buffering…";
    fill.style.width = "0%";
    return;
  }

  meta.textContent = `${np.artist} — spun by ${np.dj_name}`;
  liveDot.style.display = "inline-block";

  const remaining = remainingNow();
  const duration = np.duration || 0;
  if (remaining == null || duration <= 0) {
    pos.textContent = "";
    fill.style.width = "0%";
    return;
  }
  const elapsed = Math.max(0, duration - remaining);
  pos.textContent = `${formatTime(elapsed)} / ${formatTime(duration)}`;
  fill.style.width = `${Math.min(100, (elapsed / duration) * 100)}%`;
}

function renderDjBooth() {
  const list = $("dj-list");
  list.innerHTML = "";

  if (state.djs.length === 0) {
    list.innerHTML = '<li class="meta">No one has stepped up to DJ yet.</li>';
  }

  const lengths = {};
  for (const dj of state.djs) {
    const li = document.createElement("li");
    li.className = "dj-entry";

    const header = document.createElement("div");
    header.className = "dj-name";
    header.textContent = `${dj.avatar} ${dj.name}${dj.online === false ? " (away)" : ""}`;
    li.appendChild(header);

    if (dj.queue.length === 0) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "— empty";
      li.appendChild(empty);
    } else {
      const previously = memo.djQueueLengths[dj.id] || 0;
      const trackList = document.createElement("ul");
      trackList.className = "dj-track-list";
      dj.queue.forEach((track, index) => {
        const row = trackRow(track);
        if (index >= previously) row.classList.add("enter");
        trackList.appendChild(row);
      });
      li.appendChild(trackList);
    }
    lengths[dj.id] = dj.queue.length;
    list.appendChild(li);
  }
  memo.djQueueLengths = lengths;

  $("dj-toggle").textContent = isDj() ? "Step down" : "Step up to DJ";
  $("queue-panel").style.display = isDj() ? "block" : "none";
}

function renderMyQueue() {
  if (!isDj()) return;
  const list = $("my-queue-list");
  const queue = myQueue();
  list.innerHTML = "";

  if (queue.length === 0) {
    list.innerHTML = '<li class="meta">Nothing queued yet - search above and click a track to add it.</li>';
    memo.myQueueLength = 0;
    return;
  }
  queue.forEach((track, index) => {
    const li = trackRow(track);
    if (index >= memo.myQueueLength) li.classList.add("enter");
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "icon-btn";
    removeBtn.innerHTML = ICONS.close;
    removeBtn.addEventListener("click", () => send({ type: "remove_track", index }));
    li.appendChild(removeBtn);
    list.appendChild(li);
  });
  memo.myQueueLength = queue.length;
}

function renderListeners() {
  $("listeners").textContent =
    state.listeners.map((u) => `${u.avatar} ${u.name}`).join(", ") || "Just you.";
}

// Chat and logs only ever grow, so they append rather than rebuild - rebuilding
// would restart every entrance animation and fight the user's scroll position.
function renderChat() {
  const log = $("chat-log");
  if (state.chat.length < memo.chatCount) {
    log.innerHTML = "";
    memo.chatCount = 0;
  }
  for (const msg of state.chat.slice(memo.chatCount)) {
    const li = document.createElement("li");
    li.className = "enter";
    const avatar = msg.avatar ? `${msg.avatar} ` : "";
    li.innerHTML = `<span class="user">${avatar}${escapeHtml(msg.user)}</span>${escapeHtml(msg.text)}`;
    log.appendChild(li);
  }
  if (state.chat.length !== memo.chatCount) log.scrollTop = log.scrollHeight;
  memo.chatCount = state.chat.length;
}

function renderLogs() {
  const panel = $("log-panel");
  if (state.logs.length < memo.logCount) {
    panel.innerHTML = "";
    memo.logCount = 0;
  }
  for (const entry of state.logs.slice(memo.logCount)) {
    const li = document.createElement("li");
    li.className = `level-${entry.level} enter`;
    const time = new Date(entry.ts * 1000).toLocaleTimeString();
    li.innerHTML = `<span class="log-time">${time}</span>${escapeHtml(entry.message)}`;
    panel.appendChild(li);
  }
  if (state.logs.length !== memo.logCount) panel.scrollTop = panel.scrollHeight;
  memo.logCount = state.logs.length;
}

function renderSearch() {
  $("search-status").textContent = state.search.status;
  $("search-error").textContent = state.search.error;
  const list = $("search-results");
  list.innerHTML = "";
  for (const track of state.search.results) {
    const li = trackRow(track);
    li.style.cursor = "pointer";
    li.addEventListener("click", () => {
      send({ type: "queue_track", ...track });
      setState({ search: { ...state.search, status: `Added "${track.title}" to your queue.` } });
    });
    list.appendChild(li);
  }
}

function renderVotes() {
  $("skip-count").textContent = state.votes.skip;
  $("skip-needed").textContent = state.votes.skipNeeded;
  $("like-count").textContent = state.votes.like;
}

// The countdown is the only thing that changes without a server message.
setInterval(() => {
  if (state.phase === "live" && state.nowPlaying?.on_air) renderNowPlaying();
}, 1000);

// ------------------------------------------------------------------ transport

let ws = null;

function send(message) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

async function connect() {
  if (!(await establishSession())) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.addEventListener("open", () => {
    send({ type: "join", invite, client_id: clientId, name: state.me.name, avatar: state.me.avatar });
  });

  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "you") {
      setState({ me: { ...state.me, id: msg.id } });
    } else if (msg.type === "state") {
      setState({
        phase: "live",
        nowPlaying: msg.now_playing,
        remainingAt: Date.now(),
        djs: msg.dj_order,
        listeners: msg.users,
        chat: msg.chat_history,
        votes: {
          skip: msg.skip_votes,
          skipNeeded: msg.skip_votes_needed,
          like: msg.like_votes,
        },
      });
    } else if (msg.type === "progress") {
      // Countdown only - carries no track identity, so patch rather than replace.
      if (!state.nowPlaying) return;
      setState({
        nowPlaying: { ...state.nowPlaying, remaining: msg.remaining, on_air: msg.on_air },
        remainingAt: Date.now(),
      });
    } else if (msg.type === "chat") {
      setState({ chat: [...state.chat, msg] });
    } else if (msg.type === "log") {
      setState({ logs: [...state.logs, msg] });
    } else if (msg.type === "error") {
      alert(msg.message);
    }
  });

  ws.addEventListener("close", () => {
    setState({ phase: "disconnected" });
    setTimeout(connect, 2000);
  });
}

// --------------------------------------------------------------------- actions

function submitName() {
  const name = $("name-input").value.trim();
  if (!name) return;
  localStorage.setItem(NAME_KEY, name);
  localStorage.setItem(AVATAR_KEY, state.draftAvatar);
  setState({
    me: { ...state.me, name, avatar: state.draftAvatar },
    phase: "connecting",
  });
  // Clicking Join is a real user gesture, so audible autoplay is permitted here.
  startPlayback();
  connect();
}

function saveProfile() {
  const name = $("profile-name-input").value.trim();
  if (!name) return;
  localStorage.setItem(NAME_KEY, name);
  localStorage.setItem(AVATAR_KEY, state.draftAvatar);
  setState({ me: { ...state.me, name, avatar: state.draftAvatar }, editingProfile: false });
  // The server treats a re-join with the same client_id as an update, not a new join.
  send({ type: "join", invite, client_id: clientId, name, avatar: state.draftAvatar });
}

function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  send({ type: "chat", text });
  input.value = "";
}

async function runSearch() {
  const q = $("search-input").value.trim();
  if (!q) return;
  setState({ search: { status: "Searching…", error: "", results: [] } });

  let resp, body;
  try {
    resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    body = await resp.json();
  } catch (err) {
    setState({ search: { status: "", error: `Search request failed: ${err.message}`, results: [] } });
    return;
  }
  if (!resp.ok) {
    setState({
      search: { status: "", error: body.error || `Search failed (${resp.status})`, results: [] },
    });
    return;
  }
  setState({
    search: {
      status: body.length === 0 ? "No matches." : "",
      error: "",
      results: body,
    },
  });
}

async function loadLogHistory() {
  try {
    const resp = await fetch("/api/logs");
    setState({ logs: await resp.json() });
  } catch {
    // best-effort; the panel just stays empty if this fails
  }
}

// ----------------------------------------------------------------------- audio
//
// A media element is inherently imperative, so it stays a side-effect module. It is
// *driven* by state (syncAudio) but never writes to it.

const radioAudio = $("radio-audio");
const muteBtn = $("mute-btn");
const volumeSlider = $("volume-slider");
const LIVE_LAG_TOLERANCE = 5;
let lastSyncedTrackId = null;

function syncMuteIcon() {
  const silent = radioAudio.muted || radioAudio.volume === 0;
  muteBtn.innerHTML = silent ? ICONS.volumeOff : ICONS.volumeHigh;
  muteBtn.setAttribute("aria-label", silent ? "Unmute" : "Mute");
}

// The icon follows the element's own events rather than our assumptions, so it can
// never show "on" while the browser has actually left the stream muted.
radioAudio.addEventListener("volumechange", syncMuteIcon);
syncMuteIcon();

function ensurePlaying() {
  // A live broadcast has no meaningful "pause" - if it stopped (blocked autoplay, a
  // network blip), get it going again.
  if (radioAudio.paused) radioAudio.play().catch(() => {});
}

function liveLagSeconds() {
  try {
    const buffered = radioAudio.buffered;
    if (!buffered.length) return 0;
    return buffered.end(buffered.length - 1) - radioAudio.currentTime;
  } catch {
    return 0;
  }
}

function resyncToLiveEdge() {
  // Sitting in an idle room means buffering silence. If that backlog builds up, a
  // track starting at the source isn't heard until the backlog drains.
  if (liveLagSeconds() <= LIVE_LAG_TOLERANCE) return;
  try {
    radioAudio.currentTime = radioAudio.buffered.end(radioAudio.buffered.length - 1);
    if (liveLagSeconds() <= LIVE_LAG_TOLERANCE) return;
  } catch {
    // Live streams usually aren't seekable; fall through and reconnect instead.
  }
  const wasMuted = radioAudio.muted;
  radioAudio.load();
  radioAudio.muted = wasMuted;
  ensurePlaying();
}

function syncAudio() {
  const np = state.nowPlaying;
  const id = np && np.on_air ? np.id : null;
  if (id && id !== lastSyncedTrackId) {
    lastSyncedTrackId = id;
    resyncToLiveEdge();
  }
  if (!id) lastSyncedTrackId = null;
}

async function startPlayback() {
  // Default to audible: unmuted is what people expect from a radio.
  radioAudio.muted = false;
  try {
    await radioAudio.play();
  } catch {
    // Browsers refuse audible autoplay until the page has been interacted with. A
    // muted start is always allowed, so fall back to that; the mute button's own
    // click is a real user gesture and will unmute it.
    //
    // Deliberately NOT wired to "any click anywhere": that made unmuting look
    // random - clicking chat would silently turn sound on with no visible cause.
    radioAudio.muted = true;
    ensurePlaying();
  }
}

// ---------------------------------------------------------------------- wiring

muteBtn.addEventListener("click", () => {
  radioAudio.muted = !radioAudio.muted;
  ensurePlaying();
});

volumeSlider.addEventListener("input", () => {
  radioAudio.volume = parseFloat(volumeSlider.value);
  // Dragging the slider up is an unambiguous "I want sound".
  if (radioAudio.volume > 0) radioAudio.muted = false;
  ensurePlaying();
});

$("dj-toggle").addEventListener("click", () => send({ type: isDj() ? "step_down" : "step_up" }));
$("skip-btn").addEventListener("click", () => send({ type: "vote_skip" }));
$("like-btn").addEventListener("click", () => send({ type: "vote_like" }));
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
$("search-btn").addEventListener("click", runSearch);
$("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});
$("profile-edit-btn").addEventListener("click", () =>
  setState({ editingProfile: true, draftAvatar: state.me.avatar || state.draftAvatar })
);
$("profile-save-btn").addEventListener("click", saveProfile);
$("profile-cancel-btn").addEventListener("click", () => setState({ editingProfile: false }));

// ------------------------------------------------------------------- bootstrap

async function establishSession() {
  if (!invite) {
    setState({ phase: "need-invite" });
    return false;
  }
  try {
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invite }),
    });
    if (response.status === 401) {
      setState({ phase: "need-invite" });
      return false;
    }
    if (!response.ok) throw new Error("Session unavailable");
    return true;
  } catch {
    setState({ phase: "disconnected" });
    setTimeout(bootstrap, 2000);
    return false;
  }
}

async function bootstrap() {
  if (!(await establishSession())) return;
  startPlayback();
  loadLogHistory();
  if (!state.me.name) {
    setState({ phase: "naming" });
  } else {
    setState({ phase: "connecting" });
    connect();
  }
}

bootstrap();
