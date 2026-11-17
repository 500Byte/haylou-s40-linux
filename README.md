# haylou-s40-linux

Native Linux controller for **Haylou S40** headphones: **GTK4 / Libadwaita GUI**, **CLI**, **PipeWire codec switching**, and **USB DAC mode detection**.

Talks directly to the headset over **Classic Bluetooth RFCOMM (Channel 10)** using Python's standard library (`socket.AF_BLUETOOTH`) — no `bleak` or third-party Bluetooth dependencies required.

---

## Features

- **Noise Control (ANC):** Normal (Off), Active Noise Cancellation, Transparency (Ambient), Adaptive ANC, and Wind Noise Reduction.
- **Audio & Low Latency:** Game Mode, 360° Spatial Audio (Off / Static / Dynamic) + Scene presets, Hardware DSP EQ (5 basic presets + 3 Sound Market parametric presets).
- **PipeWire / BlueZ Codec Switcher:** Switch between `AAC`, `SBC-XQ`, `SBC`, and `LDAC` (when enabled in firmware).
- **Device Settings:** Multipoint (Dual-Device), Auto-shutdown timer (`30m` / `1h` / `3h` / `5h` / `Never`), and firmware LDAC toggle.
- **USB DAC Mode (`3654:4a55`):** Automatically detects wired USB mode (`PCM 48 kHz Full-Duplex`) and disables RFCOMM-only controls cleanly.
- **Collision-Free IPC:** While the GUI is open, CLI queries/commands delegate through `$XDG_RUNTIME_DIR/haylou-s40.sock` so external bars (Waybar / Quickshell) never fight for the RFCOMM socket.

---

## Requirements

- Linux with BlueZ (`bluetoothctl`) and PipeWire / WirePlumber (`pactl`)
- Python 3.10+ (stdlib `socket.AF_BLUETOOTH`)
- PyGObject + GTK4 + Libadwaita (for `--gui`)

---

## Installation

```bash
chmod +x haylou-s40.py
mkdir -p ~/.local/bin
ln -sf "$(pwd)/haylou-s40.py" ~/.local/bin/haylou-s40
```

Optional desktop launcher & WirePlumber USB DAC fix (fixes `broken mixer GET_CUR` on Jieli `3654:4a55` via `api.alsa.soft-mixer = true` and renames the device):

```bash
cp extras/haylou-s40.desktop ~/.local/share/applications/
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp extras/51-haylou-s40-usb.conf ~/.config/wireplumber/wireplumber.conf.d/
systemctl --user restart wireplumber
```

### Optional headless daemon

Keeps the RFCOMM channel open and answers CLI/bar queries over the UNIX socket, so integrations never pay a Bluetooth reconnect per poll:

```bash
mkdir -p ~/.config/systemd/user
cp extras/haylou-s40.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now haylou-s40
```

While the daemon runs it owns the socket and the RFCOMM link: the GUI and the CLI become clients of it (the window never opens a second Bluetooth connection). Without the daemon everything still works; each CLI call connects to the headset, reads, and disconnects.


---

## CLI Usage

```bash
haylou-s40 --gui                  # Launch GTK4/Libadwaita window
haylou-s40 --serve                # Headless daemon (UNIX socket, no GTK)
haylou-s40 status                 # Human-readable status
haylou-s40 --json                 # JSON output (for Waybar / Quickshell)
haylou-s40 anc <off|anc|transp|adaptive|cycle>
haylou-s40 game <on|off|toggle>
haylou-s40 wind <on|off|toggle>
haylou-s40 multi <on|off|toggle>
haylou-s40 spatial <off|static|dynamic>
haylou-s40 scene <music|exercise|movie>
haylou-s40 shutdown <30m|1h|3h|5h|never>
haylou-s40 eq <default|bass|rock|soft|classical|acoustic|soft_imm|bass_enh>
haylou-s40 codec <aac|sbc_xq|sbc|ldac>
haylou-s40 ldac <on|off>
haylou-s40 preset <focus|music|gaming|street>
```

`--json` returns the whole state, including `transport` (`bluetooth`/`usb`), `sample_rate`, `available_profiles` and the `ok`/`message` pair, so a bar can build codec lists from real data.

---

## Development

```bash
python3 test_protocol.py   # frames, TLV decoders, state mapping and PipeWire JSON
```

---

## Credits

Protocol reverse-engineering inspired by and verified against:
- [victommasi/haylou-s40-windows](https://github.com/victommasi/haylou-s40-windows)
- [santiagorelancio/haylou-s40-windows](https://github.com/santiagorelancio/haylou-s40-windows)
- [Flopster101/HLControl](https://github.com/Flopster101/HLControl)
