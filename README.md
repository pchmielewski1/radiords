# radiords

RTL-SDR FM radio GUI for Linux with RDS support.

This app is intentionally a single-file Python/Tkinter GUI: [rtlsdr_fm_radio_gui.py](rtlsdr_fm_radio_gui.py).

## Features

- FM band scanning and station list
- True stereo FM playback (GNU Radio + osmosdr)
- RDS decoding (PS/RT/PTY) via `rtl_fm` + `redsea` (JSON)
- MP3 recording (PCM → `lame`)
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

## Note: “one RTL-SDR at a time”

You cannot use one RTL-SDR dongle concurrently in two pipelines: playback uses osmosdr (GNU Radio), while RDS uses `rtl_fm`.

## License

MIT. See [LICENSE](LICENSE).
