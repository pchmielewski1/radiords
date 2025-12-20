# RTL-SDR FM Radio GUI — low-level documentation (for migration)

This document describes how [rtlsdr_fm_radio_gui.py](../rtlsdr_fm_radio_gui.py) works: threads, processes, IPC, data formats, integration points, and the minimal functional contract to re-implement in another language.

## 1) Functional contract (what must be preserved)

- FM band scan (configurable; default preset: “worldwide”) and station database generation in JSON.
- FM playback with **true L/R stereo** (WFM stereo demodulation).
- Optional RDS refresh during playback (`rtl_fm` → `redsea` pipeline).
- MP3 recording (`lame`) while playback is running.
- Visualizations: L/R spectrum (dBFS) + stereo correlation (L vs R scatter) + correlation/balance metric.
- “Settings…” window persisted to `fm_radio_settings.json`.
- Stable shutdown without freezing (no blocking `wait()` in the UI thread).
- Internationalization: switching language in settings updates the entire UI.

## 2) Logical code structure (despite being one file)

### 2.1 `FMStation` (data model)
Responsibilities:
- Stores station fields: `freq`, `ps`, `radiotext`, `rtplus`, `pi`, `prog_type`, `alt_freqs`, `stereo`, `tp`, `ta`, `last_seen`, `rds_count`.
- `update_from_rds(rds_data)` maps RDS decoder JSON into model fields.
- `get_now_playing()` tries to derive “Now playing” from RT+ (decoder-dependent fields).

Migration contract:
- Preserve the data structure and the RDS JSON → model mapping.
- Keep `rtplus` as a `dict` (or equivalent) — not every station transmits it.

### 2.2 `FMDatabase` (station DB persistence)
Responsibilities:
- `load()` / `save()` serialize the DB into JSON in [fm_stations_database.json](../fm_stations_database.json).
- `add_or_update(station)` updates the entry keyed by frequency.
- `get_stations_with_rds()` filters stations with `ps != None` and sorts by `freq`.

Migration contract:
- Preserve the JSON file format (see section 6.1).
- Update keyed by `freq` (float).

### 2.3 `FMRadioGUI` (runtime orchestration + UI)
Responsibilities:
- UI (Tk/ttk) + matplotlib.
- Process management: `play`, `lame`, `rtl_fm`, `redsea`.
- GNU Radio pipeline (osmosdr + `analog.wfm_rcv_pll`) that provides stereo.
- Worker threads: PCM streaming, spectrum, RDS updater, scan.
- Robust shutdown: `_terminate_process()`, `_stop_gnuradio_rx(block=False)`, `on_closing()`.
- i18n: `I18N`, `t(key)`, `ui.language` in settings.

## 3) Runtime dependencies

The full list of installation requirements (including font packages for plots and UI languages) is in: [docs/installation_requirements.md](installation_requirements.md)

### 3.1 Python (libraries)
- Tkinter + ttk
- matplotlib (TkAgg)
- numpy
- GNU Radio: `gnuradio`, `osmosdr`

### 3.2 System tools
- `play` (sox) — odtwarzanie surowego PCM
- `lame` — kodowanie MP3
- `rtl_fm` + `redsea` — dekodowanie RDS
- `amixer` — ustawianie głośności

### 3.3 Fonts (important for matplotlib + i18n)

Note: Tkinter and Matplotlib can use different fonts. To ensure plot labels (legends/titles/axis labels) work for non-Latin scripts (e.g., CJK, Arabic, Hindi, Bengali, Telugu, Tamil, Thai, Gujarati), install the appropriate font packages — see [docs/installation_requirements.md](installation_requirements.md).

## 4) Execution architecture: threads, processes, IPC

### 4.1 Tkinter rule
Tkinter is not thread-safe:
- only the UI thread touches widgets,
- worker threads update UI via `root.after(...)`.

### 4.2 Threads

1) `stream_audio()`
- Reads raw stereo PCM from the GNU Radio pipe (`self._gr_pipe_file`).
- Writes bytes to the `play` process stdin.
- If recording is active, writes the same bytes to `lame` stdin.
- Buffers chunks in `self.audio_buffer` (locked) for FFT.

2) `spectrum_analyzer()`
- Pulls chunks from `self.audio_buffer`.
- Converts interleaved S16 to float, computes FFT (rfft), scales to dBFS.
- Computes stereo correlation (L vs R scatter + metrics).
- Updates the plot via `root.after(0, update_spectrum_plot)`.

3) `rds_updater()`
- Spawns `rtl_fm` and `redsea`, reads JSON lines.
- Updates `self.current_station` and persists into the DB.
- Optional (setting `rds.enable_updates_during_playback`).

