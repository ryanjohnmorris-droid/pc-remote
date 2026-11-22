"use strict";
// Synth: a playable synth, drum pads and a step sequencer. Every sound is made live with the Web Audio API,
// so there are no sample files. Your beats and settings are saved on this device.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const NOTE_NAMES = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"];
const noteName = (m) => NOTE_NAMES[m % 12] + (Math.floor(m / 12) - 1);

// ---------- Sounds ----------
// osc: [wave, detune in cents, level]. cutoff/fenv: filter start and how far its envelope opens (Hz).
// a/d/s/r: attack, decay, sustain level, release. fd: filter decay. oct: octave used by the Melody track.
const PRESETS = {
  keys:  { name: "Piano", osc: [["triangle", 0, 1], ["sine", 1200, 0.25], ["sawtooth", 3, 0.1]], cutoff: 900, fenv: 4000, fd: 0.8, q: 0.7, a: 0.004, d: 2.4, s: 0, r: 0.4, oct: 4 },
  pluck: { name: "Pluck", osc: [["sawtooth", 0, 0.6], ["square", -5, 0.3]], cutoff: 250, fenv: 5000, fd: 0.15, q: 2, a: 0.002, d: 0.7, s: 0, r: 0.3, oct: 4 },
  pad:   { name: "Pad", osc: [["sawtooth", -9, 0.4], ["sawtooth", 9, 0.4], ["triangle", -1200, 0.4]], cutoff: 700, fenv: 900, fd: 1.5, q: 1, a: 0.5, d: 1.5, s: 0.8, r: 1.4, oct: 4, level: 1.3 },
  bass:  { name: "Bass", osc: [["sawtooth", 0, 0.5], ["square", -1200, 0.5]], cutoff: 180, fenv: 1400, fd: 0.25, q: 7, a: 0.003, d: 0.3, s: 0.55, r: 0.1, oct: 2, level: 1.1 },
  lead:  { name: "Lead", osc: [["sawtooth", 0, 0.5], ["square", 7, 0.35]], cutoff: 1200, fenv: 3500, fd: 0.4, q: 5, a: 0.01, d: 0.4, s: 0.7, r: 0.2, oct: 4 },
  organ: { name: "Organ", osc: [["sine", 0, 0.5], ["sine", 1200, 0.35], ["sine", 1902, 0.25], ["sine", 2400, 0.18]], cutoff: 7000, fenv: 0, q: 0.5, a: 0.008, d: 0.1, s: 1, r: 0.08, oct: 4 },
  bell:  { name: "Bell", osc: [["sine", 0, 0.9]], fm: { ratio: 3.5, index: 3, decay: 0.5 }, cutoff: 9000, fenv: 0, q: 0.5, a: 0.002, d: 2.2, s: 0, r: 1.2, oct: 5 },
};

const DRUMS = [
  { id: "kick", name: "Kick" }, { id: "snare", name: "Snare" }, { id: "clap", name: "Clap" }, { id: "hat", name: "Hi-hat" },
  { id: "open", name: "Open hat" }, { id: "tomLo", name: "Low tom" }, { id: "tomHi", name: "High tom" }, { id: "cow", name: "Cowbell" },
];

const SCALES = {
  minorPent: { name: "Minor pentatonic", s: [0, 3, 5, 7, 10] },
  majorPent: { name: "Major pentatonic", s: [0, 2, 4, 7, 9] },
  major: { name: "Major", s: [0, 2, 4, 5, 7, 9, 11] },
  minor: { name: "Minor", s: [0, 2, 3, 5, 7, 8, 10] },
  blues: { name: "Blues", s: [0, 3, 5, 6, 7, 10] },
};

// ---------- Saved state ----------
const STORE = "synth.v1";
const STEPS = 16, ROWS = 8;
const emptyGrid = () => Array.from({ length: ROWS }, () => Array(STEPS).fill(0));
const newPattern = () => ({ bpm: 100, swing: 0, drums: emptyGrid(), melody: emptyGrid(), sound: "pluck", root: 0, scale: "minorPent" });

function fixGrid(g) {
  const out = emptyGrid();
  if (Array.isArray(g)) for (let r = 0; r < ROWS; r++) for (let s = 0; s < STEPS; s++) out[r][s] = g[r] && g[r][s] ? 1 : 0;
  return out;
}
function fixPattern(p) {
  const d = newPattern();
  if (!p || typeof p !== "object") return d;
  return {
    bpm: clamp(Math.round(+p.bpm) || d.bpm, 50, 200),
    swing: clamp(+p.swing || 0, 0, 0.6),
    drums: fixGrid(p.drums), melody: fixGrid(p.melody),
    sound: PRESETS[p.sound] ? p.sound : d.sound,
    root: clamp(Math.round(+p.root) || 0, 0, 11),
    scale: SCALES[p.scale] ? p.scale : d.scale,
  };
}

