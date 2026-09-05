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
  plus: '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M19,13H13V19H11V13H5V11H11V5H13V11H19V13Z" /></svg>',
};

const AVATAR_EMOJI = [
  "😀", "😎", "🤠", "🥳", "🤖", "👽", "🐶", "🐱",
  "🦊", "🐼", "🐵", "🦁", "🐸", "🐙", "🦄", "🐧",
  "🍕", "🎧", "🎸", "⚡", "🔥", "🌟", "👑", "💀",
];

// Palette themes. `colors` is only the picker swatch - the values that actually
// paint the UI live in style.css under :root[data-theme="<key>"]. `ember` is the
// default and matches the bare :root block there.
const THEMES = [
  { key: "ember", label: "Midnight Ember", colors: ["#000000", "#233d4d", "#fe7f2d", "#eaecf0"] },
  { key: "lagoon", label: "Lagoon", colors: ["#224248", "#325e6a", "#44a1a4", "#ff9a00"] },
  { key: "pine", label: "Pine", colors: ["#092328", "#12544f", "#2a835f", "#8bbb92"] },
  { key: "daylight", label: "Daylight", colors: ["#f5f5f5", "#76abae", "#303841", "#ff5722"] },
];
const DEFAULT_THEME = "ember";

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
  img.draggable = false;   // don't let the thumbnail hijack a row drag
  setArt(img, track.art_url);
  li.appendChild(img);
  const label = document.createElement("span");
  label.textContent = `${track.artist} – ${track.title}`;
  li.appendChild(label);
  const plays = track.plays || 0, likes = track.likes || 0, favs = track.favorites || 0;
  if (plays > 0 || likes > 0 || favs > 0) {
    const stats = document.createElement("span");
    stats.className = "track-stats";
    stats.textContent = [
      plays > 0 && `▶ ${plays}`,
      likes > 0 && `♥ ${likes}`,
      favs > 0 && `★ ${favs}`,
    ].filter(Boolean).join(" · ");
    stats.title = [
      `Played ${plays}× in the room`,
      likes > 0 && `${likes} like${likes === 1 ? "" : "s"}`,
      favs > 0 && `favourited ${favs}×`,
    ].filter(Boolean).join(" · ");
    li.appendChild(stats);
  }
  return li;
}

// ------------------------------------------------------------------- identity

const INVITE_KEY = "tsdfm_invite";
const CLIENT_ID_KEY = "tsdfm_client_id";
const NAME_KEY = "tsdfm_name";
const AVATAR_KEY = "tsdfm_avatar";
const THEME_KEY = "tsdfm_theme";

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

function resolveTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  return THEMES.some((t) => t.key === saved) ? saved : DEFAULT_THEME;
}

const invite = resolveInvite();
const clientId = resolveClientId();

// --------------------------------------------------------------------- state

const HISTORY_PAGE_SIZE = 10;
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
  theme: resolveTheme(),
  themeMenuOpen: false,

  nowPlaying: null,      // server's record, plus on_air/remaining
  remainingAt: 0,        // when `remaining` was received, for smooth local ticking
  djs: [],
  listeners: [],
  chat: [],
  history: [],           // recently played, oldest first (as sent by the server)
  historyPage: 0,
  logs: [],
  votes: { skip: 0, skipNeeded: 1, like: 0 },
  favorited: false,      // current track starred in Navidrome
  view: "room",
  libraryDraft: "",
  library: { kind: "alphabeticalByName", query: "", offset: 0, albums: [], album: null, songs: [], artists: [], artist: null, stats: null, loading: false, loaded: false, error: "" },
  search: { status: "", error: "", results: [] },
  toast: null,          // { text } - transient confirmation, cleared on a timer
  justQueued: null,     // { id } - row to flash after a queue action; cleared on a timer
};

