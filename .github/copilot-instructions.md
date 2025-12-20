# Copilot instructions (radiords)

## Project shape / big picture
- This repo is a **single-file Tkinter GUI app**: `rtlsdr_fm_radio_gui.py`.
- Data model/persistence are still separated by classes:
  - `FMStation`: RDS fields + mapping from RDS JSON (`update_from_rds`).
  - `FMDatabase`: loads/saves station DB in `fm_stations_database.json`.
  - `FMRadioGUI`: UI + orchestration of threads/processes + DSP wiring.
- Audio path during playback is **GNU Radio → pipe → sox `play`** (stereo S16_LE). RDS is decoded by **`rtl_fm` → `redsea`** (JSON lines).

## How to run (developer workflow)
- Start the app: `python3 rtlsdr_fm_radio_gui.py`.
- It hard-checks these external tools in `main()`: `rtl_fm`, `redsea`, `play`, `amixer`.
- Stereo playback requires GNU Radio + osmosdr Python modules (`from gnuradio import ...; import osmosdr`). If `_GNURADIO_OK` is false, playback should fail early (don’t silently fall back).

## External integration points (don’t break)
- Playback DSP: `_start_gnuradio_rx(freq_mhz, gain_db)` builds an osmosdr → `analog.wfm_rcv_pll` stereo chain and writes **interleaved S16_LE** to an `os.pipe()`.
- Audio sink: `play_station()` launches sox `play` expecting `-r 48k -b 16 -c 2` and the stream loop in `stream_audio()` writes raw PCM to stdin.
- RDS scan + updates:
  - Scan: `scan_frequency_for_rds()` runs `rtl_fm` with `RDS_SAMPLE_RATE=171000` then `redsea -E` and reads JSON lines for `SCAN_TIME`.
  - During playback: `rds_updater()` periodically runs a short-lived `rtl_fm`+`redsea` session.

## Concurrency & UI rules (critical)
- Tkinter is **not thread-safe**. Worker threads must not touch widgets directly; use `root.after(...)` (see `log()`, `_flush_log_queue()`, spectrum updates).
- Shutdown must be non-blocking: `on_closing()` sets `_closing`, flips flags, terminates subprocesses via `_terminate_process()`, and calls `_stop_gnuradio_rx(block=False)`.

## “One RTL-SDR at a time” constraint
- You cannot have two SDR clients concurrently.
  - Playback uses osmosdr (RTL-SDR) via GNU Radio.
  - RDS uses `rtl_fm` (also RTL-SDR).
- Code enforces this by disabling RDS updater during recording (`start_recording()` sets `rds_updating=False`) and by skipping RDS updates while recording (`rds_updater()` checks `self.recording`). Preserve this rule when changing pipelines.

## Recording pipeline (MP3) specifics
- Recording uses `lame` fed with the same PCM bytes as playback (`stream_audio()` writes to both).
- Finalization is intentional: `stop_recording()` closes stdin and waits in a background thread (`_finalize_recording_proc`) so LAME can write headers/tags. Don’t replace with immediate kill.

## On-disk formats and where they live
- Station DB: `fm_stations_database.json` is a JSON object keyed by **stringified frequency** (e.g. "94.0"), each value is `FMStation.to_dict()`.
- Settings: `fm_radio_settings.json` is merged onto defaults (`_default_settings()` + `_load_settings()`). Notable keys:
  - `ui.language`, `recording.output_dir` (relative to repo base or absolute),
  - `audio.demod_rate_hz` must be a multiple of `audio.audio_rate_hz`.
- Debugging: `debug_log()` always appends to `radio_recording_debug.txt` even if GUI logging fails.

## Making changes safely
- When adding UI updates from background work: queue + `root.after` is the existing pattern; don’t call `root.update()` (explicitly avoided in recording).
- When adding/adjusting subprocess usage: keep `start_new_session=True` so `_terminate_process()` can kill the whole process group (`os.killpg`).
- When extending RDS mapping: prefer adding keys in `FMStation.update_from_rds()` (it already supports multiple RT+ key variants).

References: `rtlsdr_fm_radio_gui.py` and `docs/rtlsdr_fm_radio_gui_low_level_migration.md`.