// Starter beats. Drum lines are 16 steps, "x" = hit. Melody rows go from the lowest note (0) up.
const STARTERS = {
  rock: { name: "Rock", bpm: 110, drums: { kick: "x.......x.x.....", snare: "....x.......x...", hat: "x.x.x.x.x.x.x.x." } },
  hiphop: { name: "Hip hop", bpm: 90, swing: 0.3, drums: { kick: "x......x..x.....", snare: "....x.......x...", hat: "x.x.x.x.x.x.x.x.", open: "..............x." } },
  house: { name: "House", bpm: 124, sound: "bass", scale: "minor", root: 9,
    drums: { kick: "x...x...x...x...", clap: "....x.......x...", open: "..x...x...x...x." },
    melody: { 0: "..x...x...x.....", 2: "..............x." } },
  trap: { name: "Trap", bpm: 140, drums: { kick: "x......x..x.....", snare: "........x.......", hat: "x.xxx.x.x.xxx.xx", open: "...........x...." } },
  reggaeton: { name: "Reggaeton", bpm: 95, drums: { kick: "x...x...x...x...", snare: "...x..x....x..x.", hat: "x.x.x.x.x.x.x.x." } },
  lofi: { name: "Lo-fi", bpm: 80, swing: 0.35, sound: "keys", scale: "minorPent", root: 2,
    drums: { kick: "x.....x...x.....", snare: "....x.......x...", hat: "x.x.x.x.x.x.x.x.", cow: "" },
    melody: { 0: "x...............", 2: "......x.........", 3: "........x.......", 4: "..x.........x...", 5: "....x...........", 7: "..........xx...." } },
};
function starter(id) {
  const s = STARTERS[id], p = newPattern();
  p.bpm = s.bpm; p.swing = s.swing || 0;
  if (s.sound) p.sound = s.sound;
  if (s.scale) p.scale = s.scale;
  if (s.root != null) p.root = s.root;
  DRUMS.forEach((d, r) => { const line = s.drums[d.id] || ""; for (let i = 0; i < STEPS; i++) p.drums[r][i] = line[i] === "x" ? 1 : 0; });
  Object.entries(s.melody || {}).forEach(([r, line]) => { for (let i = 0; i < STEPS; i++) p.melody[+r][i] = line[i] === "x" ? 1 : 0; });
  return p;
}

const saved = (() => { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; } })();
const settings = Object.assign({ tab: "keys", preset: "keys", octave: 4, tone: 0.6, reverb: 0.25, echo: 0.1, track: "drums", padRec: false }, saved.settings || {});
if (!PRESETS[settings.preset]) settings.preset = "keys";
settings.octave = clamp(Math.round(+settings.octave) || 4, 1, 7);
let patterns = Array.isArray(saved.patterns) && saved.patterns.length === 4
  ? saved.patterns.map(fixPattern) : [starter("rock"), newPattern(), newPattern(), newPattern()];
let cur = clamp(Math.round(+saved.cur) || 0, 0, 3);
const pat = () => patterns[cur];

let saveTimer;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { try { localStorage.setItem(STORE, JSON.stringify({ settings, patterns, cur })); } catch { /* private mode: keep going */ } }, 300);
}

// ---------- Audio engine ----------
let ctx, synthBus, drumBus, revSend, echoSend, echoDelay, recDest, analyser, noiseBuf;

function ensureAudio() {
  if (ctx) { if (ctx.state !== "running") ctx.resume().catch(() => {}); return ctx; }
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) { toast("This browser can't play sound."); return null; }
  try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch { /* iPhone: plays even with the silent switch on */ }
  ctx = new AC({ latencyHint: "interactive" });

  const master = ctx.createGain(); master.gain.value = 0.9;
  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -12; comp.knee.value = 8; comp.ratio.value = 4; comp.attack.value = 0.003; comp.release.value = 0.2;
  analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
  master.connect(comp); comp.connect(analyser); analyser.connect(ctx.destination);
  if (ctx.createMediaStreamDestination) { recDest = ctx.createMediaStreamDestination(); comp.connect(recDest); }

  // Reverb: a convolver with a made-up room.
  const conv = ctx.createConvolver(); conv.buffer = makeImpulse(2.6, 3);
  revSend = ctx.createGain(); revSend.connect(conv); conv.connect(master);
  // Echo: a dotted-eighth delay that follows the tempo.
  echoSend = ctx.createGain();
  echoDelay = ctx.createDelay(2);
  const fb = ctx.createGain(); fb.gain.value = 0.38;
  const tone = ctx.createBiquadFilter(); tone.type = "lowpass"; tone.frequency.value = 3200;
  echoSend.connect(echoDelay); echoDelay.connect(tone); tone.connect(fb); fb.connect(echoDelay); tone.connect(master);

  synthBus = ctx.createGain(); synthBus.connect(master); synthBus.connect(revSend); synthBus.connect(echoSend);
  drumBus = ctx.createGain(); drumBus.gain.value = 0.9; drumBus.connect(master);
  const room = ctx.createGain(); room.gain.value = 0.12; drumBus.connect(room); room.connect(conv);

  noiseBuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
  const nd = noiseBuf.getChannelData(0);
  for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;

  applyFx();
  ctx.resume().catch(() => {});
  return ctx;
}

