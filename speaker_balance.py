#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Speaker Balance - no-install version.

Balances a stereo sound system where one speaker is quieter than the other,
then lets you raise or lower the overall volume while that balance is kept.

Runs on the Python that ships with macOS (2.7 on Big Sur, or any python3).
No Xcode, no extra packages. It drives the same per-channel CoreAudio volume
controls that the Balance slider in System Preferences uses.

    python speaker_balance.py          # window with sliders
    python speaker_balance.py --cli    # text mode (if Tk is unavailable)
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
# GUI (Tk)
# ---------------------------------------------------------------------------

def pct(v):
    return "%d%%" % int(round(v * 100))


def hw_text(hw, db):
    return "now %.1f dB" % db if db is not None else "now " + pct(hw)


def run_gui():
    try:
        import tkinter as tk
    except ImportError:
        import Tkinter as tk

    ctl = Controller()
    sound = TestSound()
    root = tk.Tk()
    root.title("Speaker Balance")
    root.resizable(False, False)
    state = {"updating": False}

    pad = {"padx": 12, "pady": 4}

    # Device row
    frm = tk.Frame(root)
    frm.pack(fill="x", **pad)
    tk.Label(frm, text="Output device:", font=("", 13, "bold")).pack(side="left")
    device_var = tk.StringVar()
    device_menu = tk.OptionMenu(frm, device_var, "")
    device_menu.pack(side="left", fill="x", expand=True, padx=6)

    def rebuild_device_menu():
        menu = device_menu["menu"]
        menu.delete(0, "end")
        for d in ctl.devices:
            menu.add_command(label=d.name, command=lambda dev=d: choose_device(dev))
        device_var.set(ctl.device.name if ctl.device else "(no output device)")

    def choose_device(dev):
        if ctl.device is None or dev.id != ctl.device.id:
            ctl.select(dev)
        refresh_ui()

    def refresh_devices():
        ctl.refresh_devices()
        rebuild_device_menu()
        refresh_ui()

    tk.Button(frm, text="Rescan", command=refresh_devices).pack(side="left")

    # Overall volume
    tk.Label(root, text="Overall volume", font=("", 13, "bold"), anchor="w").pack(fill="x", **pad)
    frm = tk.Frame(root)
    frm.pack(fill="x", **pad)
    master_scale = tk.Scale(frm, from_=0, to=100, orient="horizontal", length=360, showvalue=0,
                            command=lambda v: on_master(v))
    master_scale.pack(side="left")
    master_label = tk.Label(frm, width=5, anchor="e")
    master_label.pack(side="left")
    tk.Label(root, text="Raises or lowers both speakers together while keeping the balance below.",
             fg="gray40", anchor="w").pack(fill="x", padx=12)

    # Balance
    frm = tk.Frame(root)
    frm.pack(fill="x", **pad)
    tk.Label(frm, text="Speaker balance", font=("", 13, "bold")).pack(side="left")
    tk.Button(frm, text="Reset", command=lambda: (ctl.reset_trims(), refresh_ui())).pack(side="right")

    def channel_row(title, cmd):
        f = tk.Frame(root)
        f.pack(fill="x", **pad)
        tk.Label(f, text=title, width=6, anchor="w").pack(side="left")
        s = tk.Scale(f, from_=0, to=100, orient="horizontal", length=300, showvalue=0, command=cmd)
        s.pack(side="left")
        p = tk.Label(f, width=5, anchor="e")
        p.pack(side="left")
        h = tk.Label(f, width=14, anchor="e", fg="gray40")
        h.pack(side="left")
        return s, p, h

    left_scale, left_label, left_hw = channel_row("Left", lambda v: on_trim("left", v))
    right_scale, right_label, right_hw = channel_row("Right", lambda v: on_trim("right", v))
    hint = tk.Label(root, fg="gray40", anchor="w", justify="left", wraplength=470)
    hint.pack(fill="x", padx=12)

    # Test sound
    tk.Label(root, text="Test sound (pink noise)", font=("", 13, "bold"), anchor="w").pack(fill="x", **pad)
    frm = tk.Frame(root)
    frm.pack(fill="x", **pad)
    sound_buttons = {}
    for side, title in (("left", "Left"), ("right", "Right"), ("both", "Both")):
        b = tk.Button(frm, text=title, width=7, command=lambda s=side: (sound.toggle(s), refresh_ui()))
        b.pack(side="left")
        sound_buttons[side] = b
    tk.Button(frm, text="Stop", width=7, command=lambda: (sound.stop(), refresh_ui())).pack(side="left")
    tk.Label(root, text="Plays through the system default output. Use it one side at a time to match the speakers by ear.",
             fg="gray40", anchor="w", justify="left", wraplength=470).pack(fill="x", padx=12)

    # Options
    keep_var = tk.IntVar(value=1 if ctl.keep_balance else 0)
    tk.Checkbutton(root, text="Keep balance when the volume is changed elsewhere (volume keys, menu bar)",
                   variable=keep_var, command=lambda: (ctl.set_keep_balance(keep_var.get()), refresh_ui()),
                   anchor="w", justify="left", wraplength=470).pack(fill="x", **pad)

    status = tk.Label(root, fg="gray40", anchor="w")
    status.pack(fill="x", padx=12, pady=(0, 10))

    def on_master(v):
        if state["updating"]:
            return
        ctl.set_master(int(v) / 100.0)
        refresh_labels()

    def on_trim(side, v):
        if state["updating"]:
            return
        (ctl.set_left_trim if side == "left" else ctl.set_right_trim)(int(v) / 100.0)
        refresh_labels()

    def refresh_labels():
        master_label["text"] = pct(ctl.master)
        left_label["text"] = pct(ctl.left_trim)
        right_label["text"] = pct(ctl.right_trim)
        left_hw["text"] = hw_text(ctl.hw_left, ctl.left_db)
        right_hw["text"] = hw_text(ctl.hw_right, ctl.right_db)
        for side, b in sound_buttons.items():
            b["relief"] = "sunken" if sound.side == side else "raised"

    def refresh_ui():
        state["updating"] = True
        try:
            master_scale.set(int(round(ctl.master * 100)))
            left_scale.set(int(round(ctl.left_trim * 100)))
            right_scale.set(int(round(ctl.right_trim * 100)))
        finally:
            state["updating"] = False
        stereo = ctl.capability == Controller.STEREO
        for w in (left_scale, right_scale):
            w["state"] = "normal" if stereo else "disabled"
        master_scale["state"] = "disabled" if ctl.capability == Controller.UNSUPPORTED else "normal"
        if stereo:
            hint["text"] = ("Turn the louder speaker down until both sound the same, then use Overall volume. "
                            "The ratio you set here is kept as the volume goes up and down.")
        elif ctl.capability == Controller.MASTER_ONLY:
            hint["text"] = "This device only has a single volume control, so the speakers can't be balanced separately."
        else:
            hint["text"] = "This device has no adjustable volume (common for HDMI and optical outputs)."
        status["text"] = "Settings saved per device in ~/Library/Application Support/SpeakerBalance"
        refresh_labels()

    def poll():
        sound.tick()
        if ctl.sync_from_hardware():
            refresh_ui()
        else:
            refresh_labels()
        root.after(250, poll)

    def on_close():
        sound.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    rebuild_device_menu()
    refresh_ui()
    root.after(250, poll)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()


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
        try:
            run_gui()
        except ImportError:
            print("Tk is not available, falling back to text mode.")
            run_cli()