function setState(patch) {
  Object.assign(state, patch);
  state.historyPage = Math.max(0, Math.min(state.historyPage, Math.ceil(state.history.length / HISTORY_PAGE_SIZE) - 1));
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
const memo = { djQueueLengths: {}, myQueueLength: 0, libQueueLength: 0, chatCount: 0, logCount: 0 };

function render() {
  document.documentElement.dataset.theme = state.theme;
  renderOverlay();
  if (state.phase !== "live") return;
  renderProfile();
  renderTheme();
  renderNowPlaying();
  renderNavStrip();
  renderDjBooth();
  renderMyQueue();
  renderListeners();
  renderHistory();
  renderChat();
  renderLogs();
  renderSearch();
  renderVotes();
  renderLibrary();
  renderLibraryQueue();
  renderToast();
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

const swatchHtml = (colors) => colors.map((c) => `<i style="background:${c}"></i>`).join("");

// Built once (the palette list is static); render() only syncs open/selected.
function buildThemeMenu() {
  const menu = $("theme-menu");
  if (!menu) return;
  menu.innerHTML = THEMES.map(
    (t) =>
      `<button type="button" class="theme-option" role="option" data-theme-key="${t.key}" title="${t.label}" aria-label="${t.label}">` +
        `<span class="theme-swatch" aria-hidden="true">${swatchHtml(t.colors)}</span>` +
      `</button>`
  ).join("");
  menu.querySelectorAll(".theme-option").forEach((btn) => {
    btn.addEventListener("click", () => {
      localStorage.setItem(THEME_KEY, btn.dataset.themeKey);
      setState({ theme: btn.dataset.themeKey, themeMenuOpen: false });
    });
  });
  $("theme-trigger").addEventListener("click", () =>
    setState({ themeMenuOpen: !state.themeMenuOpen })
  );
  // Dismiss on an outside click or Escape.
  document.addEventListener("click", (e) => {
    if (state.themeMenuOpen && !e.target.closest("#theme-picker")) {
      setState({ themeMenuOpen: false });
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.themeMenuOpen) setState({ themeMenuOpen: false });
  });
}

function renderTheme() {
  const theme = THEMES.find((t) => t.key === state.theme);
  const swatch = $("theme-swatch");
  if (swatch && swatch.dataset.theme !== state.theme) {
    swatch.dataset.theme = state.theme;
    swatch.innerHTML = swatchHtml(theme ? theme.colors : []);
  }
  $("theme-trigger-name").textContent = theme ? theme.label : "";
  $("theme-trigger").setAttribute("aria-expanded", String(state.themeMenuOpen));
  $("theme-menu").hidden = !state.themeMenuOpen;
  $("theme-menu").querySelectorAll(".theme-option").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.themeKey === state.theme));
  });
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

// The Library view drops the full On Air panel, so what's playing collapses to a
// one-liner in the nav bar. It doubles as a shortcut back to the Radio room.
function renderNavStrip() {
  const strip = $("nav-now-playing");
  const np = state.nowPlaying;
  const show = state.view === "library" && Boolean(np);
  strip.hidden = !show;
  if (!show) return;
  setArt($("nav-np-art"), np.art_url);
  $("nav-np-text").textContent = np.on_air
    ? `${np.title} — ${np.artist}`
    : `${np.title} — cueing up…`;
  $("nav-live-dot").hidden = !np.on_air;
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

// Both queue views (the Radio room's "Up next", the Library's right rail) draw
// the same list from the same state - this keeps them from drifting. memoKey
// names this view's own row-count baseline so entrance animations fire only on
// genuinely new rows, independently per view.
function renderQueueInto(list, memoKey, emptyText) {
  const queue = myQueue();
  list.innerHTML = "";
  if (queue.length === 0) {
    list.innerHTML = `<li class="meta">${emptyText}</li>`;
    memo[memoKey] = 0;
    return;
  }
  queue.forEach((track, index) => {
    const li = trackRow(track);
    li.classList.add("queue-row");
    if (index >= memo[memoKey]) li.classList.add("enter");

    // Drag the whole row to reorder. Drop lands the track where the target row is.
    li.draggable = true;
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(index));
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      li.classList.add("drop-target");
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
    li.addEventListener("drop", (e) => {
      e.preventDefault();
      li.classList.remove("drop-target");
      const from = Number(e.dataTransfer.getData("text/plain"));
      if (Number.isInteger(from) && from !== index) send({ type: "move_track", from, to: index });
    });

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "icon-btn";
    removeBtn.innerHTML = ICONS.close;
    removeBtn.setAttribute("aria-label", `Remove ${track.title}`);
    removeBtn.addEventListener("click", () => send({ type: "remove_track", index }));
    li.appendChild(removeBtn);
    list.appendChild(li);
  });
  memo[memoKey] = queue.length;
}