function makeImpulse(sec, decay) {
  const len = Math.floor(ctx.sampleRate * sec), buf = ctx.createBuffer(2, len, ctx.sampleRate);
  for (let c = 0; c < 2; c++) {
    const d = buf.getChannelData(c);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / len) ** decay;
  }
  return buf;
}

function applyFx() {
  if (!ctx) return;
  const t = ctx.currentTime;
  revSend.gain.setTargetAtTime(settings.reverb * 0.9, t, 0.05);
  echoSend.gain.setTargetAtTime(settings.echo * 0.8, t, 0.05);
  echoDelay.delayTime.setTargetAtTime(Math.min(1.9, 0.75 * 60 / pat().bpm), t, 0.05);
}

function holdParam(param, t) {
  if (param.cancelAndHoldAtTime) param.cancelAndHoldAtTime(t);
  else { const v = param.value; param.cancelScheduledValues(t); param.setValueAtTime(v, t); }
}

// --- Synth voices ---
const voices = new Set();
const toneFactor = () => 2 ** ((settings.tone - 0.6) * 4);

function noteOn(midi, { t = ctx.currentTime, vel = 0.9, preset = settings.preset } = {}) {
  const p = PRESETS[preset] || PRESETS.keys;
  const f = 440 * 2 ** ((midi - 69) / 12);
  const peak = vel * 0.26 * (p.level || 1);

  const amp = ctx.createGain();
  amp.gain.setValueAtTime(0, t);
  amp.gain.linearRampToValueAtTime(peak, t + p.a);
  amp.gain.setTargetAtTime(peak * p.s, t + p.a, p.d / 3);

  const track = (f / 261.6) ** 0.3, tf = toneFactor();
  const lo = clamp(p.cutoff * tf * track, 40, 18000), hi = clamp((p.cutoff + p.fenv) * tf * track, 40, 18000);
  const flt = ctx.createBiquadFilter(); flt.type = "lowpass"; flt.Q.value = p.q;
  flt.frequency.setValueAtTime(lo, t);
  flt.frequency.linearRampToValueAtTime(hi, t + Math.max(p.a, 0.005));
  flt.frequency.setTargetAtTime(lo + (hi - lo) * p.s * 0.5, t + Math.max(p.a, 0.005), (p.fd || p.d) / 3);
  flt.connect(amp); amp.connect(synthBus);

  const srcs = [];
  for (const [type, cents, g] of p.osc) {
    const o = ctx.createOscillator(); o.type = type; o.frequency.value = f; o.detune.value = cents;
    const og = ctx.createGain(); og.gain.value = g; o.connect(og); og.connect(flt);
    if (p.fm) {
      const m = ctx.createOscillator(); m.frequency.value = f * p.fm.ratio;
      const mg = ctx.createGain();
      mg.gain.setValueAtTime(f * p.fm.index, t); mg.gain.setTargetAtTime(f * p.fm.index * 0.15, t, p.fm.decay);
      m.connect(mg); mg.connect(o.frequency); srcs.push(m);
    }
    srcs.push(o);
  }
  srcs.forEach((s) => s.start(t));
  // Sounds that fade on their own (piano, pluck, bell) stop by themselves.
  if (p.s === 0) srcs.forEach((s) => s.stop(t + p.a + p.d * 2.2 + 0.1));

  const v = { amp, srcs, p, peak, midi, released: false };
  voices.add(v);
  srcs[0].onended = () => { amp.disconnect(); voices.delete(v); };
  if (voices.size > 24) noteOff([...voices].find((x) => !x.released));
  return v;
}

function noteOff(v, t = ctx.currentTime) {
  if (!v || v.released) return;
  v.released = true;
  const g = v.amp.gain;
  if (g.cancelAndHoldAtTime) g.cancelAndHoldAtTime(t);
  else { g.cancelScheduledValues(t); g.setValueAtTime(t <= ctx.currentTime + 0.02 ? g.value : v.peak * v.p.s, t); }
  g.setTargetAtTime(0, t, v.p.r / 4);
  const end = t + v.p.r * 1.6 + 0.05;
  v.srcs.forEach((s) => { try { s.stop(end); } catch { /* already stopped */ } });
}

// --- Drums ---
function env(t, peak, decay) {
  const g = ctx.createGain();
  g.gain.setValueAtTime(peak, t); g.gain.exponentialRampToValueAtTime(0.0001, t + decay);
  g.connect(drumBus); return g;
}
function osc(type, f, t, stop) { const o = ctx.createOscillator(); o.type = type; o.frequency.setValueAtTime(f, t); o.start(t); o.stop(stop); return o; }
function noise(t, stop) { const s = ctx.createBufferSource(); s.buffer = noiseBuf; s.start(t, Math.random() * 0.5); s.stop(stop); return s; }
function filt(type, f, q = 0.7) { const b = ctx.createBiquadFilter(); b.type = type; b.frequency.value = f; b.Q.value = q; return b; }
const done = (src, ...nodes) => { src.onended = () => nodes.forEach((n) => n.disconnect()); };

