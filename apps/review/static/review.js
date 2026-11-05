// SPDX-License-Identifier: AGPL-3.0-or-later
// The local review page: gate queue, frame-accurate player with synced audio,
// approve / request changes, and notes pinned to frames. Review only: productions
// are started and steered from the CLI and Claude Code (MCP), never from here.

const $ = (id) => document.getElementById(id);
const video = $("video");
const still = $("still");
const audio = $("audio");

const state = { item: null, media: null, frame: 0, frames: 0, playing: false };

/** Timecode HH:MM:SS:FF of a zero-based frame at `fps` (non-drop). */
export function timecode(frame, fps) {
  const rate = Math.max(1, Math.round(fps));
  const ff = frame % rate;
  const total = Math.floor(frame / rate);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}:${pad(ff)}`;
}

function setStatus(text) {
  $("status").textContent = text;
}

async function api(path, init) {
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function nodePath(item) {
  return `/v1/review/productions/${encodeURIComponent(item.production)}/nodes/${encodeURIComponent(item.node)}`;
}

// ---- queue ------------------------------------------------------------------

async function loadQueue() {
  const data = await api("/v1/review/queue");
  const list = $("queue");
  list.replaceChildren();
  for (const item of data.items) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.dataset.testid = "queue-item";
    btn.dataset.production = item.production;
    btn.dataset.node = item.node;
    btn.textContent = item.node;
    const small = document.createElement("small");
    small.textContent = `${item.production} · ${item.kind} · ${item.department}`;
    btn.append(small);
    if (state.item && state.item.production === item.production && state.item.node === item.node) {
      btn.classList.add("selected");
    }
    btn.addEventListener("click", () => select(item));
    li.append(btn);
    list.append(li);
  }
  $("queue-empty").hidden = data.items.length > 0;
  return data.items;
}

// ---- player -----------------------------------------------------------------

function fps() {
  return state.media ? state.media.fps : 24;
}

function showFrame() {
  $("frame").textContent = String(state.frame);
  $("timecode").textContent = timecode(state.frame, fps());
}

function frameAt(time) {
  return Math.min(Math.max(0, Math.floor(time * fps() + 1e-6)), Math.max(0, state.frames - 1));
}

function syncAudio(force) {
  if (!state.media || !state.media.audio) return;
  const drift = Math.abs(audio.currentTime - video.currentTime);
  if (force || drift > 1 / fps()) audio.currentTime = video.currentTime;
}

function seekFrame(frame) {
  if (!state.media || video.hidden) return;
  const last = Math.max(0, state.frames - 1);
  state.frame = Math.min(Math.max(0, frame), last);
  // Aim at the middle of the frame so rounding never lands on its neighbour.
  video.currentTime = (state.frame + 0.5) / fps();
  syncAudio(true);
  showFrame();
}

function pause() {
  video.pause();
  if (state.media && state.media.audio) audio.pause();
  state.playing = false;
  $("play").textContent = "Play";
  seekFrame(frameAt(video.currentTime));
}

async function play() {
  if (!state.media || video.hidden) return;
  syncAudio(true);
  state.playing = true;
  $("play").textContent = "Pause";
  const starts = [video.play()];
  if (state.media.audio) starts.push(audio.play());
  await Promise.all(starts).catch((err) => setStatus(`playback: ${err.message}`));
  tick();
}

function tick() {
  if (!state.playing) return;
  state.frame = frameAt(video.currentTime);
  showFrame();
  syncAudio(false);
  if (typeof video.requestVideoFrameCallback === "function") {
    video.requestVideoFrameCallback(() => tick());
  } else {
    requestAnimationFrame(() => tick());
  }
}

video.addEventListener("ended", () => pause());

function loadMedia(media) {
  state.media = media;
  state.frame = 0;
  state.frames = 0;
  video.hidden = true;
  still.hidden = true;
  $("no-media").hidden = Boolean(media);
  audio.removeAttribute("src");
  if (!media) {
    $("no-media").textContent = "This node has no media to play.";
    $("fps").textContent = "";
    showFrame();
    return;
  }
  $("fps").textContent = `${media.fps} fps`;
  const isStill = /\.(png|jpe?g|webp|gif)$/i.test(media.video);
  if (isStill) {
    still.src = media.video;
    still.hidden = false;
    state.frames = 1;
    showFrame();
    return;
  }
  video.muted = Boolean(media.audio);
  video.src = media.video;
  if (media.audio) audio.src = media.audio;
  video.hidden = false;
  video.addEventListener(
    "loadedmetadata",
    () => {
      state.frames = Math.max(1, Math.round(video.duration * fps()));
      video.dataset.frames = String(state.frames);
      seekFrame(0);
      video.dataset.ready = "1";
    },
    { once: true },
  );
}

// ---- node, decisions and notes ---------------------------------------------

function renderNotes(notes) {
  const list = $("notes");
  list.replaceChildren();
  for (const note of notes) {
    const li = document.createElement("li");
    li.dataset.testid = "note";
    const where = document.createElement("span");
    where.className = "tc";
    where.textContent = note.frame === null ? "whole node" : `${note.timecode || timecode(note.frame, fps())} (f${note.frame})`;
    li.append(where, document.createTextNode(` ${note.text}`));
    if (note.frame !== null) li.addEventListener("click", () => seekFrame(note.frame));
    list.append(li);
  }
}

async function select(item) {
  state.item = item;
  delete video.dataset.ready;
  const detail = await api(nodePath(item));
  $("current").textContent = `${item.production} / ${item.node} (${detail.kind}, ${detail.status})`;
  $("output").textContent = JSON.stringify(detail.output, null, 2);
  loadMedia(detail.media);
  renderNotes(detail.notes);
  const awaiting = detail.status === "awaiting_approval";
  $("approve").disabled = !awaiting;
  $("reject").disabled = !awaiting;
  $("add-note").disabled = false;
  setStatus(detail.media_error || "");
  for (const btn of document.querySelectorAll("#queue button")) {
    btn.classList.toggle("selected", btn.dataset.node === item.node && btn.dataset.production === item.production);
  }
}

async function decide(kind) {
  if (!state.item) return;
  const item = state.item;
  const comment = $("decision-comment").value;
  try {
    const res = await post(`${nodePath(item)}/${kind}`, kind === "reject" ? { comment, revise: true } : { comment });
    setStatus(`${item.node}: ${res.status}`);
    $("decision-comment").value = "";
    $("approve").disabled = true;
    $("reject").disabled = true;
    const items = await loadQueue();
    if (items.length) await select(items[0]);
  } catch (err) {
    setStatus(`${kind} failed: ${err.message}`);
  }
}

async function addNote() {
  if (!state.item) return;
  const text = $("note-text").value.trim();
  if (!text) return;
  try {
    const res = await post(`${nodePath(state.item)}/comments`, {
      frame: state.frame,
      timecode: timecode(state.frame, fps()),
      text,
    });
    $("note-text").value = "";
    renderNotes(res.notes);
    setStatus(`note pinned to frame ${state.frame}`);
  } catch (err) {
    setStatus(`note failed: ${err.message}`);
  }
}

$("prev").addEventListener("click", () => (state.playing ? pause() : seekFrame(state.frame - 1)));
$("next").addEventListener("click", () => (state.playing ? pause() : seekFrame(state.frame + 1)));
$("play").addEventListener("click", () => (state.playing ? pause() : play()));
$("approve").addEventListener("click", () => decide("approve"));
$("reject").addEventListener("click", () => decide("reject"));
$("add-note").addEventListener("click", () => addNote());
document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLTextAreaElement) return;
  if (event.key === "ArrowLeft") seekFrame(state.frame - 1);
  else if (event.key === "ArrowRight") seekFrame(state.frame + 1);
  else if (event.key === " ") {
    event.preventDefault();
    if (state.playing) pause();
    else play();
  } else return;
});

loadQueue()
  .then((items) => (items.length ? select(items[0]) : undefined))
  .catch((err) => setStatus(`queue: ${err.message}`));
