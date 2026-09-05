let ws = null;
let myId = null;
let isDj = false;

const PLACEHOLDER_ART = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">' +
  '<rect width="24" height="24" fill="#262834"/>' +
  '<path d="M12 3V13.55C11.41 13.21 10.73 13 10 13C7.79 13 6 14.79 6 17S7.79 21 10 21 14 19.21 14 17V7H18V3H12Z" fill="#8b8fa3"/>' +
  '</svg>'
);

function setArt(imgEl, url) {
  imgEl.onerror = () => {
    imgEl.onerror = null;
    imgEl.src = PLACEHOLDER_ART;
  };
  imgEl.src = url || PLACEHOLDER_ART;
}

const joinOverlay = document.getElementById("join-overlay");
const joinBox = document.getElementById("join-box");
const appEl = document.getElementById("app");

const INVITE_KEY = "tsdfm_invite";
const CLIENT_ID_KEY = "tsdfm_client_id";
const NAME_KEY = "tsdfm_name";
const AVATAR_KEY = "tsdfm_avatar";

const AVATAR_EMOJI = [
  "😀", "😎", "🤠", "🥳", "🤖", "👽", "🐶", "🐱",
  "🦊", "🐼", "🐵", "🦁", "🐸", "🐙", "🦄", "🐧",
  "🍕", "🎧", "🎸", "⚡", "🔥", "🌟", "👑", "💀",
];

function renderJoinBox(innerHtml) {
  joinBox.innerHTML = `<strong>tsdfm</strong>${innerHtml}`;
}

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

function getClientId() {
  let id = localStorage.getItem(CLIENT_ID_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(CLIENT_ID_KEY, id);
  }
  return id;
}

const myInvite = resolveInvite();
const myClientId = getClientId();
let myName = localStorage.getItem(NAME_KEY) || "";
let myAvatar = localStorage.getItem(AVATAR_KEY) || "";

if (!myInvite) {
  renderJoinBox('<p id="join-message">You need an invite link to join - ask the host for one.</p>');
} else if (myName) {
  connect();
} else {
  showNamePrompt();
}

function avatarGridHtml(selected) {
  return AVATAR_EMOJI.map(
    (emoji) => `<button type="button" class="avatar-choice${emoji === selected ? " selected" : ""}" data-emoji="${emoji}">${emoji}</button>`
  ).join("");
}

function wireAvatarGrid(container, onSelect) {
  container.querySelectorAll(".avatar-choice").forEach((btn) => {
    btn.addEventListener("click", () => {
      container.querySelectorAll(".avatar-choice").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      onSelect(btn.dataset.emoji);
    });
  });
}

let selectedAvatar = AVATAR_EMOJI[Math.floor(Math.random() * AVATAR_EMOJI.length)];

function showNamePrompt() {
  renderJoinBox(`
    <div class="avatar-grid" id="join-avatar-grid">${avatarGridHtml(selectedAvatar)}</div>
    <input id="name-input" placeholder="Your name" maxlength="30" />
    <button id="name-submit" type="button">Join</button>
  `);
  wireAvatarGrid(document.getElementById("join-avatar-grid"), (emoji) => { selectedAvatar = emoji; });
  document.getElementById("name-submit").addEventListener("click", submitName);
  document.getElementById("name-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitName();
  });
}

function submitName() {
  const name = document.getElementById("name-input").value.trim();
  if (!name) return;
  myName = name;
  myAvatar = selectedAvatar;
  localStorage.setItem(NAME_KEY, myName);
  localStorage.setItem(AVATAR_KEY, myAvatar);
  tryAutoplay();
  connect();
}

function renderProfile() {
  document.getElementById("profile-avatar").textContent = myAvatar || "🙂";
  document.getElementById("profile-name").textContent = myName;
}