function renderMyQueue() {
  if (!isDj()) return;
  renderQueueInto($("my-queue-list"), "myQueueLength",
    "Nothing queued yet - search above and click a track to add it.");
}

function renderLibraryQueue() {
  const list = $("library-queue-list");
  if (!isDj()) {
    list.innerHTML = '<li class="meta">Step up to DJ in the Radio room to start a queue.</li>';
    memo.libQueueLength = 0;
    return;
  }
  renderQueueInto(list, "libQueueLength", "Nothing queued yet - click a track to add it.");
}

function renderListeners() {
  $("listeners").textContent =
    state.listeners.map((u) => `${u.avatar} ${u.name}`).join(", ") || "Just you.";
}

// Everything that's been on air, newest first. A DJ can click any row to drop it
// back on their own queue (same click-to-queue contract as the library).
function renderHistory() {
  const list = $("history-list");
  const pageSize = HISTORY_PAGE_SIZE;
  const pages = Math.max(1, Math.ceil(state.history.length / pageSize));
  const entries = state.history.slice().reverse().slice(state.historyPage * pageSize, (state.historyPage + 1) * pageSize);
  $("history-pagination").hidden = pages === 1;
  $("history-page").textContent = `Page ${state.historyPage + 1} of ${pages}`;
  $("history-prev").disabled = state.historyPage === 0;
  $("history-next").disabled = state.historyPage >= pages - 1;
  const dj = isDj();
  // Keep the scroll position and focused rows intact during unrelated updates.
  const key = JSON.stringify([entries, state.historyPage, dj, state.justQueued?.id]);
  if (memo.historyKey === key) return;
  memo.historyKey = key;
  list.innerHTML = "";
  if (state.history.length === 0) {
    list.innerHTML = '<li class="meta">Nothing has played yet.</li>';
    return;
  }
  for (const h of entries) {
    const track = {
      id: h.navidrome_id, title: h.title, artist: h.artist,
      duration: h.duration, art_url: h.art_url,
    };
    const li = trackRow(track);
    li.classList.add("history-row");
    if (state.justQueued?.id === h.navidrome_id) li.classList.add("flash");
    if (dj) {
      li.classList.add("queueable");
      li.setAttribute("role", "button");
      li.title = "Add to your queue";
      li.setAttribute("aria-label", `Queue ${h.title} again`);
      const add = document.createElement("button");
      add.type = "button";
      add.className = "icon-btn";
      add.tabIndex = -1;
      add.innerHTML = ICONS.plus;
      li.appendChild(add);
    }
    li.addEventListener("click", () => queueTrack(track, li));
    list.appendChild(li);
  }
  if (memo.historyPage !== state.historyPage) list.scrollTop = 0;
  memo.historyPage = state.historyPage;
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
    if (state.justQueued?.id === track.id) li.classList.add("flash");
    const duration = document.createElement("span");
    duration.className = "track-duration";
    duration.textContent = Number.isFinite(track.duration) && track.duration > 0
      ? formatTime(track.duration) : "—";
    duration.title = "Track duration";
    li.appendChild(duration);
    li.style.cursor = "pointer";
    li.addEventListener("click", () => queueTrack(track, li));
    list.appendChild(li);
  }
}

const LIBRARY_VIEWS = [
  ["alphabeticalByName", "All albums"], ["alphabeticalByArtist", "By artist"],
  ["songs", "Songs"],
  ["random", "Random"], ["starred", "Favourites"], ["highest", "Top rated"],
  ["newest", "Recently added"], ["recent", "Recently played"], ["frequent", "Most played"],
];