let openHat = null;
function metal(t, v, decay) {
  const g = env(t, v * 0.5, decay), mix = ctx.createGain(); mix.gain.value = 0.3;
  const bp = filt("bandpass", 10000, 0.8), hp = filt("highpass", 7000);
  mix.connect(bp); bp.connect(hp); hp.connect(g);
  let last;
  for (const r of [2, 3, 4.16, 5.43, 6.79, 8.21]) { last = osc("square", 40 * r, t, t + decay + 0.05); last.connect(mix); }
  done(last, g, mix);
  return g;
}
const DRUM_FX = {
  kick(t, v) {
    const g = env(t, v, 0.5), o = osc("sine", 160, t, t + 0.55);
    o.frequency.exponentialRampToValueAtTime(42, t + 0.13); o.connect(g); done(o, g);
    const c = env(t, v * 0.25, 0.02), n = noise(t, t + 0.03), f = filt("lowpass", 3500);
    n.connect(f); f.connect(c); done(n, c);
  },
  snare(t, v) {
    const g = env(t, v * 0.45, 0.12), o = osc("triangle", 200, t, t + 0.15);
    o.frequency.exponentialRampToValueAtTime(160, t + 0.1); o.connect(g); done(o, g);
    const ng = env(t, v * 0.55, 0.22), n = noise(t, t + 0.25), hp = filt("highpass", 1500);
    n.connect(hp); hp.connect(ng); done(n, ng);
  },
  clap(t, v) {
    const g = ctx.createGain(); g.connect(drumBus);
    g.gain.setValueAtTime(0.0001, t);
    for (let k = 0; k < 3; k++) { const tk = t + k * 0.011; g.gain.setValueAtTime(v * 1.3, tk); g.gain.exponentialRampToValueAtTime(v * 0.15, tk + 0.01); }
    g.gain.setValueAtTime(v * 1.2, t + 0.033); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.26);
    const n = noise(t, t + 0.3), bp = filt("bandpass", 1200, 1.2), hp = filt("highpass", 600);
    n.connect(bp); bp.connect(hp); hp.connect(g); done(n, g);
  },
  hat(t, v) {
    if (openHat) { holdParam(openHat.gain, t); openHat.gain.setTargetAtTime(0.0001, t, 0.008); openHat = null; } // a closed hat cuts the open one
    metal(t, v, 0.06);
  },
  open(t, v) { openHat = metal(t, v * 0.9, 0.38); },
  tomLo(t, v) { tom(t, v, 105); },
  tomHi(t, v) { tom(t, v, 175); },
  cow(t, v) {
    const g = env(t, v * 0.24, 0.35), bp = filt("bandpass", 800, 1.6); bp.connect(g);
    const a = osc("square", 540, t, t + 0.4), b = osc("square", 800, t, t + 0.4); a.connect(bp); b.connect(bp); done(a, g);
  },
};
function tom(t, v, f) {
  const g = env(t, v * 0.8, 0.45), o = osc("sine", f, t, t + 0.5);
  o.frequency.exponentialRampToValueAtTime(f * 0.62, t + 0.3); o.connect(g); done(o, g);
}
function playDrum(i, t = ctx.currentTime, vel = 1) { const fx = DRUM_FX[DRUMS[i].id]; if (fx) fx(Math.max(t, ctx.currentTime), vel); }

// ---------- Keys tab ----------
const piano = $("#piano");
const KEYMAP = "awsedftgyhujkolp;'"; // a = C, w = C#, s = D ... like a piano on the home row
const pointers = new Map(); // pointerId -> { midi, voice }
const held = new Map(); // notes kept ringing by Hold: midi -> voice
const keyDown = new Map(); // computer keys -> voice
let pianoStart = 60;

function buildPiano() {
  const whites = clamp(Math.floor(piano.clientWidth / 46), 7, 22);
  pianoStart = 12 * (settings.octave + 1);
  const w = 100 / whites, keys = [];
  let wi = 0;
  for (let m = pianoStart; wi < whites; m++) {
    const black = [1, 3, 6, 8, 10].includes(m % 12);
    if (black) keys.push(`<div class="key b" data-m="${m}" style="left:${wi * w - w * 0.3}%;width:${w * 0.6}%"></div>`);
    else { keys.push(`<div class="key w" data-m="${m}" style="left:${wi * w}%;width:${w}%">${m % 12 === 0 ? noteName(m) : ""}</div>`); wi++; }
  }
  piano.innerHTML = keys.join("");
  $("#octVal").textContent = settings.octave;
}
const keyEl = (m) => piano.querySelector(`.key[data-m="${m}"]`);
function lightKey(m, on) { const k = keyEl(m); if (k) k.classList.toggle("down", on); }