function showProfileEdit() {
  document.getElementById("profile-display").style.display = "none";
  const form = document.getElementById("profile-edit-form");
  form.style.display = "flex";

  let editAvatar = myAvatar;
  const grid = document.getElementById("profile-avatar-grid");
  grid.innerHTML = avatarGridHtml(editAvatar);
  wireAvatarGrid(grid, (emoji) => { editAvatar = emoji; });

  const nameInput = document.getElementById("profile-name-input");
  nameInput.value = myName;

  document.getElementById("profile-save-btn").onclick = () => {
    const name = nameInput.value.trim();
    if (!name) return;
    myName = name;
    myAvatar = editAvatar;
    localStorage.setItem(NAME_KEY, myName);
    localStorage.setItem(AVATAR_KEY, myAvatar);
    // Already connected - push the update over the live socket instead of
    // reconnecting. The server treats a re-join with the same client_id as
    // an update, not a fresh join.
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "join", invite: myInvite, client_id: myClientId, name: myName, avatar: myAvatar }));
    }
    renderProfile();
    form.style.display = "none";
    document.getElementById("profile-display").style.display = "flex";
  };
  document.getElementById("profile-cancel-btn").onclick = () => {
    form.style.display = "none";
    document.getElementById("profile-display").style.display = "flex";
  };
}

document.getElementById("profile-edit-btn").addEventListener("click", showProfileEdit);

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ type: "join", invite: myInvite, client_id: myClientId, name: myName, avatar: myAvatar }));
  });

  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "you") {
      myId = msg.id;
    } else if (msg.type === "state") {
      handleState(msg);
    } else if (msg.type === "chat") {
      appendChat(msg);
    } else if (msg.type === "log") {
      appendLog(msg);
    } else if (msg.type === "error") {
      alert(msg.message);
    }
  });

  ws.addEventListener("close", () => {
    appEl.classList.remove("visible");
    renderJoinBox('<p id="join-message">Disconnected - reconnecting...</p>');
    joinOverlay.style.display = "flex";
    setTimeout(connect, 2000);
  });
}

function handleState(state) {
  if (!appEl.classList.contains("visible")) {
    joinOverlay.style.display = "none";
    appEl.classList.add("visible");
  }

  renderProfile();
  renderNowPlaying(state.now_playing);
  renderDjList(state.dj_order);
  renderListeners(state.users);
  renderChatHistory(state.chat_history);

  document.getElementById("skip-count").textContent = state.skip_votes;
  document.getElementById("skip-needed").textContent = state.skip_votes_needed;
  document.getElementById("like-count").textContent = state.like_votes;
}

let currentNowPlaying = null;

function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function updatePosition() {
  const posEl = document.getElementById("np-position");
  const fillEl = document.getElementById("np-progress-fill");
  if (!currentNowPlaying) {
    posEl.textContent = "";
    fillEl.style.width = "0%";
    return;
  }
  const elapsed = Math.min(currentNowPlaying.duration, Date.now() / 1000 - currentNowPlaying.started_at);
  posEl.textContent = `${formatTime(elapsed)} / ${formatTime(currentNowPlaying.duration)}`;
  const pct = currentNowPlaying.duration > 0 ? (elapsed / currentNowPlaying.duration) * 100 : 0;
  fillEl.style.width = `${Math.min(100, pct)}%`;
}

setInterval(updatePosition, 1000);

function renderNowPlaying(np) {
  const title = document.getElementById("np-title");
  const meta = document.getElementById("np-meta");
  const art = document.getElementById("np-art");
  const liveDot = document.getElementById("live-dot");
  currentNowPlaying = np;
  if (!np) {
    title.textContent = "Nothing playing";
    meta.textContent = "Step up to DJ and queue a track to get things started.";
    art.style.display = "none";
    liveDot.style.display = "none";
    updatePosition();
    return;
  }
  title.textContent = np.title;
  meta.textContent = `${np.artist} — spun by ${np.dj_name}`;
  art.style.display = "block";
  liveDot.style.display = "inline-block";
  setArt(art, np.art_url);
  updatePosition();
}

let latestDjOrder = [];

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