// A library track row: title + art (via trackRow) plus a duration and a
// queue affordance. Shared by album detail and the flat Songs list.
function libraryTrackRow(track) {
  const row = trackRow(track);
  if (state.justQueued?.id === track.id) row.classList.add("flash");
  const duration = document.createElement("span");
  duration.className = "track-duration";
  duration.textContent = track.duration > 0 ? formatTime(track.duration) : "—";
  const hint = document.createElement("span");
  hint.className = "track-add-hint";
  const queued = myQueue().some(t => t.id === track.id);
  if (isDj()) {
    row.classList.add("queueable");
    hint.textContent = queued ? "✓ In queue" : "+ Queue";
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `Queue ${track.title}`);
  } else {
    hint.textContent = queued ? "✓ In queue" : "";
  }
  // Wired either way: queueTrack refuses a non-DJ visibly instead of the click
  // landing on nothing at all.
  row.addEventListener("click", () => queueTrack(track, row));
  row.append(duration, hint);
  return row;
}

function renderLibrary() {
  const lib = state.library;
  const browsing = state.view === "library";
  $("app").classList.toggle("library-view", browsing);
  $("library-panel").hidden = !browsing;
  $("room-tab").setAttribute("aria-pressed", String(!browsing));
  $("library-tab").setAttribute("aria-pressed", String(browsing));
  $("library-query").value = state.libraryDraft;
  // Keep album nodes stable during playback ticks, including keyboard focus.
  const key = JSON.stringify([lib, isDj(), myQueue().map(t => t.id), state.justQueued?.id]);
  if (memo.library === key) return;
  memo.library = key;
  const mode = lib.album ? "albumDetail"
    : lib.kind === "songs" ? "songList"
    : (lib.kind === "alphabeticalByArtist" && !lib.query && !lib.artist) ? "artistList"
    : "albumGrid";
  // Only the plain album grid and the flat song list paginate - not an artist's
  // discography, not search results.
  const paged = (mode === "albumGrid" && !lib.artist) || mode === "songList";

  $("library-query").placeholder = mode === "songList" ? "Search songs…" : "Search albums…";

  $("library-filters").replaceChildren(...LIBRARY_VIEWS.map(([kind, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("aria-pressed", String(lib.kind === kind && !lib.query));
    button.addEventListener("click", () =>
      kind === "alphabeticalByArtist" ? showArtists()
        : kind === "songs" ? loadSongs({ query: "", offset: 0 })
        : loadAlbums({ kind, query: "", offset: 0 }));
    return button;
  }));

  let status = "";
  if (lib.loading) status = "Loading…";
  else if (mode === "albumDetail")
    status = isDj() ? "Choose songs to add to your queue." : "Step up in the Radio room to queue songs.";
  else if (mode === "songList")
    status = !lib.songs.length ? (lib.loaded ? "No songs found." : "")
      : lib.query ? `${lib.songs.length} song${lib.songs.length === 1 ? "" : "s"} matching “${lib.query}”`
      : `Songs ${lib.offset + 1}–${lib.offset + lib.songs.length}`;
  else if (mode === "artistList")
    status = lib.artists.length
      ? `${lib.artists.length} artists · ${lib.stats ? lib.stats.albumCount : "…"} albums`
      : lib.loaded ? "No artists found." : "";
  else if (lib.artist)
    status = `${lib.artist.name || "Artist"} · ${lib.albums.length} ${lib.albums.length === 1 ? "album" : "albums"}`;
  else if (!lib.loaded) status = "";
  else if (!lib.albums.length) status = "No albums found.";
  else if (lib.query)
    status = `${lib.albums.length} album${lib.albums.length === 1 ? "" : "s"} matching “${lib.query}”`;
  else
    status = `Albums ${lib.offset + 1}–${lib.offset + lib.albums.length}${lib.stats ? ` of ${lib.stats.albumCount}` : ""}`;
  $("library-status").textContent = status;
  $("library-error").textContent = lib.error;

  $("library-back").hidden = !(lib.album || lib.artist);
  $("library-back").textContent = lib.album ? "← Back to albums" : "← All artists";
  $("album-grid").hidden = mode !== "albumGrid";
  $("artist-list").hidden = mode !== "artistList";
  $("song-list").hidden = mode !== "songList";
  $("library-prev").disabled = !paged || lib.loading || lib.offset === 0 || lib.kind === "random" && !lib.query;
  $("library-next").disabled = !paged || lib.loading || lib.kind === "random" && !lib.query
    || (mode === "songList" ? lib.songs.length < 48
        : lib.stats && !lib.query ? lib.offset + 48 >= lib.stats.albumCount : lib.albums.length < 48);
  $("library-refresh").disabled = lib.loading;
  $("library-page").textContent = paged && !lib.query ? `Page ${lib.offset / 48 + 1}` : "";

  $("artist-list").replaceChildren(...lib.artists.map(a => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "artist-row";
    const name = document.createElement("span");
    name.className = "artist-name";
    name.textContent = a.name;
    const count = document.createElement("span");
    count.className = "artist-count";
    count.textContent = `${a.album_count} ${a.album_count === 1 ? "album" : "albums"}`;
    button.append(name, count);
    button.addEventListener("click", () => loadArtist(a.id, a.name));
    return button;
  }));

  $("album-grid").replaceChildren(...lib.albums.map(album => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "album-card";
    button.title = `${album.name} — ${album.artist}`;
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    setArt(img, album.art_url);
    const title = document.createElement("strong");
    title.textContent = album.name;
    const artist = document.createElement("span");
    artist.textContent = album.artist;
    const meta = document.createElement("small");
    meta.textContent = [album.year, `${album.song_count} tracks`].filter(Boolean).join(" · ");
    button.append(img, title, artist, meta);
    button.addEventListener("click", () => loadAlbum(album.id));
    return button;
  }));
  $("song-list").replaceChildren(...lib.songs.map(libraryTrackRow));

  const detail = $("album-detail");
  detail.replaceChildren();
  if (!lib.album) return;
  const album = lib.album;
  const header = document.createElement("div");
  header.className = "album-header";
  const art = document.createElement("img");
  art.alt = "";
  setArt(art, album.art_url);
  const info = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = album.name;
  const meta = document.createElement("p");
  meta.textContent = [album.artist, album.year, `${album.tracks.length} tracks`, album.duration > 0 ? formatTime(album.duration) : ""].filter(Boolean).join(" · ");
  info.append(title, meta);
  header.append(art, info);
  const tracks = document.createElement("ol");
  tracks.className = "album-tracks";
  for (const track of album.tracks) tracks.appendChild(libraryTrackRow(track));
  detail.append(header, tracks);
}

