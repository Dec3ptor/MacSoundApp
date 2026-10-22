#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Speaker Balance - no-install version.

Balances a stereo sound system where one speaker is quieter than the other,
then lets you raise or lower the overall volume while that balance is kept.

Runs on the Python that ships with macOS (2.7 on Big Sur, or any python3).
No Xcode, no extra packages. It drives the same per-channel CoreAudio volume
controls that the Balance slider in System Preferences uses.

    python speaker_balance.py          # opens the controls in your browser
    python speaker_balance.py --cli    # text mode in the Terminal
"""
from __future__ import print_function

import ctypes
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import wave

# ---------------------------------------------------------------------------
# CoreAudio via ctypes
# ---------------------------------------------------------------------------

c_uint32 = ctypes.c_uint32
c_void_p = ctypes.c_void_p
POINTER = ctypes.POINTER

try:
    CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
except OSError:
    print("This tool only works on macOS.")
    sys.exit(1)


class PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


CA.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32)]
CA.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
CA.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
CA.AudioObjectGetPropertyData.restype = ctypes.c_int32
CA.AudioObjectSetPropertyData.argtypes = [c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, c_uint32, c_void_p]
CA.AudioObjectSetPropertyData.restype = ctypes.c_int32
CA.AudioObjectHasProperty.argtypes = [c_uint32, POINTER(PropertyAddress)]
CA.AudioObjectHasProperty.restype = ctypes.c_ubyte
CA.AudioObjectIsPropertySettable.argtypes = [c_uint32, POINTER(PropertyAddress), POINTER(ctypes.c_ubyte)]
CA.AudioObjectIsPropertySettable.restype = ctypes.c_int32
CF.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]
CF.CFStringGetCString.restype = ctypes.c_ubyte
CF.CFRelease.argtypes = [c_void_p]
CF.CFRelease.restype = None


def fourcc(code):
    b = code.encode("ascii")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3] if sys.version_info[0] >= 3 else \
        (ord(b[0]) << 24) | (ord(b[1]) << 16) | (ord(b[2]) << 8) | ord(b[3])


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = fourcc("glob")
SCOPE_OUTPUT = fourcc("outp")
PROP_DEVICES = fourcc("dev#")
PROP_DEFAULT_OUTPUT = fourcc("dOut")
PROP_NAME = fourcc("lnam")
PROP_UID = fourcc("uid ")
PROP_STREAM_CONFIG = fourcc("slay")
PROP_VOLUME_SCALAR = fourcc("volm")
PROP_VOLUME_DB = fourcc("vold")
CF_UTF8 = 0x08000100

MASTER, LEFT, RIGHT = 0, 1, 2


def _addr(selector, scope, element):
    return PropertyAddress(selector, scope, element)


def _get_raw(obj, address):
    size = c_uint32(0)
    if CA.AudioObjectGetPropertyDataSize(obj, ctypes.byref(address), 0, None, ctypes.byref(size)) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if CA.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None, ctypes.byref(size), buf) != 0:
        return None
    return buf.raw[: size.value]


def _get_string(obj, selector):
    address = _addr(selector, SCOPE_GLOBAL, MASTER)
    if not CA.AudioObjectHasProperty(obj, ctypes.byref(address)):
        return None
    ref = c_void_p(0)
    size = c_uint32(ctypes.sizeof(c_void_p))
    if CA.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(ref)) != 0 or not ref.value:
        return None
    buf = ctypes.create_string_buffer(512)
    ok = CF.CFStringGetCString(ref, buf, 512, CF_UTF8)
    CF.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else None


class Device(object):
    def __init__(self, device_id):
        self.id = device_id
        self.name = _get_string(device_id, PROP_NAME) or "Output device %d" % device_id
        self.uid = _get_string(device_id, PROP_UID) or "device-%d" % device_id
        self.output_channels = self._output_channels()

    def _output_channels(self):
        raw = _get_raw(self.id, _addr(PROP_STREAM_CONFIG, SCOPE_OUTPUT, MASTER))
        if not raw or len(raw) < 8:
            return 0
        count = struct.unpack("<I", raw[:4])[0]
        total = 0
        for i in range(count):
            offset = 8 + i * 16  # AudioBuffer: UInt32 channels, UInt32 bytes, void* data
            if offset + 4 > len(raw):
                break
            total += struct.unpack("<I", raw[offset:offset + 4])[0]
        return total

    def _vol_addr(self, selector, channel):
        return _addr(selector, SCOPE_OUTPUT, channel)

    def can_set_volume(self, channel):
        address = self._vol_addr(PROP_VOLUME_SCALAR, channel)
        if not CA.AudioObjectHasProperty(self.id, ctypes.byref(address)):
            return False
        settable = ctypes.c_ubyte(0)
        if CA.AudioObjectIsPropertySettable(self.id, ctypes.byref(address), ctypes.byref(settable)) != 0:
            return False
        return bool(settable.value)

    def volume(self, channel):
        address = self._vol_addr(PROP_VOLUME_SCALAR, channel)
        value = ctypes.c_float(0)
        size = c_uint32(4)
        if CA.AudioObjectGetPropertyData(self.id, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)) != 0:
            return None
        return value.value

    def set_volume(self, channel, value):
        address = self._vol_addr(PROP_VOLUME_SCALAR, channel)
        v = ctypes.c_float(min(1.0, max(0.0, value)))
        return CA.AudioObjectSetPropertyData(self.id, ctypes.byref(address), 0, None, 4, ctypes.byref(v)) == 0

    def decibels(self, channel):
        address = self._vol_addr(PROP_VOLUME_DB, channel)
        if not CA.AudioObjectHasProperty(self.id, ctypes.byref(address)):
            return None
        value = ctypes.c_float(0)
        size = c_uint32(4)
        if CA.AudioObjectGetPropertyData(self.id, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)) != 0:
            return None
        return value.value


def all_output_devices():
    raw = _get_raw(SYSTEM_OBJECT, _addr(PROP_DEVICES, SCOPE_GLOBAL, MASTER))
    if not raw:
        return []
    ids = struct.unpack("<%dI" % (len(raw) // 4), raw)
    devices = [Device(i) for i in ids]
    return [d for d in devices if d.output_channels > 0]


def default_output_device_id():
    raw = _get_raw(SYSTEM_OBJECT, _addr(PROP_DEFAULT_OUTPUT, SCOPE_GLOBAL, MASTER))
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", raw[:4])[0]


# ---------------------------------------------------------------------------
# Balance model: hardware level = overall x trim, per speaker
# ---------------------------------------------------------------------------

SETTINGS_PATH = os.path.expanduser("~/Library/Application Support/SpeakerBalance/settings.json")
TOLERANCE = 0.03


def clamp(v):
    return min(1.0, max(0.0, float(v)))


class Controller(object):
    STEREO, MASTER_ONLY, UNSUPPORTED = "stereo", "master", "none"

    def __init__(self):
        self.settings = self._load_settings()
        self.keep_balance = self.settings.get("keepBalance", True)
        self.devices = []
        self.device = None
        self.capability = self.UNSUPPORTED
        self.master = 1.0
        self.left_trim = 1.0
        self.right_trim = 1.0
        self.hw_left = 0.0
        self.hw_right = 0.0
        self.left_db = None
        self.right_db = None
        self._last_written = None
        self._last_observed = None
        self.refresh_devices()

    # -- settings --------------------------------------------------------
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        try:
            d = os.path.dirname(SETTINGS_PATH)
            if not os.path.isdir(d):
                os.makedirs(d)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    def _save_trims(self):
        if self.device:
            self.settings.setdefault("trims", {})[self.device.uid] = [self.left_trim, self.right_trim]
            self._save_settings()

    def _saved_trims(self, device):
        t = self.settings.get("trims", {}).get(device.uid)
        if isinstance(t, list) and len(t) == 2:
            return clamp(t[0]), clamp(t[1])
        return None

    # -- devices ---------------------------------------------------------
    def refresh_devices(self):
        self.devices = all_output_devices()
        default_id = default_output_device_id()
        current = self.device.id if self.device else None
        if current is not None and any(d.id == current for d in self.devices):
            return
        pick = None
        for d in self.devices:
            if d.id == default_id:
                pick = d
        if pick is None and self.devices:
            pick = self.devices[0]
        self.select(pick)

    def select(self, device):
        self.device = device
        self._last_written = None
        self._last_observed = None
        if device is None:
            self.capability = self.UNSUPPORTED
            return
        if device.can_set_volume(LEFT) and device.can_set_volume(RIGHT):
            self.capability = self.STEREO
        elif device.can_set_volume(MASTER):
            self.capability = self.MASTER_ONLY
        else:
            self.capability = self.UNSUPPORTED
            self.left_db = self.right_db = None
            return
        saved = self._saved_trims(device) if self.capability == self.STEREO else None
        if saved and self.keep_balance:
            self.left_trim, self.right_trim = saved
            levels = self._read_levels()
            self._update_meters(levels)
            self._last_observed = levels
            self.master = self._infer_master(levels)
            self.apply()
        else:
            self.adopt_hardware()

    # -- user actions ----------------------------------------------------
    def set_master(self, v):
        self.master = clamp(v)
        self.apply()

    def set_left_trim(self, v):
        self.left_trim = clamp(v)
        self._save_trims()
        self.apply()

    def set_right_trim(self, v):
        self.right_trim = clamp(v)
        self._save_trims()
        self.apply()

    def reset_trims(self):
        self.left_trim = self.right_trim = 1.0
        self._save_trims()
        self.apply()

    def set_keep_balance(self, enabled):
        self.keep_balance = bool(enabled)
        self.settings["keepBalance"] = self.keep_balance
        self._save_settings()
        if enabled and self.capability == self.STEREO:
            self.apply()

    # -- hardware --------------------------------------------------------
    def apply(self):
        if not self.device:
            return
        if self.capability == self.STEREO:
            l = clamp(self.master * self.left_trim)
            r = clamp(self.master * self.right_trim)
            self._last_written = (l, r)
            self.device.set_volume(LEFT, l)
            self.device.set_volume(RIGHT, r)
        elif self.capability == self.MASTER_ONLY:
            self._last_written = (self.master, self.master)
            self.device.set_volume(MASTER, self.master)

    def _read_levels(self):
        if not self.device:
            return (0.0, 0.0)
        if self.capability == self.STEREO:
            return (self.device.volume(LEFT) or 0.0, self.device.volume(RIGHT) or 0.0)
        if self.capability == self.MASTER_ONLY:
            m = self.device.volume(MASTER) or 0.0
            return (m, m)
        return (0.0, 0.0)

    def _update_meters(self, levels):
        self.hw_left, self.hw_right = levels
        if not self.device or self.capability == self.UNSUPPORTED:
            self.left_db = self.right_db = None
        elif self.capability == self.STEREO:
            self.left_db = self.device.decibels(LEFT)
            self.right_db = self.device.decibels(RIGHT)
        else:
            self.left_db = self.right_db = self.device.decibels(MASTER)

    def adopt_hardware(self):
        levels = self._read_levels()
        self._update_meters(levels)
        self._last_observed = levels
        if self.capability == self.STEREO:
            peak = max(levels)
            self.master = peak
            if peak > 0.001:
                self.left_trim = clamp(levels[0] / peak)
                self.right_trim = clamp(levels[1] / peak)
        elif self.capability == self.MASTER_ONLY:
            self.master = levels[0]
            self.left_trim = self.right_trim = 1.0

    def _infer_master(self, levels):
        scale = max(self.left_trim, self.right_trim)
        if scale <= 0.001:
            return max(levels)
        return clamp(max(levels) / scale)

    def sync_from_hardware(self):
        """Call periodically. Returns True if the model changed."""
        if not self.device or self.capability == self.UNSUPPORTED:
            return False
        levels = self._read_levels()
        self._update_meters(levels)
        o = self._last_observed
        if o and abs(o[0] - levels[0]) < 0.001 and abs(o[1] - levels[1]) < 0.001:
            return False
        self._last_observed = levels
        w = self._last_written
        if w and abs(w[0] - levels[0]) <= TOLERANCE and abs(w[1] - levels[1]) <= TOLERANCE:
            return False
        if self.capability != self.STEREO:
            self.master = levels[0]
            return True
        if self.keep_balance:
            self.master = self._infer_master(levels)
            el = clamp(self.master * self.left_trim)
            er = clamp(self.master * self.right_trim)
            if abs(el - levels[0]) > TOLERANCE or abs(er - levels[1]) > TOLERANCE:
                self.apply()
        else:
            self.adopt_hardware()
        return True


# ---------------------------------------------------------------------------
# Test sound: pink noise WAV played with the built-in afplay
# ---------------------------------------------------------------------------

class TestSound(object):
    def __init__(self):
        self.proc = None
        self.side = None
        self.files = {}
        self.tmpdir = tempfile.mkdtemp(prefix="speakerbalance-")

    def _pink_samples(self, n):
        b0 = b1 = b2 = b3 = b4 = b5 = b6 = 0.0
        out = []
        rnd = random.uniform
        for _ in range(n):
            w = rnd(-1.0, 1.0)
            b0 = 0.99886 * b0 + w * 0.0555179
            b1 = 0.99332 * b1 + w * 0.0750759
            b2 = 0.96900 * b2 + w * 0.1538520
            b3 = 0.86650 * b3 + w * 0.3104856
            b4 = 0.55000 * b4 + w * 0.5329522
            b5 = -0.7616 * b5 - w * 0.0168980
            pink = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362) * 0.11
            b6 = w * 0.115926
            out.append(max(-1.0, min(1.0, pink * 0.4)))
        return out

    def _file(self, side):
        if side in self.files:
            return self.files[side]
        rate, seconds = 44100, 8
        samples = self._pink_samples(rate * seconds)
        path = os.path.join(self.tmpdir, "pink-%s.wav" % side)
        w = wave.open(path, "wb")
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        pack = struct.Struct("<hh").pack
        for s in samples:
            v = int(s * 32767)
            frames += pack(v if side in ("left", "both") else 0, v if side in ("right", "both") else 0)
        w.writeframes(bytes(frames))
        w.close()
        self.files[side] = path
        return path

    def toggle(self, side):
        if self.side == side:
            self.stop()
        else:
            self.play(side)

    def play(self, side):
        self.stop()
        self.side = side
        self._launch()

    def _launch(self):
        try:
            self.proc = subprocess.Popen(["/usr/bin/afplay", self._file(self.side)])
        except Exception:
            self.proc = None
            self.side = None

    def tick(self):
        """Restart the clip when it ends so it loops until stopped."""
        if self.side and self.proc is not None and self.proc.poll() is not None:
            self._launch()

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None
        self.side = None


# ---------------------------------------------------------------------------
# GUI: a tiny local web page opened in your browser (no Tk, which crashes on
# some macOS/Python combinations)
# ---------------------------------------------------------------------------

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Speaker Balance</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#f5f5f7; --card:#fff; --fg:#1d1d1f; --muted:#6e6e73; --accent:#0a84ff; --line:#e5e5ea; }
@media (prefers-color-scheme: dark) { :root { --bg:#1c1c1e; --card:#2c2c2e; --fg:#f5f5f7; --muted:#98989d; --line:#3a3a3c; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
main { max-width:560px; margin:24px auto; padding:0 16px; }
h1 { font-size:22px; margin:0 0 16px; }
section { background:var(--card); border-radius:12px; padding:16px; margin-bottom:14px; box-shadow:0 1px 2px rgba(0,0,0,.08); }
h2 { font-size:15px; margin:0 0 10px; }
.row { display:flex; align-items:center; gap:10px; margin:8px 0; }
.row label { width:52px; }
input[type=range] { flex:1; -webkit-appearance:none; height:6px; border-radius:3px; background:var(--line); outline:none; }
input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:22px; height:22px; border-radius:50%; background:#fff; border:1px solid #c7c7cc; box-shadow:0 1px 3px rgba(0,0,0,.3); cursor:pointer; }
input[type=range]:disabled { opacity:.4; }
.val { width:48px; text-align:right; font-variant-numeric:tabular-nums; }
.hw { width:110px; text-align:right; color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }
.hint { color:var(--muted); font-size:13px; margin:6px 0 0; }
select, button { font:inherit; padding:6px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--fg); }
button { cursor:pointer; }
button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
button.small { padding:4px 10px; font-size:13px; }
.spread { display:flex; justify-content:space-between; align-items:center; }
#err { color:#ff453a; font-size:13px; }
footer { color:var(--muted); font-size:12px; text-align:center; margin-top:10px; }
</style></head>
<body><main>
<h1>Speaker Balance</h1>

<section>
  <div class="row"><h2 style="margin:0;width:120px">Output device</h2>
    <select id="device" style="flex:1"></select>
    <button class="small" id="rescan">Rescan</button></div>
</section>

<section>
  <h2>Overall volume</h2>
  <div class="row"><span>🔈</span><input type="range" id="master" min="0" max="100"><span>🔊</span><span class="val" id="master-val"></span></div>
  <p class="hint">Raises or lowers both speakers together while keeping the balance below.</p>
</section>

<section>
  <div class="spread"><h2>Speaker balance</h2><button class="small" id="reset">Reset</button></div>
  <div class="row"><label>Left</label><input type="range" id="left" min="0" max="100"><span class="val" id="left-val"></span><span class="hw" id="left-hw"></span></div>
  <div class="row"><label>Right</label><input type="range" id="right" min="0" max="100"><span class="val" id="right-val"></span><span class="hw" id="right-hw"></span></div>
  <p class="hint" id="hint"></p>
</section>

<section>
  <h2>Test sound (pink noise)</h2>
  <div class="row">
    <button data-side="left">▶ Left</button><button data-side="right">▶ Right</button><button data-side="both">▶ Both</button><button id="stop">■ Stop</button>
  </div>
  <p class="hint">Plays through the system default output. Use one side at a time to match the speakers by ear.</p>
</section>

<section>
  <label class="row" style="margin:0"><input type="checkbox" id="keep"> Keep balance when the volume is changed elsewhere (volume keys, menu bar)</label>
  <p class="hint">Turn this off if you'd rather use the Balance slider in System Preferences.</p>
</section>

<div class="spread"><span id="err"></span><button class="small" id="quit">Quit</button></div>
<footer>Balance is saved per device. Keep the Terminal window open while using this page.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
let pending = null, inflight = false, dragging = {}, lastDevices = "";

function send(payload) {
  pending = Object.assign(pending || {}, payload);
  if (!inflight) flush();
}
async function flush() {
  if (!pending) return;
  inflight = true;
  const body = JSON.stringify(pending); pending = null;
  try { await fetch("/api/set", {method: "POST", headers: {"Content-Type": "application/json"}, body}); }
  catch (e) { $("err").textContent = "Lost connection to the script. Is the Terminal window still open?"; }
  inflight = false;
  if (pending) flush();
}
function slider(id, key) {
  const el = $(id);
  el.addEventListener("input", () => { dragging[id] = true; $(id + "-val").textContent = el.value + "%"; send({[key]: el.value / 100}); });
  const done = () => { dragging[id] = false; };
  el.addEventListener("change", done); el.addEventListener("mouseup", done); el.addEventListener("touchend", done);
}
slider("master", "master"); slider("left", "left"); slider("right", "right");
$("reset").onclick = () => send({reset: true});
$("rescan").onclick = () => send({rescan: true});
$("stop").onclick = () => send({sound: "stop"});
document.querySelectorAll("button[data-side]").forEach(b => b.onclick = () => send({sound: b.dataset.side}));
$("keep").onchange = () => send({keep: $("keep").checked});
$("device").onchange = () => send({device: Number($("device").value)});
$("quit").onclick = async () => { await fetch("/api/quit", {method: "POST"}); document.body.innerHTML = "<main><h1>Speaker Balance</h1><p>Stopped. You can close this tab.</p></main>"; };

const pct = v => Math.round(v * 100) + "%";
const hw = (v, db) => db === null ? "now " + pct(v) : "now " + db.toFixed(1) + " dB";
function render(s) {
  const key = JSON.stringify(s.devices) + s.device;
  if (key !== lastDevices) {
    lastDevices = key;
    $("device").innerHTML = s.devices.map(d => `<option value="${d.id}" ${d.id === s.device ? "selected" : ""}>${d.name}</option>`).join("");
  }
  for (const id of ["master", "left", "right"]) {
    if (!dragging[id]) { $(id).value = Math.round(s[id] * 100); $(id + "-val").textContent = pct(s[id]); }
  }
  $("left-hw").textContent = hw(s.hwLeft, s.leftDb);
  $("right-hw").textContent = hw(s.hwRight, s.rightDb);
  const stereo = s.capability === "stereo";
  $("left").disabled = $("right").disabled = !stereo;
  $("master").disabled = s.capability === "none";
  $("keep").checked = s.keep;
  $("hint").textContent = stereo
    ? "Turn the louder speaker down until both sound the same, then use Overall volume. The ratio you set here is kept as the volume goes up and down."
    : s.capability === "master"
      ? "This device only has a single volume control, so the speakers can't be balanced separately. Try another output device."
      : "This device has no adjustable volume (common for HDMI and optical outputs). Use the knob on the sound system, or pick another output.";
  document.querySelectorAll("button[data-side]").forEach(b => b.classList.toggle("on", b.dataset.side === s.sound));
}
async function poll() {
  try { render(await (await fetch("/api/state")).json()); $("err").textContent = ""; }
  catch (e) { $("err").textContent = "Lost connection to the script. Is the Terminal window still open?"; }
}
poll(); setInterval(poll, 300);
</script></body></html>
"""