function press(m) {
  if (!ensureAudio()) return null;
  if (held.has(m)) { noteOff(held.get(m)); held.delete(m); }
  lightKey(m, true);
  return noteOn(m);
}
function release(m, v) {
  if (settings.hold) { held.set(m, v); return; }
  noteOff(v);
  const stillDown = [...pointers.values()].some((p) => p.midi === m) || [...keyDown.values()].some((x) => x.midi === m);
  if (!stillDown) lightKey(m, false);
}
function keyAt(x, y) { const el = document.elementFromPoint(x, y); return el && el.closest(".key") && piano.contains(el) ? +el.dataset.m : null; }

piano.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  piano.setPointerCapture?.(e.pointerId);
  const m = keyAt(e.clientX, e.clientY); if (m == null) return;
  pointers.set(e.pointerId, { midi: m, voice: press(m) });
});
piano.addEventListener("pointermove", (e) => {
  const p = pointers.get(e.pointerId); if (!p) return;
  const m = keyAt(e.clientX, e.clientY);
  if (m == null || m === p.midi) return;
  const old = p.midi; p.midi = m;
  release(old, p.voice);
  p.voice = press(m);
});
function pianoUp(e) {
  const p = pointers.get(e.pointerId); if (!p) return;
  pointers.delete(e.pointerId);
  release(p.midi, p.voice);
}
piano.addEventListener("pointerup", pianoUp);
piano.addEventListener("pointercancel", pianoUp);
piano.addEventListener("contextmenu", (e) => e.preventDefault());

function releaseHeld() {
  held.forEach((v, m) => { noteOff(v); lightKey(m, false); });
  held.clear();
}

function buildPresets() {
  $("#presets").innerHTML = Object.entries(PRESETS).map(([id, p]) => `<button class="chip" data-p="${id}" aria-pressed="${id === settings.preset}">${p.name}</button>`).join("");
}
$("#presets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-p]"); if (!b) return;
  settings.preset = b.dataset.p; save();
  $$("#presets .chip").forEach((c) => c.setAttribute("aria-pressed", c === b));
  if (ensureAudio()) { const v = noteOn(pianoStart + 12, { vel: 0.7 }); setTimeout(() => noteOff(v), 350); }
});
$$("[data-oct]").forEach((b) => b.addEventListener("click", () => setOctave(settings.octave + +b.dataset.oct)));
function setOctave(o) {
  o = clamp(o, 1, 7); if (o === settings.octave) return;
  releaseHeld(); settings.octave = o; save(); buildPiano();
}
$("#hold").addEventListener("click", (e) => {
  settings.hold = !settings.hold;
  e.currentTarget.setAttribute("aria-pressed", settings.hold);
  if (!settings.hold) releaseHeld();
});
for (const id of ["tone", "reverb", "echo"]) {
  const el = $("#" + id); el.value = settings[id];
  el.addEventListener("input", () => { settings[id] = +el.value; applyFx(); save(); });
}

// ---------- Pads tab ----------
const padsEl = $("#pads");
padsEl.innerHTML = DRUMS.map((d, i) => `<button class="pad" data-i="${i}">${d.name}<kbd class="kb">${i + 1}</kbd></button>`).join("");
function hitPad(i) {
  if (!ensureAudio()) return;
  playDrum(i);
  const el = padsEl.children[i];
  el.classList.add("hit"); setTimeout(() => el.classList.remove("hit"), 90);
  if (navigator.vibrate) try { navigator.vibrate(6); } catch { /* not allowed */ }
  if (settings.padRec && playing) recordHit(i);
}
padsEl.addEventListener("pointerdown", (e) => {
  const b = e.target.closest(".pad"); if (!b) return;
  e.preventDefault(); hitPad(+b.dataset.i);
});
padsEl.addEventListener("contextmenu", (e) => e.preventDefault());
const padRec = $("#padRec");
padRec.checked = !!settings.padRec;
padRec.addEventListener("change", () => { settings.padRec = padRec.checked; save(); });

// ---------- Beats tab: step sequencer ----------
const grid = $("#grid");
let playing = false, step = 0, nextTime = 0, timer = 0, page = 0, nowStep = -1;
const queue = []; // steps scheduled ahead, for drawing the playhead in time with the sound
const skip = new Set(); // hits you just played live, so they aren't doubled on this pass
const stepDur = () => 60 / pat().bpm / 4;

function melodyBase(p) { return 12 * ((PRESETS[p.sound].oct || 4) + 1) + p.root; }
function rowNote(p, r) { const sc = SCALES[p.scale].s; return melodyBase(p) + 12 * Math.floor(r / sc.length) + sc[r % sc.length]; }

function scheduleStep(s, t) {
  const p = pat();
  p.drums.forEach((row, r) => {
    if (!row[s]) return;
    if (skip.delete(r + ":" + s)) return;
    playDrum(r, t, s % 4 === 0 ? 1 : 0.85);
  });
  const sd = stepDur();
  p.melody.forEach((row, r) => {
    if (!row[s] || (s > 0 && row[s - 1])) return; // a run of squares is one long note
    let len = 1; while (s + len < STEPS && row[s + len]) len++;
    const v = noteOn(rowNote(p, r), { t, vel: 0.85, preset: p.sound });
    noteOff(v, t + len * sd * 0.92);
  });
}