// A request token prevents a slow response from replacing a newer selection.
let libraryRequest = 0;
async function libraryFetch(url, patch, merge) {
  const request = ++libraryRequest;
  setState({ library: { ...state.library, ...patch, loading: true, error: "" } });
  try {
    const response = await fetch(url);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Library unavailable. Please try again.");
    if (request !== libraryRequest) return;
    setState({ library: { ...state.library, loading: false, loaded: true, ...merge(body) } });
  } catch (error) {
    if (request !== libraryRequest) return;
    setState({ library: { ...state.library, loading: false, error: error.message } });
  }
}

function loadAlbums(patch = {}) {
  const lib = { ...state.library, ...patch };
  if (patch.query !== undefined) setState({ libraryDraft: patch.query });
  return libraryFetch(`/api/albums?${new URLSearchParams({ kind: lib.kind, q: lib.query, offset: lib.offset })}`,
    { ...patch, album: null, artist: null, albums: [] }, body => ({ albums: body }));
}

function loadSongs(patch = {}) {
  const lib = { ...state.library, kind: "songs", ...patch };
  if (patch.query !== undefined) setState({ libraryDraft: patch.query });
  return libraryFetch(`/api/songs?${new URLSearchParams({ q: lib.query, offset: lib.offset })}`,
    { ...patch, kind: "songs", album: null, artist: null, songs: [] }, body => ({ songs: body }));
}

function loadAlbum(id) {
  return libraryFetch(`/api/album?${new URLSearchParams({ id })}`, {}, body => ({ album: body }));
}

function loadArtist(id, name = "") {
  return libraryFetch(`/api/artist?${new URLSearchParams({ id })}`,
    { artist: { id, name }, album: null, albums: [] },
    body => ({ artist: { id: body.id, name: body.name }, albums: body.albums }));
}