def run_gui():
    ctl = Controller()
    sound = TestSound()
    flags = {"quit": False}

    def state():
        return {
            "devices": [{"id": d.id, "name": d.name} for d in ctl.devices],
            "device": ctl.device.id if ctl.device else None,
            "capability": ctl.capability,
            "master": ctl.master, "left": ctl.left_trim, "right": ctl.right_trim,
            "hwLeft": ctl.hw_left, "hwRight": ctl.hw_right,
            "leftDb": ctl.left_db, "rightDb": ctl.right_db,
            "keep": ctl.keep_balance, "sound": sound.side,
        }

    def handle_set(data):
        if "device" in data:
            for d in ctl.devices:
                if d.id == data["device"] and (ctl.device is None or d.id != ctl.device.id):
                    ctl.select(d)
        if data.get("rescan"):
            ctl.refresh_devices()
        if "master" in data:
            ctl.set_master(data["master"])
        if "left" in data:
            ctl.set_left_trim(data["left"])
        if "right" in data:
            ctl.set_right_trim(data["right"])
        if data.get("reset"):
            ctl.reset_trims()
        if "keep" in data:
            ctl.set_keep_balance(data["keep"])
        if "sound" in data:
            if data["sound"] == "stop":
                sound.stop()
            else:
                sound.toggle(data["sound"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/state":
                self._send(json.dumps(state()))
            elif self.path == "/" or self.path.startswith("/?"):
                self._send(PAGE, "text/html")
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if self.path == "/api/set":
                try:
                    handle_set(json.loads(body.decode("utf-8")))
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}))
                    return
                self._send(json.dumps(state()))
            elif self.path == "/api/quit":
                flags["quit"] = True
                self._send("{}")
            else:
                self.send_error(404)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.25
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Speaker Balance is running at %s" % url)
    print("Your browser should open it now. Keep this window open; press Ctrl-C (or the Quit button) to stop.")
    try:
        subprocess.call(["/usr/bin/open", url])
    except Exception:
        print("Open that address in your browser.")
    try:
        while not flags["quit"]:
            server.handle_request()
            ctl.sync_from_hardware()
            sound.tick()
    except KeyboardInterrupt:
        pass
    finally:
        sound.stop()
        server.server_close()
        print("Stopped.")