function scheduler() {
  const sd = stepDur();
  while (nextTime < ctx.currentTime + 0.12) {
    const t = nextTime + (step % 2 ? pat().swing * sd : 0);
    scheduleStep(step, t);
    queue.push({ step, t });
    nextTime += sd;
    step = (step + 1) % STEPS;
  }
}

function play() {
  if (!ensureAudio()) return;
  playing = true; step = 0; queue.length = 0; skip.clear();
  nextTime = ctx.currentTime + 0.06;
  scheduler(); timer = setInterval(scheduler, 25);
  drawPlay();
}
function stop() {
  playing = false; clearInterval(timer); queue.length = 0; nowStep = -1;
  markNow(); drawPlay();
}
function drawPlay() {
  const b = $("#play");
  b.setAttribute("aria-label", playing ? "Stop" : "Play");
  b.innerHTML = playing
    ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l11-6.5a1 1 0 0 0 0-1.72l-11-6.5A1 1 0 0 0 8 5.5z"/></svg>';
}
$("#play").addEventListener("click", () => (playing ? stop() : play()));

// Pad hits while the beat plays land on the nearest step.
function recordHit(i) {
  if (nowStep < 0) return;
  const last = lastShown;
  let s = last.step;
  if (ctx.currentTime - last.t > stepDur() / 2) s = (s + 1) % STEPS;
  if (pat().drums[i][s]) return;
  pat().drums[i][s] = 1;
  if (!queue.some((q) => q.step === s)) skip.add(i + ":" + s);
  save();
  if (settings.track === "drums") renderGrid();
}

const narrow = () => grid.clientWidth < 520;
function visible() { return narrow() ? [page * 8, 8] : [0, 16]; }

function renderGrid() {
  const p = pat(), track = settings.track, data = p[track];
  const [start, count] = visible();
  grid.className = "grid " + track;
  grid.style.setProperty("--cols", count);
  const order = track === "drums" ? DRUMS.map((_, r) => r) : [...Array(ROWS).keys()].reverse();
  grid.innerHTML = order.map((r) => {
    const label = track === "drums" ? DRUMS[r].name : noteName(rowNote(p, r));
    let cells = "";
    for (let s = start; s < start + count; s++) {
      const cls = ["cell"]; if (data[r][s]) cls.push("on"); if (Math.floor(s / 4) % 2 === 0) cls.push("beat");
      cells += `<button class="${cls.join(" ")}" data-r="${r}" data-s="${s}" aria-pressed="${!!data[r][s]}" aria-label="${label}, step ${s + 1}"></button>`;
    }
    return `<div class="grow"><button class="rlabel" data-r="${r}">${label}</button><div class="cells">${cells}</div></div>`;
  }).join("");
  $("#pages").hidden = !narrow();
  $$("#pages [data-page]").forEach((b) => b.setAttribute("aria-pressed", +b.dataset.page === page));
  markNow();
}

function preview(r) {
  if (!ensureAudio()) return;
  if (settings.track === "drums") playDrum(r);
  else { const p = pat(), v = noteOn(rowNote(p, r), { preset: p.sound, vel: 0.8 }); noteOff(v, ctx.currentTime + 0.3); }
}

// Tap a square to turn it on or off; drag to paint several.
let paint = null;
function setCell(el, on) {
  const r = +el.dataset.r, s = +el.dataset.s, data = pat()[settings.track];
  if (!!data[r][s] === on) return;
  data[r][s] = on ? 1 : 0;
  el.classList.toggle("on", on); el.setAttribute("aria-pressed", on);
}
grid.addEventListener("pointerdown", (e) => {
  const lbl = e.target.closest(".rlabel");
  if (lbl) { preview(+lbl.dataset.r); return; }
  const c = e.target.closest(".cell"); if (!c) return;
  e.preventDefault();
  paint = { on: !c.classList.contains("on"), id: e.pointerId };
  setCell(c, paint.on);
  if (paint.on && !playing) preview(+c.dataset.r);
});
grid.addEventListener("pointermove", (e) => {
  if (!paint || e.pointerId !== paint.id) return;
  const el = document.elementFromPoint(e.clientX, e.clientY);
  const c = el && el.closest(".cell"); if (c && grid.contains(c)) setCell(c, paint.on);
});
const endPaint = () => { if (paint) { paint = null; save(); } };
window.addEventListener("pointerup", endPaint);
window.addEventListener("pointercancel", endPaint);
grid.addEventListener("click", (e) => { if (e.detail === 0 && e.target.closest(".cell")) { const c = e.target.closest(".cell"); setCell(c, !c.classList.contains("on")); save(); } }); // keyboard users

$$("#pages [data-page]").forEach((b) => b.addEventListener("click", () => { page = +b.dataset.page; renderGrid(); }));

let lastNow = [], lastShown = { step: 0, t: 0 };
function markNow() {
  lastNow.forEach((c) => c.classList.remove("now"));
  lastNow = nowStep < 0 ? [] : $$(`.cell[data-s="${nowStep}"]`, grid);
  lastNow.forEach((c) => c.classList.add("now"));
  if (narrow()) $$("#pages [data-page]").forEach((b) => b.classList.toggle("live", nowStep >= 0 && Math.floor(nowStep / 8) === +b.dataset.page));
}