// The artist index is also the only cheap source of library-wide totals, so it's
// fetched on entry (interactive=false, totals only) and again when "By artist" is
// opened without a cached copy (interactive=true, drives the loading state).
async function fetchArtists(interactive) {
  const request = interactive ? ++libraryRequest : libraryRequest;
  try {
    const response = await fetch("/api/artists");
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Library unavailable. Please try again.");
    if (interactive && request !== libraryRequest) return;
    setState({ library: { ...state.library, loaded: true, artists: body.artists,
      stats: { albumCount: body.album_count, artistCount: body.artist_count },
      ...(interactive ? { loading: false } : {}) } });
  } catch (error) {
    if (interactive && request === libraryRequest)
      setState({ library: { ...state.library, loading: false, error: error.message } });
  }
}

function showArtists() {
  const cached = state.library.artists.length > 0;
  ++libraryRequest;
  setState({
    libraryDraft: "",
    library: { ...state.library, kind: "alphabeticalByArtist", query: "", offset: 0,
      artist: null, album: null, albums: [], loading: !cached, error: "" },
  });
  if (!cached) fetchArtists(true);
}

function renderVotes() {
  $("skip-count").textContent = state.votes.skip;
  $("skip-needed").textContent = state.votes.skipNeeded;
  $("like-count").textContent = state.votes.like;
  const fav = $("fav-btn");
  fav.classList.toggle("active", state.favorited);
  fav.setAttribute("aria-pressed", state.favorited ? "true" : "false");
}

// Fire-and-forget actions (queueing a track) get no echo of their own from the
// server, so this is their acknowledgement. The dismiss timer lives outside
// render() - render only mirrors whether state.toast is currently set, and keeps
// the last text through the fade-out so it doesn't blank mid-transition.
let toastTimer = null;
function renderToast() {
  const el = $("toast");
  if (state.toast) {
    el.textContent = state.toast.text;
    // Kept through the fade-out, like the text, so it can't flip colour mid-transition.
    el.classList.toggle("deny", state.toast.kind === "deny");
  }
  el.classList.toggle("show", Boolean(state.toast));
}
function showToast(text, kind = "ok") {
  setState({ toast: { text, kind } });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => setState({ toast: null }), 2400);
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
        history: msg.play_history || [],
        votes: {
          skip: msg.skip_votes,
          skipNeeded: msg.skip_votes_needed,
          like: msg.like_votes,
        },
        favorited: !!msg.favorited,
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

// Queueing is DJ-only. Refuse a non-DJ click visibly here rather than letting it
// reach a server that silently drops it - that read as a successful add.
function denyQueue(rowEl) {
  showToast("Only DJs can queue - step up in the DJ booth", "deny");
  if (!rowEl) return;
  rowEl.classList.remove("deny");
  void rowEl.offsetWidth;  // restart the animation when the same row is clicked again
  rowEl.classList.add("deny");
  setTimeout(() => rowEl.classList.remove("deny"), 600);
}

let justQueuedTimer = null;
function queueTrack(track, rowEl) {
  if (!isDj()) return denyQueue(rowEl);
  send({ type: "queue_track", ...track });
  showToast(`Added “${track.title}” to your queue`);
  if (rowEl) flyFromRow(rowEl, track);
  // State-driven so the flash survives the list rebuild the server response
  // triggers; the fly-up above is a detached node and needs no such help.
  setState({ justQueued: { id: track.id } });
  clearTimeout(justQueuedTimer);
  justQueuedTimer = setTimeout(() => setState({ justQueued: null }), 600);
}

// Presentational flourish, side-effect only - driven by an action, never by
// render(). Reads nothing, writes nothing back to state (same rationale as the
// audio module): a label detached onto <body> that flies up from the clicked
// row so the click has a visible origin and direction.
function flyFromRow(rowEl, track) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const rect = rowEl.getBoundingClientRect();
  const fly = document.createElement("div");
  fly.className = "queue-fly";
  fly.textContent = `＋ ${track.title}`;
  fly.style.left = `${rect.left + 44}px`;
  fly.style.top = `${rect.top + 8}px`;
  fly.style.maxWidth = `${Math.max(80, rect.width - 140)}px`;
  document.body.appendChild(fly);
  requestAnimationFrame(() => {
    // Up and slightly toward the queue rail on the right.
    fly.style.transform = "translate(24px, -56px)";
    fly.style.opacity = "0";
  });
  setTimeout(() => fly.remove(), 650);
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