4) `scan_fm_band()`
- Iterates `freq` across the currently selected FM band (min/max/step from settings).
- For each frequency tries to capture RDS (`scan_frequency_for_rds`).
- Persists results and refreshes the station list in the GUI.
- Note: scan progress UI updates go through `root.after`.

### 4.3 Audio pipeline (true stereo)

Implementacja w `_start_gnuradio_rx(freq_mhz, gain_db)`:
- `osmosdr.source` (RTL-SDR) z parametrami:
	- `sample_rate = demod_rate_hz`
	- `center_freq = freq_mhz * 1e6`
	- `freq_corr = ppm`
	- `gain = gain_db`
	- `bandwidth = rf_bandwidth_hz`
- `analog.wfm_rcv_pll(demod_rate, audio_decim, deemph_tau)` produkuje dwa kanały float (L i R).
- `float_to_short(scale=32767)` dla obu.
- `blocks.interleave` do formatu interleaved S16_LE.
- `file_descriptor_sink` zapisuje do `os.pipe()`.

Pipe format (contract):
- stereo, interleaved
- signed 16-bit little-endian
- sample rate = `audio_rate_hz` (from settings; must divide `demod_rate_hz`)

### 4.4 Playback and recording

Playback:
- `play` (sox) subprocess with stdin.

Recording:
- `lame` subprocess with stdin.
- `stop_recording()` closes stdin and finalizes LAME in the background (important: MP3 header/tags).

### 4.5 Shutdown (non-blocking UI)

Requirement: clicking the window close button must not freeze the process.

Applied rules:
- `on_closing()`:
	- sets `self._closing=True`
	- flips flags (`scanning=False`, `playing=False`)
	- terminates processes via `_terminate_process()` (SIGTERM/SIGKILL to process group)
	- stops GNU Radio via `_stop_gnuradio_rx(block=False)` (wait happens in background)
	- destroys the window via `root.after(0, root.destroy)`

## 5) i18n (internationalization)

Implementation:
- `TOP25_UI_LANGUAGES`: list of 25 languages (code + Polish name + native name)
- `I18N`: translations dict (currently full PL/EN; other languages selectable with fallback to EN)
- `t(key, **kwargs)`: translation function
- `ui.language` setting in [fm_radio_settings.json](../fm_radio_settings.json)
- changing language in Settings triggers `_apply_language_to_ui()` and refreshes labels

Migration contract:
- all UI text must be keyed (e.g., `scan_band`, `settings_title`)
- runtime switch: no process restart; refresh the current UI

## 6) File formats

### 6.1 Station database: `fm_stations_database.json`
Structure:
- JSON object `{ "freq": station_dict }`
- `station_dict` is produced by `FMStation.to_dict()`

Note: `freq` is a string key in JSON, but represents a float.

### 6.2 Settings: `fm_radio_settings.json`
Structure (groups):
- `fm_band.preset`: FM band preset (range + scan step), e.g. `"worldwide"`, `"us_ca"`, `"japan"`, `"oirt"`
- `ui.language`: e.g. `"pl"`, `"en"`
- `sdr`: `osmosdr_args`, `ppm`, `rf_bandwidth_hz`
- `audio`: `demod_rate_hz`, `audio_rate_hz`, `enable_deemphasis`
- `rds`: `enable_updates_during_playback`, `update_interval_s`
- `spectrum`: `max_hz`, `ymin_dbfs`, `ymax_dbfs`, `time_smoothing_alpha`, `freq_smoothing_bins`, `fps`, `corr_points`, `corr_point_alpha`, `corr_marker_size`

## 7) Performance — where the real cost is

- Stereo DSP runs in GNU Radio (C++), not in Python.
- Python hotspots are typically:
	- FFT + preparing plot data
	- matplotlib rendering in Tk (often the biggest cost and source of UI lag)
- Migration wins usually come from:
	- better rendering (GPU/scenegraph)
	- stronger backend/frontend split
	- a better concurrency model

## 8) Recommended migration strategy

Safest (lowest risk) split:
- DSP backend (GNU Radio in C++ or a separate process) + PCM/RDS export via ZeroMQ/gRPC/IPC
- GUI frontend in your chosen stack (Qt/QML, Avalonia, JavaFX, etc.)

Minimal interfaces (to re-implement 1:1):
- `StartPlayback(freq, gain, settings) -> stream PCM stereo`
- `StopPlayback()`
- `StartRecording(path)` / `StopRecording()`
- `ScanBand(start, end, step) -> events StationFound`
- `RdsUpdate(freq) -> StationData`
- `SettingsLoad/Save`

---

This document is intentionally “low-level”: it should let you reproduce the behavior without reading the entire source code.