function setTrack(t) {
  settings.track = t; save();
  $$("[data-track]").forEach((b) => b.setAttribute("aria-selected", b.dataset.track === t));
  $("#melodyOpts").hidden = t !== "melody";
  renderGrid();
}
$$("[data-track]").forEach((b) => b.addEventListener("click", () => setTrack(b.dataset.track)));

function renderSlots() {
  $("#slots").innerHTML = "ABCD".split("").map((l, i) => `<button data-slot="${i}" aria-pressed="${i === cur}" aria-label="Pattern ${l}">${l}</button>`).join("");
}
$("#slots").addEventListener("click", (e) => {
  const b = e.target.closest("[data-slot]"); if (!b) return;
  cur = +b.dataset.slot; save(); renderSlots(); syncPattern();
});

function syncPattern() {
  const p = pat();
  $("#bpmVal").textContent = p.bpm;
  $("#swing").value = p.swing;
  $("#mSound").value = p.sound; $("#mRoot").value = p.root; $("#mScale").value = p.scale;
  applyFx(); renderGrid();
}
let bpmHold;
function nudgeBpm(d) { const p = pat(); p.bpm = clamp(p.bpm + d, 50, 200); $("#bpmVal").textContent = p.bpm; applyFx(); save(); }
$$("[data-bpm]").forEach((b) => {
  const d = +b.dataset.bpm;
  b.addEventListener("pointerdown", () => { nudgeBpm(d); let n = 0; bpmHold = setInterval(() => nudgeBpm(d * (++n > 8 ? 5 : 1)), 140); });
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(ev, () => clearInterval(bpmHold));
  b.addEventListener("click", (e) => { if (e.detail === 0) nudgeBpm(d); });
});
$("#swing").addEventListener("input", (e) => { pat().swing = +e.target.value; save(); });

$("#mSound").innerHTML = Object.entries(PRESETS).map(([id, p]) => `<option value="${id}">${p.name}</option>`).join("");
$("#mRoot").innerHTML = NOTE_NAMES.map((n, i) => `<option value="${i}">${n}</option>`).join("");
$("#mScale").innerHTML = Object.entries(SCALES).map(([id, s]) => `<option value="${id}">${s.name}</option>`).join("");
$("#mSound").addEventListener("change", (e) => { pat().sound = e.target.value; save(); renderGrid(); preview(0); });
$("#mRoot").addEventListener("change", (e) => { pat().root = +e.target.value; save(); renderGrid(); });
$("#mScale").addEventListener("change", (e) => { pat().scale = e.target.value; save(); renderGrid(); });

const isEmpty = (p) => !p.drums.some((r) => r.some(Boolean)) && !p.melody.some((r) => r.some(Boolean));
$("#starter").innerHTML = '<option value="">Load a starter beat…</option>' + Object.entries(STARTERS).map(([id, s]) => `<option value="${id}">${s.name}</option>`).join("");
$("#starter").addEventListener("change", (e) => {
  const id = e.target.value; e.target.value = "";
  if (!id) return;
  if (!isEmpty(pat()) && !confirm(`Replace pattern ${"ABCD"[cur]} with the ${STARTERS[id].name} beat?`)) return;
  patterns[cur] = starter(id); save(); syncPattern();
  if (!playing) play();
});
$("#clear").addEventListener("click", () => {
  if (isEmpty(pat())) return;
  if (!confirm(`Clear the ${settings.track === "drums" ? "drums" : "melody"} in pattern ${"ABCD"[cur]}?`)) return;
  pat()[settings.track] = emptyGrid(); save(); renderGrid();
});

// ---------- Recording ----------
const recBtn = $("#rec");
let recorder = null, recStart = 0, recTick = 0, recUrl = "";
const recMime = window.MediaRecorder && ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg"].find((m) => MediaRecorder.isTypeSupported?.(m));
if (!window.MediaRecorder || !(window.AudioContext || window.webkitAudioContext)?.prototype.createMediaStreamDestination) recBtn.hidden = true;

