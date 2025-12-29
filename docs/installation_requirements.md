# RTL-SDR FM Radio GUI — installation requirements

This document collects **everything** you need installed for the app to work correctly (startup, playback, RDS, recording, plots, and all UI languages).

## 1) Hardware requirements

- An RTL-SDR compatible USB dongle.
- An FM antenna.

## 2) System requirements

- Linux (tested on Kali/Debian rolling, but the packages are analogous on Ubuntu/Debian).

## 3) Python requirements

The app is a single file: [rtlsdr_fm_radio_gui.py](../rtlsdr_fm_radio_gui.py)

### 3.1 Python and libraries

- Python 3
- Tkinter (`tkinter`, `ttk`) — GUI
- `numpy`
- `matplotlib` (TkAgg backend)

Note: on many distributions, Tkinter and matplotlib are delivered as separate system packages.

### 3.2 GNU Radio / osmosdr (required for stereo playback)

Stereo playback uses GNU Radio + the Python `osmosdr` module. If these modules are missing, the app **should refuse to start playback** (there is no silent fallback).

In the code this is checked via `from gnuradio import ...` and `import osmosdr`.

## 4) System tools (required)

In `main()` the app checks that required commands are available in `PATH`. For full functionality you need:

- `redsea` — RDS JSON decoder (used for scan and live RDS decoding)
- `play` — SoX (plays raw PCM)

Optional (feature-dependent):

- `rtl_fm` — from RTL-SDR tools (used by the legacy external RDS backend and some scan modes)

For **recording**, you also need an encoder (depending on the selected recording format):

- `lame` — MP3 recording
- `flac` — FLAC recording (lossless)

Without these tools, parts of the app will be unavailable.

## 5) Fonts for UI languages + plots (matplotlib)

Tkinter and Matplotlib **do not have to use the same fonts**. Even if Tkinter renders characters correctly, Matplotlib (legends/titles/axis labels) may show tofu boxes or warn about missing glyphs.

To make plot labels work for all languages from the `TOP25_UI_LANGUAGES` list (including CJK, Arabic, Hindi, Bengali, Telugu, Tamil, Thai, Gujarati), the packages below are recommended.

### 5.1 Font packages (Debian/Ubuntu/Kali)

Minimum for CJK (Chinese/Japanese/Korean):

- `fonts-noto-cjk`

Broad Unicode coverage (many scripts):

- `fonts-noto-core`
- `fonts-noto-extra`

Additional fonts that help in practice for mixed text (local script + ASCII like `dBFS`, `L/R`, numbers):

- `fonts-noto-ui-core`
- `fonts-noto-ui-extra`
- Devanagari/Telugu/Tamil/Gujarati (often better than Noto for mixed ASCII):
  - `fonts-lohit-deva`
  - `fonts-lohit-deva-marathi` (optional)
  - `fonts-lohit-telu`
  - `fonts-lohit-taml`
  - `fonts-lohit-gujr`
- Bengali:
  - `fonts-beng-extra` (e.g. “Likhan”)
  - `fonts-lohit-beng-bengali`
- Tamil:
  - `fonts-meera-inimai`
- Thai:
  - `fonts-thai-tlwg`
- Gujarati + Latin:
  - `fonts-yrsa-rasa` (the “Rasa” font includes Gujarati + ASCII)

Example install (Kali/Debian/Ubuntu):

```bash
sudo apt-get update
sudo apt-get install -y \
  fonts-noto-cjk fonts-noto-core fonts-noto-extra \
  fonts-noto-ui-core fonts-noto-ui-extra \
  fonts-lohit-deva fonts-lohit-telu fonts-lohit-taml fonts-lohit-gujr \
  fonts-lohit-beng-bengali fonts-beng-extra \
  fonts-meera-inimai fonts-thai-tlwg fonts-yrsa-rasa
```

After installing fonts:

- close and re-open the app (Matplotlib loads fonts on startup),
- if you still see old fonts, clear the Matplotlib cache:

```bash
rm -rf ~/.cache/matplotlib
```

## 6) RTL-SDR drivers / permissions

If `rtl_fm` cannot see the device, or GNU Radio playback does not work, it is usually due to:

