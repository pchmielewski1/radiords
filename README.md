# radiords

RTL-SDR FM radio GUI for Linux with RDS support.

This app is intentionally a single-file Python/Tkinter GUI: [rtlsdr_fm_radio_gui.py](rtlsdr_fm_radio_gui.py).

## Features

- FM band scanning and station list
- True stereo FM playback (GNU Radio + osmosdr)
- Live RDS decoding (PS/RT/PTY) while listening (single dongle) — fixed by using GNU Radio in the playback flowgraph (MPX branch) feeding `redsea` (JSON)
- Recording: MP3 (`lame`) or FLAC (`flac`) — selectable in **Settings… → Recording format**
- Spectrum/plots in the GUI (matplotlib)

## Installation

### `.deb` package (recommended)

Packages are published via **GitHub Releases**.

After downloading the `.deb` file:

```bash
sudo apt install ./radiords_*.deb
```

### From source (dev)

```bash
python3 rtlsdr_fm_radio_gui.py
```

## Requirements

See the full dependency list and notes (GNU Radio, system tools, fonts):
- [docs/installation_requirements.md](docs/installation_requirements.md)

## Note: RTL-SDR device access

By default, live RDS updates during playback use the same GNU Radio flowgraph (single dongle).

This is controlled by the RDS backend setting:

- Settings key: `rds.backend = "gnuradio"` (recommended; enables RDS + playback at the same time)
- Settings key: `rds.enable_updates_during_playback = true`

If you switch the RDS backend to the legacy external pipeline (`rtl_fm` → `redsea`), it cannot run at the same time as playback on a single dongle.

## License

MIT. See [LICENSE](LICENSE).