recBtn.addEventListener("click", () => {
  if (recorder) { recorder.stop(); return; }
  if (!ensureAudio() || !recDest) return;
  const chunks = [];
  try { recorder = new MediaRecorder(recDest.stream, recMime ? { mimeType: recMime } : undefined); }
  catch { toast("Recording isn't supported in this browser."); return; }
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = () => {
    const type = recorder.mimeType || recMime || "audio/webm";
    recorder = null; clearInterval(recTick);
    recBtn.classList.remove("on"); $("#recLabel").textContent = "Rec";
    if (!chunks.length) { toast("Nothing was recorded."); return; }
    showRecording(new Blob(chunks, { type }));
  };
  recorder.start(250);
  recStart = Date.now();
  recBtn.classList.add("on");
  const tick = () => { const s = Math.floor((Date.now() - recStart) / 1000); $("#recLabel").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  tick(); recTick = setInterval(tick, 500);
});

function showRecording(blob) {
  if (recUrl) URL.revokeObjectURL(recUrl);
  recUrl = URL.createObjectURL(blob);
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const d = new Date(), pad = (n) => String(n).padStart(2, "0");
  const name = `synth-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.${ext}`;
  $("#recAudio").src = recUrl;
  const saveLink = $("#recSave"); saveLink.href = recUrl; saveLink.download = name;
  const file = new File([blob], name, { type: blob.type });
  const share = $("#recShare");
  share.hidden = !(navigator.canShare && navigator.canShare({ files: [file] }));
  share.onclick = () => navigator.share({ files: [file], title: "My Synth recording" }).catch(() => {});
  $("#scrim").hidden = false; $("#recSheet").hidden = false;
}
function closeSheet() { $("#recAudio").pause(); $("#scrim").hidden = true; $("#recSheet").hidden = true; }
$("#recClose").addEventListener("click", closeSheet);
$("#scrim").addEventListener("click", closeSheet);

// ---------- Waveform and playhead ----------
const scope = $("#scope"), sctx = scope.getContext("2d");
let inkColor = "", wave;
const readColors = () => { inkColor = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#DD4F27"; };
readColors();
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", readColors);

function frame() {
  // playhead: show the step that is sounding right now
  if (playing && ctx) {
    let changed = false;
    while (queue.length && queue[0].t <= ctx.currentTime) { lastShown = queue.shift(); nowStep = lastShown.step; changed = true; }
    if (changed && settings.tab === "beats") markNow();
  }
  // waveform
  const dpr = window.devicePixelRatio || 1, w = scope.clientWidth, h = scope.clientHeight;
  if (scope.width !== Math.round(w * dpr) || scope.height !== Math.round(h * dpr)) { scope.width = Math.round(w * dpr); scope.height = Math.round(h * dpr); }
  sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  sctx.clearRect(0, 0, w, h);
  sctx.strokeStyle = inkColor; sctx.lineWidth = 2; sctx.lineJoin = "round";
  sctx.beginPath();
  if (analyser) {
    wave = wave || new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(wave);
    const n = wave.length;
    for (let i = 0; i < n; i += 4) {
      const x = (i / (n - 1)) * w, y = h / 2 + ((wave[i] - 128) / 128) * (h / 2 - 2);
      i ? sctx.lineTo(x, y) : sctx.moveTo(x, y);
    }
  } else { sctx.moveTo(0, h / 2); sctx.lineTo(w, h / 2); }
  sctx.stroke();
  requestAnimationFrame(frame);
}

// ---------- Tabs, keyboard, start-up ----------
function setTab(t) {
  settings.tab = t; save();
  $$(".view").forEach((v) => (v.hidden = v.dataset.view !== t));
  $$("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t));
  if (t === "keys") buildPiano();
  if (t === "beats") renderGrid();
}
$$("[data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

const typing = (e) => e.target.closest && e.target.closest("input, select, textarea");
window.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || typing(e)) return;
  if (e.key === " ") { e.preventDefault(); if (!e.repeat) (playing ? stop() : play()); return; }
  if (e.repeat) return;
  const k = e.key.toLowerCase();
  if (settings.tab === "pads" && k >= "1" && k <= "8") { hitPad(+k - 1); return; }
  if (settings.tab !== "keys") return;
  if (k === "z") return setOctave(settings.octave - 1);
  if (k === "x") return setOctave(settings.octave + 1);
  const i = KEYMAP.indexOf(k);
  if (i < 0 || keyDown.has(k)) return;
  const m = 12 * (settings.octave + 1) + i;
  const v = press(m); if (v) keyDown.set(k, { midi: m, voice: v });
});
window.addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase(), x = keyDown.get(k); if (!x) return;
  keyDown.delete(k); release(x.midi, x.voice);
});
window.addEventListener("blur", () => { keyDown.forEach((x) => release(x.midi, x.voice)); keyDown.clear(); });

// Phones only allow sound to start after a tap, so wake the audio on the first touch anywhere.
for (const ev of ["pointerdown", "touchend", "keydown"]) window.addEventListener(ev, () => ensureAudio(), { capture: true, passive: true });

let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (settings.tab === "keys") buildPiano(); if (settings.tab === "beats") renderGrid(); }, 120); });

let toastT;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 2600); }

// Keyboard hints only make sense with a real keyboard.
if (matchMedia("(hover: hover) and (pointer: fine)").matches) {
  $("#keysHint").textContent += " On a computer, play with the A–L row (W, E, T, Y, U are the black keys) and change octave with Z and X. Space starts and stops the beat.";
  $("#padsHint").textContent += " Keys 1–8 play the pads.";
} else $$(".pad .kb").forEach((k) => k.remove());

settings.hold = false;
buildPresets(); renderSlots(); drawPlay(); setTrack(settings.track); syncPattern(); setTab(settings.tab);
requestAnimationFrame(frame);

if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