// State is fully re-rendered on every "state" message (which fires on lots of
// unrelated events - a vote, someone else joining, etc.), not just when a track
// is added. Tracking previous lengths lets us animate only genuinely new rows
// (appended past the old length) instead of replaying the entrance for everything.
let prevDjQueueLengths = {};
let prevMyQueueLength = 0;

function renderDjList(djOrder) {
  latestDjOrder = djOrder;
  const list = document.getElementById("dj-list");
  list.innerHTML = "";
  isDj = djOrder.some((d) => d.id === myId);

  if (djOrder.length === 0) {
    list.innerHTML = '<li class="meta">No one has stepped up to DJ yet.</li>';
  }

  const newLengths = {};
  for (const dj of djOrder) {
    const li = document.createElement("li");
    li.className = "dj-entry";

    const header = document.createElement("div");
    header.className = "dj-name";
    header.textContent = `${dj.avatar} ${dj.name}`;
    li.appendChild(header);

    if (dj.queue.length === 0) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "— empty";
      li.appendChild(empty);
    } else {
      const prevLen = prevDjQueueLengths[dj.id] || 0;
      const trackList = document.createElement("ul");
      trackList.className = "dj-track-list";
      dj.queue.forEach((track, index) => {
        const row = trackRow(track);
        if (index >= prevLen) row.classList.add("enter");
        trackList.appendChild(row);
      });
      li.appendChild(trackList);
    }
    newLengths[dj.id] = dj.queue.length;
    list.appendChild(li);
  }
  prevDjQueueLengths = newLengths;

  const toggleBtn = document.getElementById("dj-toggle");
  toggleBtn.textContent = isDj ? "Step down" : "Step up to DJ";
  document.getElementById("queue-panel").style.display = isDj ? "block" : "none";

  if (isDj) renderMyQueue();
}

function getMyQueue() {
  return latestDjOrder.find((d) => d.id === myId)?.queue || [];
}

function renderMyQueue() {
  const list = document.getElementById("my-queue-list");
  list.innerHTML = "";
  const queue = getMyQueue();
  if (queue.length === 0) {
    list.innerHTML = '<li class="meta">Nothing queued yet - search above and click a track to add it.</li>';
    prevMyQueueLength = 0;
    return;
  }
  queue.forEach((track, index) => {
    const li = trackRow(track);
    if (index >= prevMyQueueLength) li.classList.add("enter");
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "icon-btn";
    removeBtn.innerHTML = ICONS.close;
    removeBtn.addEventListener("click", () => {
      ws.send(JSON.stringify({ type: "remove_track", index }));
    });
    li.appendChild(removeBtn);
    list.appendChild(li);
  });
  prevMyQueueLength = queue.length;
}

function renderListeners(users) {
  document.getElementById("listeners").textContent =
    users.map((u) => `${u.avatar} ${u.name}`).join(", ") || "Just you.";
}

function renderChatHistory(history) {
  const log = document.getElementById("chat-log");
  log.innerHTML = "";
  for (const msg of history) appendChat(msg, false);
}

function appendChat(msg) {
  const log = document.getElementById("chat-log");
  const li = document.createElement("li");
  li.className = "enter";
  const avatar = msg.avatar ? `${msg.avatar} ` : "";
  li.innerHTML = `<span class="user">${avatar}${escapeHtml(msg.user)}</span>${escapeHtml(msg.text)}`;
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
}

function appendLog(entry) {
  const panel = document.getElementById("log-panel");
  const li = document.createElement("li");
  li.className = `level-${entry.level} enter`;
  const time = new Date(entry.ts * 1000).toLocaleTimeString();
  li.innerHTML = `<span class="log-time">${time}</span>${escapeHtml(entry.message)}`;
  panel.appendChild(li);
  panel.scrollTop = panel.scrollHeight;
}

