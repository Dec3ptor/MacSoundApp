# Speaker Balance

A small macOS app for sound systems where one speaker is quieter than the other.

You set a **balance** once (turn the louder speaker down until both sound the
same), and from then on the **Overall volume** slider moves both speakers
together while keeping that ratio. Raising the volume raises both speakers
equally, instead of making the loud one even louder.

Runs on macOS 11 Big Sur and later (tested target: MacBook Pro Mid 2014, 11.7.9).

## How it works

macOS exposes a separate hardware volume control for the left and right
channel of most outputs (the headphone jack, built-in speakers, USB audio).
The app writes `overall × trim` to each channel:

| | Left | Right |
|---|---|---|
| Trim (set once) | 70 % | 100 % |
| Overall 50 % → hardware | 35 % | 50 % |
| Overall 100 % → hardware | 70 % | 100 % |

Features:

- Per-speaker trim sliders with a live read-out of what the hardware reports (in dB when the driver provides it).
- A linked overall volume slider.
- A pink-noise **test sound** for the left, right or both speakers so you can match them by ear.
- **Keep balance**: if the volume keys, the menu bar slider, or a sleep/wake cycle reset the channels, the app re-applies your balance at the new level.
- Trims are remembered per output device, and the app follows the system default output when you plug things in or out.

HDMI and optical outputs have no hardware volume at all, so the app can only
tell you that; use the knob on the amplifier for those.

## Building

You need the Swift toolchain. On Big Sur that means **Xcode 13.2.1** (the last
Xcode that runs on 11.x) or the matching Command Line Tools:

```
xcode-select --install
```

Then, in Terminal:

```
cd MacSoundApp
./build-app.sh
open build/SpeakerBalance.app
```

Drag `build/SpeakerBalance.app` into `/Applications` if you want to keep it.
The bundle is ad-hoc signed, so it opens without any Gatekeeper fuss on the
machine it was built on.

You can also open `Package.swift` in Xcode and press Run.

## Notes

- The app does not add anything to the audio path. It only drives the same
  per-channel controls the Balance slider in System Preferences → Sound uses,
  so there is no latency and nothing keeps running when the app is closed.
- With *Keep balance* on, changing the Balance slider in System Preferences
  will be undone by the app. Turn the option off if you want to use that
  slider instead.