# ---------------------------------------------------------------------------
# Text mode fallback
# ---------------------------------------------------------------------------

def run_cli():
    ctl = Controller()
    sound = TestSound()
    read = raw_input if sys.version_info[0] < 3 else input  # noqa: F821

    def show():
        ctl.sync_from_hardware()
        print("")
        print("Device : %s  [%s]" % (ctl.device.name if ctl.device else "none", ctl.capability))
        print("Overall: %s" % pct(ctl.master))
        print("Left   : trim %s  (%s)" % (pct(ctl.left_trim), hw_text(ctl.hw_left, ctl.left_db)))
        print("Right  : trim %s  (%s)" % (pct(ctl.right_trim), hw_text(ctl.hw_right, ctl.right_db)))

    print("Speaker Balance (text mode)")
    print("Commands:  v 60   overall 60%     l 70   left trim 70%     r 100  right trim 100%")
    print("           t l|r|b  test sound    s  stop sound          d  pick device     q  quit")
    while True:
        show()
        try:
            line = read("> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            line = ["q"]
        if not line:
            continue
        cmd, arg = line[0].lower(), (line[1] if len(line) > 1 else "")
        try:
            if cmd == "q":
                sound.stop()
                return
            elif cmd == "v":
                ctl.set_master(int(arg) / 100.0)
            elif cmd == "l":
                ctl.set_left_trim(int(arg) / 100.0)
            elif cmd == "r":
                ctl.set_right_trim(int(arg) / 100.0)
            elif cmd == "t":
                sound.play({"l": "left", "r": "right", "b": "both"}.get(arg, "both"))
            elif cmd == "s":
                sound.stop()
            elif cmd == "d":
                ctl.refresh_devices()
                for i, d in enumerate(ctl.devices):
                    print("  %d) %s" % (i + 1, d.name))
                choice = read("number: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(ctl.devices):
                    ctl.select(ctl.devices[int(choice) - 1])
            else:
                print("unknown command")
        except ValueError:
            print("give a number from 0 to 100, e.g.  v 60")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        run_cli()
    else:
        run_gui()