async function loadLogHistory() {
  try {
    const resp = await fetch("/api/logs");
    const entries = await resp.json();
    for (const entry of entries) appendLog(entry);
  } catch (err) {
    // best-effort; the panel just stays empty if this fails
  }
}
loadLogHistory();

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// Inline MDI (pictogrammers.com/library/mdi) icons - kept as raw SVG rather
// than pulling in an icon font/CDN, so the app has no external asset deps.
const ICONS = {
  volumeOff: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M12,4L9.91,6.09L12,8.18M4.27,3L3,4.27L7.73,9H3V15H7L12,20V13.27L16.25,17.53C15.58,18.04 14.83,18.46 14,18.7V20.77C15.38,20.45 16.63,19.82 17.68,18.96L19.73,21L21,19.73L12,10.73M19,12C19,12.94 18.8,13.82 18.46,14.64L19.97,16.15C20.62,14.91 21,13.5 21,12C21,7.72 18,4.14 14,3.23V5.29C16.89,6.15 19,8.83 19,12M16.5,12C16.5,10.23 15.5,8.71 14,7.97V10.18L16.45,12.63C16.5,12.43 16.5,12.21 16.5,12Z" /></svg>',
  volumeHigh: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z" /></svg>',
  close: '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z" /></svg>',
};

const radioAudio = document.getElementById("radio-audio");
const muteBtn = document.getElementById("mute-btn");
const volumeSlider = document.getElementById("volume-slider");

function tryAutoplay() {
  // It's a continuous live broadcast, not a seekable file - there's no real
  // "pause", so playback always runs and the control just mutes/unmutes it.
  // Starting muted is always allowed by browser autoplay policy, unlike
  // starting with sound, so this reliably plays without needing a click first.
  radioAudio.muted = true;
  radioAudio.play().catch(() => {});
}

muteBtn.innerHTML = ICONS.volumeOff;
tryAutoplay();
document.addEventListener("click", tryAutoplay, { once: true });
document.addEventListener("keydown", tryAutoplay, { once: true });

muteBtn.addEventListener("click", () => {
  radioAudio.muted = !radioAudio.muted;
  muteBtn.innerHTML = radioAudio.muted ? ICONS.volumeOff : ICONS.volumeHigh;
});

volumeSlider.addEventListener("input", () => {
  radioAudio.volume = parseFloat(volumeSlider.value);
});

document.getElementById("dj-toggle").addEventListener("click", () => {
  ws.send(JSON.stringify({ type: isDj ? "step_down" : "step_up" }));
});

document.getElementById("skip-btn").addEventListener("click", () => {
  ws.send(JSON.stringify({ type: "vote_skip" }));
});

document.getElementById("like-btn").addEventListener("click", () => {
  ws.send(JSON.stringify({ type: "vote_like" }));
});

document.getElementById("chat-send").addEventListener("click", sendChat);
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});

function sendChat() {
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;
  ws.send(JSON.stringify({ type: "chat", text }));
  input.value = "";
}

document.getElementById("search-btn").addEventListener("click", runSearch);
document.getElementById("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});

async function runSearch() {
  const q = document.getElementById("search-input").value.trim();
  if (!q) return;
  const list = document.getElementById("search-results");
  const errorEl = document.getElementById("search-error");
  const statusEl = document.getElementById("search-status");
  const searchBtn = document.getElementById("search-btn");

  errorEl.textContent = "";
  list.innerHTML = "";
  statusEl.textContent = "Searching...";
  searchBtn.disabled = true;
  searchBtn.textContent = "Searching...";

  let resp, body;
  try {
    resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    body = await resp.json();
  } catch (err) {
    errorEl.textContent = `Search request failed: ${err.message}`;
    return;
  } finally {
    statusEl.textContent = "";
    searchBtn.disabled = false;
    searchBtn.textContent = "Search";
  }

  if (!resp.ok) {
    errorEl.textContent = body.error || `Search failed (${resp.status})`;
    return;
  }

  const results = body;
  if (results.length === 0) {
    statusEl.textContent = "No matches.";
    return;
  }
  for (const track of results) {
    const li = trackRow(track);
    li.style.cursor = "pointer";
    li.addEventListener("click", () => {
      ws.send(JSON.stringify({ type: "queue_track", ...track }));
      statusEl.textContent = `Added "${track.title}" to your queue.`;
    });
    list.appendChild(li);
  }
}