- missing USB permissions (udev rules),
- a conflict with the DVB driver (sometimes you need to detach/blacklist a module depending on the dongle),
- `osmosdr` arguments (e.g. selecting the device).

## 7) Quick mapping: what each feature needs

- **GUI + station list + log**: Python + Tkinter + matplotlib + numpy
- **Stereo playback**: GNU Radio + osmosdr + `play` + working RTL-SDR access
- **Live RDS during playback (default)**: GNU Radio + `redsea` + working RTL-SDR access
- **RDS scan / legacy external backend**: `rtl_fm` + `redsea` + working RTL-SDR access
- **Recording (MP3)**: `lame`
- **Recording (FLAC, lossless)**: `flac`
- **Plots in non-Latin scripts**: font packages from section 5

## 8) FM broadcast band standards (ranges and channel steps)

Not every country uses the 88–108 MHz band. Differences include:

- **frequency range** (min/max),
- **channel raster / step** (e.g. 100 kHz vs 200 kHz).

Common variants (practical scanning presets):

- **Worldwide (most common):** 87.5–108.0 MHz, 100 kHz step
- **USA/Canada:** 87.9–107.9 MHz, 200 kHz step (channels like 87.9, 88.1, 88.3…)
- **Japan:** 76.0–95.0 MHz (often 100 kHz); sometimes extended to 99.0 MHz
- **Brazil:** band may be extended downward; scanning ~76.1–108.0 MHz works well in practice
- **OIRT (legacy, former Eastern bloc):** 65.8–74.0 MHz

Note: local frequency plans can have extra nuances (spacing, exceptions, etc.), but the presets above cover typical RTL-SDR use cases.

## 9) App settings: selecting an FM band preset

In **Settings…** there is an **FM band** preset that affects:

- scan range (min/max),
- scan step,
- validation of a manually entered frequency.

The setting is saved in `fm_radio_settings.json` under:

```json
{
  "fm_band": {
    "preset": "worldwide"
  }
}
```

Allowed `preset` values:

- `worldwide`
- `us_ca`
- `japan`
- `japan_wide`
- `brazil`
- `oirt`

## 9.1) UI theme (Dark mode)

In **Settings…** the **Dark mode** toggle changes the UI look (ttk/Tk) and plots (Matplotlib).

The setting is saved in `fm_radio_settings.json` under:

```json
{
  "ui": {
    "language": "pl",
    "theme": "dark"
  }
}
```

Allowed `theme` values:

- `light`
- `dark`

Note: after changing the theme, click **Apply**. The theme should switch immediately.

## 9.2) Window layout (readability)

In newer versions, the main window proportions were adjusted so that:

- the station list is taller,
- the log is taller and easier to read,
- the “Current RDS / station info” panel is more compact when there is little data.

## 10) Installing as a `.deb` package (Debian/Kali/Ubuntu)

This repo can build a `.deb` package so you can install the app system-wide and run it as `radiords`.

### 10.1 Building the package (from the repo)

The package is generated into `dist/` (e.g. `dist/radiords_…_all.deb`).

### 10.2 Installation

Install the package:

```bash
sudo dpkg -i dist/radiords_*_all.deb
```

If the system reports missing dependencies, install them:

```bash
sudo apt-get -f install
```

Run:

```bash
radiords
```

## 11) Where the app stores files (important for system-wide installs)

When running from the repo (the folder is writable), the app uses files next to the script.

When installed system-wide (e.g. under `/usr/lib/...`), the app directory is usually **not writable** — then the app automatically switches to per-user XDG directories:

- Ustawienia: `~/.config/radiords/fm_radio_settings.json`
- Baza stacji: `~/.local/share/radiords/fm_stations_database.json`
- Debug log: `~/.local/state/radiords/radio_recording_debug.txt`
- Domyślny katalog nagrań: `~/.local/share/radiords/recordings/` (lub zgodnie z `recording.output_dir`)

- Settings: `~/.config/radiords/fm_radio_settings.json`
- Station database: `~/.local/share/radiords/fm_stations_database.json`
- Debug log: `~/.local/state/radiords/radio_recording_debug.txt`
- Default recordings directory: `~/.local/share/radiords/recordings/` (or as configured via `recording.output_dir`)