function audioIsAudible() {
  // "Sound is reaching the listener" - all three have to be true. A blocked
  // autoplay leaves the element paused with muted still false, so checking
  // `muted` alone would show "on" while nothing is playing.
  return !radioAudio.paused && !radioAudio.muted && radioAudio.volume > 0;
}

function syncMuteIcon() {
  const audible = audioIsAudible();
  muteBtn.innerHTML = audible ? ICONS.volumeHigh : ICONS.volumeOff;
  muteBtn.setAttribute("aria-label", audible ? "Mute" : "Unmute");
}

// The icon follows the element's own events rather than our assumptions, so it can
// never show "on" while the browser has actually left the stream silent - whether
// that is an explicit mute, zero volume, or a play() the browser refused.
for (const event of ["volumechange", "play", "playing", "pause", "waiting", "stalled"]) {
  radioAudio.addEventListener(event, syncMuteIcon);
}
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

$("history-prev").addEventListener("click", () => setState({ historyPage: state.historyPage - 1 }));
$("history-next").addEventListener("click", () => setState({ historyPage: state.historyPage + 1 }));

muteBtn.addEventListener("click", () => {
  if (audioIsAudible()) {
    radioAudio.muted = true;
    return;
  }
  // Whatever silent state we are in - muted, zeroed volume, or a paused stream
  // the browser blocked - one click of a real user gesture puts sound back. The
  // gesture makes an audible play() allowed, so this never needs a second click.
  radioAudio.muted = false;
  if (radioAudio.volume === 0) {
    radioAudio.volume = 1;
    volumeSlider.value = "1";
  }
  ensurePlaying();
});

volumeSlider.addEventListener("input", () => {
  radioAudio.volume = parseFloat(volumeSlider.value);
  // Dragging the slider up is an unambiguous "I want sound".
  if (radioAudio.volume > 0) radioAudio.muted = false;
  ensurePlaying();
});

$("room-tab").addEventListener("click", () => setState({ view: "room" }));
$("nav-now-playing").addEventListener("click", () => setState({ view: "room" }));
$("library-tab").addEventListener("click", () => {
  setState({ view: "library" });
  if (!state.library.loaded && !state.library.loading) loadAlbums();
  if (!state.library.stats) fetchArtists(false);
});
$("library-query").addEventListener("input", event => setState({ libraryDraft: event.target.value }));
$("library-search").addEventListener("submit", event => {
  event.preventDefault();
  const patch = { query: $("library-query").value.trim(), offset: 0 };
  (state.library.kind === "songs" ? loadSongs : loadAlbums)(patch);
});
const libraryPager = () => state.library.kind === "songs" ? loadSongs : loadAlbums;
$("library-prev").addEventListener("click", () => libraryPager()({ offset: Math.max(0, state.library.offset - 48) }));
$("library-next").addEventListener("click", () => libraryPager()({ offset: state.library.offset + 48 }));
$("library-refresh").addEventListener("click", () => {
  const lib = state.library;
  if (lib.album) loadAlbum(lib.album.id);
  else if (lib.artist) loadArtist(lib.artist.id, lib.artist.name);
  else if (lib.kind === "songs") loadSongs();
  else if (lib.kind === "alphabeticalByArtist" && !lib.query) {
    setState({ library: { ...lib, loading: true, error: "" } });
    fetchArtists(true);
  } else loadAlbums();
});
$("library-back").addEventListener("click", () => {
  ++libraryRequest;
  const lib = state.library;
  setState({ library: lib.album
    ? { ...lib, album: null, loading: false, error: "" }
    : { ...lib, artist: null, albums: [], loading: false, error: "" } });
});

$("dj-toggle").addEventListener("click", () => send({ type: isDj() ? "step_down" : "step_up" }));
$("skip-btn").addEventListener("click", () => send({ type: "vote_skip" }));
$("like-btn").addEventListener("click", () => send({ type: "vote_like" }));
$("fav-btn").addEventListener("click", () => send({ type: "toggle_favorite" }));
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

buildThemeMenu();

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
