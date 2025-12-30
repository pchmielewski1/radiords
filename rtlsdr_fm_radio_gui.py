#!/usr/bin/env python3
"""RTL-SDR FM Radio - graphical user interface.

Scans the FM band, decodes RDS, and lets you listen to stations with volume control.
"""

import subprocess
import json
import os
import time
import threading
from threading import Lock
from datetime import datetime
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import select
from copy import deepcopy
import signal
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import struct
import math

# GNU Radio (for true L/R stereo)
try:
    from gnuradio import gr, blocks, analog, filter
    import osmosdr
    _GNURADIO_OK = True
except Exception:
    _GNURADIO_OK = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_ID = "radiords"


def _xdg_app_dir(env_var: str, fallback_rel: str) -> str:
    base = os.environ.get(env_var)
    if base:
        return os.path.join(base, APP_ID)
    return os.path.join(os.path.expanduser("~"), fallback_rel, APP_ID)


def _is_writable_dir(path: str) -> bool:
    try:
        return os.path.isdir(path) and os.access(path, os.W_OK)
    except Exception:
        return False


# When running from a source checkout (repo folder), BASE_DIR is typically writable.
# When installed system-wide (e.g. /usr/lib/...), it won't be writable; then we use XDG dirs.
DEV_MODE = _is_writable_dir(BASE_DIR)

if DEV_MODE:
    APP_CONFIG_DIR = BASE_DIR
    APP_DATA_DIR = BASE_DIR
    APP_STATE_DIR = BASE_DIR
else:
    APP_CONFIG_DIR = _xdg_app_dir("XDG_CONFIG_HOME", ".config")
    APP_DATA_DIR = _xdg_app_dir("XDG_DATA_HOME", ".local/share")
    APP_STATE_DIR = _xdg_app_dir("XDG_STATE_HOME", ".local/state")

    for _p in (APP_CONFIG_DIR, APP_DATA_DIR, APP_STATE_DIR):
        try:
            os.makedirs(_p, exist_ok=True)
        except Exception:
            pass

# DEBUG LOG FILE - ALWAYS WRITE HERE
DEBUG_LOG_FILE = os.path.join(APP_STATE_DIR, "radio_recording_debug.txt")

def debug_log(msg):
    """Always append debug logs to a file, regardless of GUI state."""
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{timestamp}] {msg}\n"
    try:
        with open(DEBUG_LOG_FILE, "a") as f:
            f.write(line)
    except:
        pass
    print(line.strip())  # Also to stdout

# Configuration
FM_START = 88.0
FM_END = 108.0
FM_STEP = 0.1
SCAN_TIME = 5
RDS_SAMPLE_RATE = 171000
RTL_GAIN = 49.6

# FM broadcast band presets.
# NOTE: Countries can differ by band edges and channel raster.
# We keep a small set of practical presets for scanning/validation.
FM_BAND_PRESETS = {
    # Most of the world (ITU-R Region 1/2/3) commonly uses 87.5–108.0 MHz.
    "worldwide": {"min_khz": 87500, "max_khz": 108000, "step_khz": 100},
    # North America: stations are on odd 0.2 MHz channels (e.g., 87.9, 88.1, ... 107.9).
    "us_ca": {"min_khz": 87900, "max_khz": 107900, "step_khz": 200},
    # Japan: historically 76–90; currently commonly 76–95 (with an extended band in some contexts).
    "japan": {"min_khz": 76000, "max_khz": 95000, "step_khz": 100},
    "japan_wide": {"min_khz": 76000, "max_khz": 99000, "step_khz": 100},
    # Brazil: extended down to ~76 MHz (varies by locality/plan); scanning 76.1–108 works in practice.
    "brazil": {"min_khz": 76100, "max_khz": 108000, "step_khz": 100},
    # OIRT (legacy, Eastern Europe/USSR): 65.8–74.0 MHz.
    "oirt": {"min_khz": 65800, "max_khz": 74000, "step_khz": 100},
}

DEFAULT_FM_BAND_PRESET = "worldwide"

# Audio spectrum (what we show on the X axis)
# FM baseband audio (after demod) is mostly meaningful up to ~15 kHz; use 16 kHz as a readable limit.
SPECTRUM_MAX_HZ = 16000
ENABLE_DEEMPHASIS = True
DB_FILE = os.path.join(BASE_DIR, "fm_stations_database.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "fm_radio_settings.json")

if not DEV_MODE:
    DB_FILE = os.path.join(APP_DATA_DIR, "fm_stations_database.json")
    SETTINGS_FILE = os.path.join(APP_CONFIG_DIR, "fm_radio_settings.json")


# UI i18n
# Top-25 popular spoken languages (for UI selection). Values: (code, polish_name, native_name)
TOP25_UI_LANGUAGES = [
    ("en", "Angielski", "English"),
    ("zh", "Chiński (mandaryński)", "中文 (普通话)"),
    ("hi", "Hindi", "हिन्दी"),
    ("es", "Hiszpański", "Español"),
    ("fr", "Francuski", "Français"),
    ("ar", "Arabski", "العربية"),
    ("bn", "Bengalski", "বাংলা"),
    ("pt", "Portugalski", "Português"),
    ("ru", "Rosyjski", "Русский"),
    ("ur", "Urdu", "اردو"),
    ("id", "Indonezyjski", "Bahasa Indonesia"),
    ("de", "Niemiecki", "Deutsch"),
    ("ja", "Japoński", "日本語"),
    ("sw", "Suahili", "Kiswahili"),
    ("mr", "Marathi", "मराठी"),
    ("te", "Telugu", "తెలుగు"),
    ("tr", "Turecki", "Türkçe"),
    ("ta", "Tamilski", "தமிழ்"),
    ("vi", "Wietnamski", "Tiếng Việt"),
    ("ko", "Koreański", "한국어"),
    ("it", "Włoski", "Italiano"),
    ("th", "Tajski", "ไทย"),
    ("gu", "Gudźarati", "ગુજરાતી"),
    ("pl", "Polski", "Polski"),
    ("fa", "Perski", "فارسی"),
]


I18N = {
    "pl": {
        "app_title": "RTL-SDR FM Radio with RDS",
        "title": "FM Radio z RDS",
        "status_ready": "Gotowy",
        "manual_tuning": "Ręczne strojenie",
        "frequency_mhz": "Częstotliwość (MHz):",
        "tune": "Strojenie",
        "save": "Zapisz",
        "stations": "Stacje FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Stacja",
        "station_info": "Informacje o stacji",
        "scan_band": "Skanuj pasmo FM",
        "play": "Odtwarzaj",
        "stop": "Stop",
        "record_start": "Rozpocznij nagrywanie",
        "record_stop": "Zatrzymaj nagrywanie",
        "sdr_audio_panel": "Ustawienia SDR i audio",
        "gain": "Gain RTL-SDR:",
        "volume": "Głośność:",
        "settings": "Ustawienia...",
        "log": "Log",
        "viz": "Wizualizacja audio",
        "spec_title": "Spektrum (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Lewy",
        "right": "Prawy",
        "corr_title": "Korelacja stereo",
        "corr_xlabel": "Lewy (L)",
        "corr_ylabel": "Prawy (R)",
        "settings_title": "Ustawienia",
        "apply": "Zastosuj",
        "close": "Zamknij",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spektrum",
        "group_ui": "Interfejs",
        "language": "Język:",
        "recordings_dir": "Katalog nagrań:",
        "recording_format": "Format nagrania:",
        "dark_mode": "Tryb ciemny:",
        "fm_band": "Pasmo FM:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "Deemphasis (50 µs)",
        "rds_updates": "Aktualizuj RDS podczas odtwarzania",
        "interval_s": "Interwał (s):",
        "max_hz": "Max Hz:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Wygładz. czas:",
        "smooth_freq": "Wygładz. freq:",
        "fps": "FPS:",
        "corr_points": "Corr punkty:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr rozmiar:",
        "err": "Błąd",
        "warn": "Uwaga",
        "info": "Info",
        "invalid_settings": "Nieprawidłowe ustawienia: {e}",
        "apply_now_title": "Zastosować teraz?",
        "apply_now_msg": "Te zmiany wymagają restartu odtwarzania. Zrestartować teraz?",
        "scan_already": "Skanowanie już trwa",
        "pick_station": "Wybierz stację z listy",
        "station_not_found": "Nie znaleziono danych stacji",
        "need_playback_first": "Najpierw włącz odtwarzanie stacji",
        "missing_recording_encoder": "Brak enkodera do nagrywania ({tool}). Zainstaluj go, aby nagrywać w formacie {format}.",
        "bad_freq": "Nieprawidłowa częstotliwość",
        "freq_out_of_range": "Częstotliwość poza zakresem {min:.1f}-{max:.1f} MHz",
        "playing": "▶ Odtwarzanie: {name}",
        "stopped": "⏹ Zatrzymano",
        "scanning": "🔍 Skanowanie...",
        "scanning_progress": "🔍 Skanowanie: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Znaleziono {found} stacji",
        "settings_saved": "Ustawienia zapisane",
        "now_playing": "Teraz leci: {text}",
        "unknown": "Nieznane",

        # Settings validation errors (keep placeholders)
        "err_demod_audio_positive": "demod_rate/audio_rate muszą być > 0",
        "err_demod_multiple_audio": "demod_rate musi być wielokrotnością audio_rate",
        "err_ymax_gt_ymin": "Y max musi być > Y min",
        "err_smooth_time_range": "Wygładz. czas musi być w [0..1]",
        "err_smooth_freq_range": "Wygładz. freq musi być w [0..10]",
        "err_fps_range": "FPS musi być w [10..120]",
        "err_corr_points_range": "Corr punkty muszą być w [64..2048]",
        "err_corr_alpha_range": "Corr alpha musi być w [0.05..1]",
        "err_corr_size_range": "Corr rozmiar musi być w [1..8]",

        # Recording/log helper strings (keep placeholders)
        "recording_log": "Nagrywanie: {file}",
        "recording_status": "Nagrywanie: {file} ({size_mb:.2f} MB) | wejście PCM: {mb_in:.2f} MB",
        "record_saved": "Zapisano: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Plik zapisany: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Zatrzymano nagrywanie",
        "recording_file_prefix": "nagranie",
        "cannot_start_recording": "Nie można rozpocząć nagrywania: {e}",

        # Log strings
        "log_no_stations": "Brak stacji w bazie. Naciśnij 'Skanuj pasmo FM'.",
        "log_apply_gain": "Zastosowanie gain: {gain} dB",
        "log_playing": "Odtwarzanie: {freq:.1f} MHz - {ps}",
        "log_gain": "RTL-SDR Gain: {gain} dB",
        "log_playback_error": "Błąd odtwarzania: {e}",
        "log_playback_stopped": "Zatrzymano odtwarzanie",
        "log_record_error": "Błąd nagrywania: {e}",
        "log_stream_error": "Błąd streamu audio: {e}",
        "log_rds_updated": "RDS zaktualizowane: {ps}",
        "log_spectrum_error": "Błąd spektrum: {e}",
        "log_scan_start": "Rozpoczynam skanowanie pasma FM {min:.1f}-{max:.1f} MHz...",
        "log_scan_step": "[{scanned}/{total}] Skanowanie {freq:.1f} MHz...",
        "log_scan_found": "  ✓ Znaleziono: {ps}",
        "log_scan_error": "Błąd skanowania: {e}",
        "log_scan_done": "Skanowanie zakończone: znaleziono {found} stacji",
        "log_scan_freq_error": "  ✗ Błąd: {e}",
        "log_settings_save_error": "Nie można zapisać ustawień: {e}",
    },
    "en": {
        "app_title": "RTL-SDR FM Radio with RDS",
        "title": "FM Radio with RDS",
        "status_ready": "Ready",
        "manual_tuning": "Manual tuning",
        "frequency_mhz": "Frequency (MHz):",
        "tune": "Tune",
        "save": "Save",
        "stations": "FM stations",
        "stations_col_freq": "MHz",
        "stations_col_name": "Station",
        "station_info": "Station info",
        "scan_band": "Scan FM band",
        "play": "Play",
        "stop": "Stop",
        "record_start": "Start recording",
        "record_stop": "Stop recording",
        "sdr_audio_panel": "SDR and audio",
        "gain": "RTL-SDR gain:",
        "volume": "Volume:",
        "settings": "Settings...",
        "log": "Log",
        "viz": "Audio visualization",
        "spec_title": "Spectrum (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Left",
        "right": "Right",
        "corr_title": "Stereo correlation",
        "corr_xlabel": "Left (L)",
        "corr_ylabel": "Right (R)",
        "settings_title": "Settings",
        "apply": "Apply",
        "close": "Close",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spectrum",
        "group_ui": "UI",
        "language": "Language:",
        "recordings_dir": "Recordings folder:",
        "recording_format": "Recording format:",
        "dark_mode": "Dark mode:",
        "fm_band": "FM band:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-emphasis (50 µs)",
        "rds_updates": "Update RDS during playback",
        "interval_s": "Interval (s):",
        "max_hz": "Max Hz:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Time smooth:",
        "smooth_freq": "Freq smooth:",
        "fps": "FPS:",
        "corr_points": "Corr points:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr size:",
        "err": "Error",
        "warn": "Warning",
        "info": "Info",
        "invalid_settings": "Invalid settings: {e}",
        "apply_now_title": "Apply now?",
        "apply_now_msg": "These changes require restarting playback. Restart now?",
        "scan_already": "Scan already running",
        "pick_station": "Select a station from the list",
        "station_not_found": "Station data not found",
        "need_playback_first": "Start playback first",
        "missing_recording_encoder": "Missing recording encoder ({tool}). Install it to record in {format}.",
        "bad_freq": "Invalid frequency",
        "freq_out_of_range": "Frequency out of range {min:.1f}-{max:.1f} MHz",
        "playing": "▶ Playing: {name}",
        "stopped": "⏹ Stopped",
        "scanning": "🔍 Scanning...",
        "scanning_progress": "🔍 Scanning: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Found {found} stations",
        "settings_saved": "Settings saved",
        "now_playing": "Now playing: {text}",
        "unknown": "Unknown",

        # Settings validation errors
        "err_demod_audio_positive": "demod_rate/audio_rate must be > 0",
        "err_demod_multiple_audio": "demod_rate must be a multiple of audio_rate",
        "err_ymax_gt_ymin": "Y max must be > Y min",
        "err_smooth_time_range": "Time smoothing must be in [0..1]",
        "err_smooth_freq_range": "Freq smoothing must be in [0..10]",
        "err_fps_range": "FPS must be in [10..120]",
        "err_corr_points_range": "Corr points must be in [64..2048]",
        "err_corr_alpha_range": "Corr alpha must be in [0.05..1]",
        "err_corr_size_range": "Corr marker size must be in [1..8]",

        # Recording/log helper strings (keep placeholders)
        "recording_log": "Recording: {file}",
        "recording_status": "Recording: {file} ({size_mb:.2f} MB) | PCM input: {mb_in:.2f} MB",
        "record_saved": "Saved: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "File saved: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Recording stopped",
        "recording_file_prefix": "recording",
        "cannot_start_recording": "Cannot start recording: {e}",

        # Log strings
        "log_no_stations": "No stations in database. Click 'Scan FM band'.",
        "log_apply_gain": "Applying gain: {gain} dB",
        "log_playing": "Playing: {freq:.1f} MHz - {ps}",
        "log_gain": "RTL-SDR Gain: {gain} dB",
        "log_playback_error": "Playback error: {e}",
        "log_playback_stopped": "Playback stopped",
        "log_record_error": "Recording error: {e}",
        "log_stream_error": "Audio streaming error: {e}",
        "log_rds_updated": "RDS updated: {ps}",
        "log_spectrum_error": "Spectrum error: {e}",
        "log_scan_start": "Starting FM band scan {min:.1f}-{max:.1f} MHz...",
        "log_scan_step": "[{scanned}/{total}] Scanning {freq:.1f} MHz...",
        "log_scan_found": "  ✓ Found: {ps}",
        "log_scan_error": "Scan error: {e}",
        "log_scan_done": "Scan finished: found {found} stations",
        "log_scan_freq_error": "  ✗ Error: {e}",
        "log_settings_save_error": "Cannot save settings: {e}",
    },

    # NOTE: For all translations below, keep placeholders exactly as in EN/PL.
    "it": {
        "app_title": "Radio FM RTL-SDR con RDS",
        "title": "Radio FM con RDS",
        "status_ready": "Pronto",
        "manual_tuning": "Sintonizzazione manuale",
        "frequency_mhz": "Frequenza (MHz):",
        "tune": "Sintonizza",
        "stations": "Stazioni FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Stazione",
        "station_info": "Informazioni sulla stazione",
        "scan_band": "Scansiona banda FM",
        "play": "Riproduci",
        "stop": "Stop",
        "record_start": "Avvia registrazione",
        "record_stop": "Ferma registrazione",
        "sdr_audio_panel": "SDR e audio",
        "gain": "Guadagno RTL-SDR:",
        "volume": "Volume:",
        "settings": "Impostazioni...",
        "log": "Log",
        "viz": "Visualizzazione audio",
        "spec_title": "Spettro (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Sinistro",
        "right": "Destro",
        "corr_title": "Correlazione stereo",
        "corr_xlabel": "Sinistro (L)",
        "corr_ylabel": "Destro (R)",
        "settings_title": "Impostazioni",
        "apply": "Applica",
        "close": "Chiudi",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spettro",
        "group_ui": "Interfaccia",
        "language": "Lingua:",
        "recordings_dir": "Cartella registrazioni:",
        "osmosdr_args": "argomenti osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "Larghezza banda (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-enfasi (50 µs)",
        "rds_updates": "Aggiorna RDS durante la riproduzione",
        "interval_s": "Intervallo (s):",
        "max_hz": "Hz max:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Smussamento tempo:",
        "smooth_freq": "Smussamento freq:",
        "fps": "FPS:",
        "corr_points": "Punti corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Dimensione corr:",
        "err": "Errore",
        "warn": "Avviso",
        "info": "Info",
        "invalid_settings": "Impostazioni non valide: {e}",
        "apply_now_title": "Applicare ora?",
        "apply_now_msg": "Queste modifiche richiedono il riavvio della riproduzione. Riavviare ora?",
        "scan_already": "Scansione già in corso",
        "pick_station": "Seleziona una stazione dall'elenco",
        "station_not_found": "Dati stazione non trovati",
        "need_playback_first": "Avvia prima la riproduzione",
        "bad_freq": "Frequenza non valida",
        "freq_out_of_range": "Frequenza fuori intervallo 88-108 MHz",
        "playing": "▶ In riproduzione: {name}",
        "stopped": "⏹ Fermato",
        "scanning": "🔍 Scansione...",
        "scanning_progress": "🔍 Scansione: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Trovate {found} stazioni",
        "settings_saved": "Impostazioni salvate",
        "now_playing": "In riproduzione: {text}",

        "recording_log": "Registrazione: {file}",
        "recording_status": "Registrazione: {file} ({size_mb:.2f} MB) | ingresso PCM: {mb_in:.2f} MB",
        "record_saved": "Salvato: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "File salvato: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Registrazione interrotta",

        # Log strings
        "log_no_stations": "Nessuna stazione nel database. Premi 'Scansiona banda FM'.",
        "log_apply_gain": "Applico gain: {gain} dB",
        "log_playing": "Riproduzione: {freq:.1f} MHz - {ps}",
        "log_gain": "Gain RTL-SDR: {gain} dB",
        "log_playback_error": "Errore di riproduzione: {e}",
        "log_playback_stopped": "Riproduzione interrotta",
        "log_record_error": "Errore di registrazione: {e}",
        "log_stream_error": "Errore streaming audio: {e}",
        "log_rds_updated": "RDS aggiornato: {ps}",
        "log_spectrum_error": "Errore spettro: {e}",
        "log_scan_start": "Avvio scansione banda FM 88-108 MHz...",
        "log_scan_step": "[{scanned}/{total}] Scansione {freq:.1f} MHz...",
        "log_scan_found": "  ✓ Trovato: {ps}",
        "log_scan_error": "Errore scansione: {e}",
        "log_scan_done": "Scansione completata: trovate {found} stazioni",
        "log_scan_freq_error": "  ✗ Errore: {e}",
        "log_settings_save_error": "Impossibile salvare le impostazioni: {e}",
    },

    "es": {
        "app_title": "Radio FM RTL-SDR con RDS",
        "title": "Radio FM con RDS",
        "status_ready": "Listo",
        "manual_tuning": "Sintonización manual",
        "frequency_mhz": "Frecuencia (MHz):",
        "tune": "Sintonizar",
        "stations": "Emisoras FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Emisora",
        "station_info": "Información de la emisora",
        "scan_band": "Escanear banda FM",
        "play": "Reproducir",
        "stop": "Detener",
        "record_start": "Iniciar grabación",
        "record_stop": "Detener grabación",
        "sdr_audio_panel": "SDR y audio",
        "gain": "Ganancia RTL-SDR:",
        "volume": "Volumen:",
        "settings": "Ajustes...",
        "log": "Registro",
        "viz": "Visualización de audio",
        "spec_title": "Espectro (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Izquierdo",
        "right": "Derecho",
        "corr_title": "Correlación estéreo",
        "corr_xlabel": "Izquierdo (L)",
        "corr_ylabel": "Derecho (R)",
        "settings_title": "Ajustes",
        "apply": "Aplicar",
        "close": "Cerrar",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Espectro",
        "group_ui": "Interfaz",
        "language": "Idioma:",
        "recordings_dir": "Carpeta de grabaciones:",
        "osmosdr_args": "argumentos osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "Ancho de banda (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-énfasis (50 µs)",
        "rds_updates": "Actualizar RDS durante la reproducción",
        "interval_s": "Intervalo (s):",
        "max_hz": "Hz máx:",
        "ymin_dbfs": "Y mín (dBFS):",
        "ymax_dbfs": "Y máx (dBFS):",
        "smooth_time": "Suavizado tiempo:",
        "smooth_freq": "Suavizado frec:",
        "fps": "FPS:",
        "corr_points": "Puntos corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Tamaño corr:",
        "err": "Error",
        "warn": "Aviso",
        "info": "Info",
        "invalid_settings": "Ajustes no válidos: {e}",
        "apply_now_title": "¿Aplicar ahora?",
        "apply_now_msg": "Estos cambios requieren reiniciar la reproducción. ¿Reiniciar ahora?",
        "scan_already": "El escaneo ya está en curso",
        "pick_station": "Selecciona una emisora de la lista",
        "station_not_found": "No se encontraron datos de la emisora",
        "need_playback_first": "Inicia la reproducción primero",
        "bad_freq": "Frecuencia no válida",
        "freq_out_of_range": "Frecuencia fuera de rango 88-108 MHz",
        "playing": "▶ Reproduciendo: {name}",
        "stopped": "⏹ Detenido",
        "scanning": "🔍 Escaneando...",
        "scanning_progress": "🔍 Escaneo: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Encontradas {found} emisoras",
        "settings_saved": "Ajustes guardados",
        "now_playing": "Reproduciendo: {text}",
    },

    "fr": {
        "app_title": "Radio FM RTL-SDR avec RDS",
        "title": "Radio FM avec RDS",
        "status_ready": "Prêt",
        "manual_tuning": "Réglage manuel",
        "frequency_mhz": "Fréquence (MHz) :",
        "tune": "Accorder",
        "stations": "Stations FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Station",
        "station_info": "Infos station",
        "scan_band": "Scanner la bande FM",
        "play": "Lire",
        "stop": "Stop",
        "record_start": "Démarrer l'enregistrement",
        "record_stop": "Arrêter l'enregistrement",
        "sdr_audio_panel": "SDR et audio",
        "gain": "Gain RTL-SDR :",
        "volume": "Volume :",
        "settings": "Paramètres...",
        "log": "Journal",
        "viz": "Visualisation audio",
        "spec_title": "Spectre (dBFS) G/D",
        "spec_ylabel": "dBFS",
        "left": "Gauche",
        "right": "Droite",
        "corr_title": "Corrélation stéréo",
        "corr_xlabel": "Gauche (L)",
        "corr_ylabel": "Droite (R)",
        "settings_title": "Paramètres",
        "apply": "Appliquer",
        "close": "Fermer",
        "group_sdr": "SDR",
        "group_audio": "Audio / Démod",
        "group_rds": "RDS",
        "group_spectrum": "Spectre",
        "group_ui": "Interface",
        "language": "Langue :",
        "recordings_dir": "Dossier des enregistrements :",
        "osmosdr_args": "arguments osmosdr :",
        "ppm": "PPM :",
        "bw_khz": "BP (kHz) :",
        "demod_rate": "demod_rate (Hz) :",
        "audio_rate": "audio_rate (Hz) :",
        "deemphasis": "Désaccentuation (50 µs)",
        "rds_updates": "Mettre à jour le RDS pendant la lecture",
        "interval_s": "Intervalle (s) :",
        "max_hz": "Hz max :",
        "ymin_dbfs": "Y min (dBFS) :",
        "ymax_dbfs": "Y max (dBFS) :",
        "smooth_time": "Lissage temps :",
        "smooth_freq": "Lissage freq :",
        "fps": "FPS :",
        "corr_points": "Points corr :",
        "corr_alpha": "Alpha corr :",
        "corr_size": "Taille corr :",
        "err": "Erreur",
        "warn": "Avertissement",
        "info": "Info",
        "invalid_settings": "Paramètres invalides : {e}",
        "apply_now_title": "Appliquer maintenant ?",
        "apply_now_msg": "Ces changements nécessitent de redémarrer la lecture. Redémarrer maintenant ?",
        "scan_already": "Scan déjà en cours",
        "pick_station": "Sélectionnez une station dans la liste",
        "station_not_found": "Données de la station introuvables",
        "need_playback_first": "Démarrez la lecture d'abord",
        "bad_freq": "Fréquence invalide",
        "freq_out_of_range": "Fréquence hors plage 88-108 MHz",
        "playing": "▶ Lecture : {name}",
        "stopped": "⏹ Arrêté",
        "scanning": "🔍 Scan...",
        "scanning_progress": "🔍 Scan : {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} stations trouvées",
        "settings_saved": "Paramètres enregistrés",
        "now_playing": "En cours : {text}",
    },

    "de": {
        "app_title": "RTL-SDR UKW-Radio mit RDS",
        "title": "UKW-Radio mit RDS",
        "status_ready": "Bereit",
        "manual_tuning": "Manuelle Abstimmung",
        "frequency_mhz": "Frequenz (MHz):",
        "tune": "Abstimmen",
        "stations": "UKW-Sender",
        "stations_col_freq": "MHz",
        "stations_col_name": "Sender",
        "station_info": "Senderinfo",
        "scan_band": "UKW-Band scannen",
        "play": "Wiedergabe",
        "stop": "Stopp",
        "record_start": "Aufnahme starten",
        "record_stop": "Aufnahme stoppen",
        "sdr_audio_panel": "SDR und Audio",
        "gain": "RTL-SDR Gain:",
        "volume": "Lautstärke:",
        "settings": "Einstellungen...",
        "log": "Log",
        "viz": "Audio-Visualisierung",
        "spec_title": "Spektrum (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Links",
        "right": "Rechts",
        "corr_title": "Stereo-Korrelation",
        "corr_xlabel": "Links (L)",
        "corr_ylabel": "Rechts (R)",
        "settings_title": "Einstellungen",
        "apply": "Anwenden",
        "close": "Schließen",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spektrum",
        "group_ui": "UI",
        "language": "Sprache:",
        "recordings_dir": "Aufnahmeordner:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "Bandbreite (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-Emphasis (50 µs)",
        "rds_updates": "RDS während der Wiedergabe aktualisieren",
        "interval_s": "Intervall (s):",
        "max_hz": "Max Hz:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Zeit glätten:",
        "smooth_freq": "Frequenz glätten:",
        "fps": "FPS:",
        "corr_points": "Korrelationspunkte:",
        "corr_alpha": "Korr-Alpha:",
        "corr_size": "Korr-Größe:",
        "err": "Fehler",
        "warn": "Warnung",
        "info": "Info",
        "invalid_settings": "Ungültige Einstellungen: {e}",
        "apply_now_title": "Jetzt anwenden?",
        "apply_now_msg": "Diese Änderungen erfordern einen Neustart der Wiedergabe. Jetzt neu starten?",
        "scan_already": "Scan läuft bereits",
        "pick_station": "Wähle einen Sender aus der Liste",
        "station_not_found": "Senderdaten nicht gefunden",
        "need_playback_first": "Starte zuerst die Wiedergabe",
        "bad_freq": "Ungültige Frequenz",
        "freq_out_of_range": "Frequenz außerhalb des Bereichs 88-108 MHz",
        "playing": "▶ Wiedergabe: {name}",
        "stopped": "⏹ Gestoppt",
        "scanning": "🔍 Scanne...",
        "scanning_progress": "🔍 Scan: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} Sender gefunden",
        "settings_saved": "Einstellungen gespeichert",
        "now_playing": "Jetzt läuft: {text}",
    },

    "pt": {
        "app_title": "Rádio FM RTL-SDR com RDS",
        "title": "Rádio FM com RDS",
        "status_ready": "Pronto",
        "manual_tuning": "Sintonia manual",
        "frequency_mhz": "Frequência (MHz):",
        "tune": "Sintonizar",
        "stations": "Estações FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Estação",
        "station_info": "Informações da estação",
        "scan_band": "Varredura da banda FM",
        "play": "Reproduzir",
        "stop": "Parar",
        "record_start": "Iniciar gravação",
        "record_stop": "Parar gravação",
        "sdr_audio_panel": "SDR e áudio",
        "gain": "Ganho RTL-SDR:",
        "volume": "Volume:",
        "settings": "Configurações...",
        "log": "Log",
        "viz": "Visualização de áudio",
        "spec_title": "Espectro (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Esquerdo",
        "right": "Direito",
        "corr_title": "Correlação estéreo",
        "corr_xlabel": "Esquerdo (L)",
        "corr_ylabel": "Direito (R)",
        "settings_title": "Configurações",
        "apply": "Aplicar",
        "close": "Fechar",
        "group_sdr": "SDR",
        "group_audio": "Áudio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Espectro",
        "group_ui": "Interface",
        "language": "Idioma:",
        "recordings_dir": "Pasta de gravações:",
        "osmosdr_args": "argumentos osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "Largura de banda (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-ênfase (50 µs)",
        "rds_updates": "Atualizar RDS durante a reprodução",
        "interval_s": "Intervalo (s):",
        "max_hz": "Hz máx:",
        "ymin_dbfs": "Y mín (dBFS):",
        "ymax_dbfs": "Y máx (dBFS):",
        "smooth_time": "Suavização tempo:",
        "smooth_freq": "Suavização freq:",
        "fps": "FPS:",
        "corr_points": "Pontos corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Tamanho corr:",
        "err": "Erro",
        "warn": "Aviso",
        "info": "Info",
        "invalid_settings": "Configurações inválidas: {e}",
        "apply_now_title": "Aplicar agora?",
        "apply_now_msg": "Essas alterações exigem reiniciar a reprodução. Reiniciar agora?",
        "scan_already": "Varredura já em execução",
        "pick_station": "Selecione uma estação da lista",
        "station_not_found": "Dados da estação não encontrados",
        "need_playback_first": "Inicie a reprodução primeiro",
        "bad_freq": "Frequência inválida",
        "freq_out_of_range": "Frequência fora do intervalo 88-108 MHz",
        "playing": "▶ Reproduzindo: {name}",
        "stopped": "⏹ Parado",
        "scanning": "🔍 Varrendo...",
        "scanning_progress": "🔍 Varredura: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Encontradas {found} estações",
        "settings_saved": "Configurações salvas",
        "now_playing": "Tocando: {text}",
    },

    "ru": {
        "app_title": "RTL-SDR FM радио с RDS",
        "title": "FM радио с RDS",
        "status_ready": "Готово",
        "manual_tuning": "Ручная настройка",
        "frequency_mhz": "Частота (MHz):",
        "tune": "Настроить",
        "stations": "FM станции",
        "stations_col_freq": "MHz",
        "stations_col_name": "Станция",
        "station_info": "Информация о станции",
        "scan_band": "Сканировать FM диапазон",
        "play": "Воспроизвести",
        "stop": "Стоп",
        "record_start": "Начать запись",
        "record_stop": "Остановить запись",
        "sdr_audio_panel": "SDR и аудио",
        "gain": "Усиление RTL-SDR:",
        "volume": "Громкость:",
        "settings": "Настройки...",
        "log": "Лог",
        "viz": "Визуализация аудио",
        "spec_title": "Спектр (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Левый",
        "right": "Правый",
        "corr_title": "Стерео корреляция",
        "corr_xlabel": "Левый (L)",
        "corr_ylabel": "Правый (R)",
        "settings_title": "Настройки",
        "apply": "Применить",
        "close": "Закрыть",
        "group_sdr": "SDR",
        "group_audio": "Аудио / Демод",
        "group_rds": "RDS",
        "group_spectrum": "Спектр",
        "group_ui": "Интерфейс",
        "language": "Язык:",
        "recordings_dir": "Папка записей:",
        "osmosdr_args": "аргументы osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "Полоса (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "Деэмфазис (50 µs)",
        "rds_updates": "Обновлять RDS во время воспроизведения",
        "interval_s": "Интервал (s):",
        "max_hz": "Макс Hz:",
        "ymin_dbfs": "Y мин (dBFS):",
        "ymax_dbfs": "Y макс (dBFS):",
        "smooth_time": "Сглаж. по времени:",
        "smooth_freq": "Сглаж. по частоте:",
        "fps": "FPS:",
        "corr_points": "Точки корр:",
        "corr_alpha": "Альфа корр:",
        "corr_size": "Размер корр:",
        "err": "Ошибка",
        "warn": "Предупреждение",
        "info": "Инфо",
        "invalid_settings": "Неверные настройки: {e}",
        "apply_now_title": "Применить сейчас?",
        "apply_now_msg": "Эти изменения требуют перезапуска воспроизведения. Перезапустить сейчас?",
        "scan_already": "Сканирование уже идет",
        "pick_station": "Выберите станцию из списка",
        "station_not_found": "Данные станции не найдены",
        "need_playback_first": "Сначала запустите воспроизведение",
        "bad_freq": "Неверная частота",
        "freq_out_of_range": "Частота вне диапазона 88-108 MHz",
        "playing": "▶ Воспроизведение: {name}",
        "stopped": "⏹ Остановлено",
        "scanning": "🔍 Сканирование...",
        "scanning_progress": "🔍 Сканирование: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Найдено {found} станций",
        "settings_saved": "Настройки сохранены",
        "now_playing": "Сейчас играет: {text}",
    },

    "id": {
        "app_title": "Radio FM RTL-SDR dengan RDS",
        "title": "Radio FM dengan RDS",
        "status_ready": "Siap",
        "manual_tuning": "Penyetelan manual",
        "frequency_mhz": "Frekuensi (MHz):",
        "tune": "Setel",
        "stations": "Stasiun FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Stasiun",
        "station_info": "Info stasiun",
        "scan_band": "Pindai pita FM",
        "play": "Putar",
        "stop": "Berhenti",
        "record_start": "Mulai rekam",
        "record_stop": "Hentikan rekam",
        "sdr_audio_panel": "SDR dan audio",
        "gain": "Gain RTL-SDR:",
        "volume": "Volume:",
        "settings": "Pengaturan...",
        "log": "Log",
        "viz": "Visualisasi audio",
        "spec_title": "Spektrum (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Kiri",
        "right": "Kanan",
        "corr_title": "Korelasi stereo",
        "corr_xlabel": "Kiri (L)",
        "corr_ylabel": "Kanan (R)",
        "settings_title": "Pengaturan",
        "apply": "Terapkan",
        "close": "Tutup",
        "group_sdr": "SDR",
        "group_audio": "Audio / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spektrum",
        "group_ui": "UI",
        "language": "Bahasa:",
        "recordings_dir": "Folder rekaman:",
        "osmosdr_args": "argumen osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-emphasis (50 µs)",
        "rds_updates": "Perbarui RDS saat memutar",
        "interval_s": "Interval (s):",
        "max_hz": "Hz maks:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y maks (dBFS):",
        "smooth_time": "Pemulusan waktu:",
        "smooth_freq": "Pemulusan frek:",
        "fps": "FPS:",
        "corr_points": "Titik corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Ukuran corr:",
        "err": "Kesalahan",
        "warn": "Peringatan",
        "info": "Info",
        "invalid_settings": "Pengaturan tidak valid: {e}",
        "apply_now_title": "Terapkan sekarang?",
        "apply_now_msg": "Perubahan ini memerlukan restart pemutaran. Restart sekarang?",
        "scan_already": "Pemindaian sudah berjalan",
        "pick_station": "Pilih stasiun dari daftar",
        "station_not_found": "Data stasiun tidak ditemukan",
        "need_playback_first": "Mulai pemutaran dulu",
        "bad_freq": "Frekuensi tidak valid",
        "freq_out_of_range": "Frekuensi di luar rentang 88-108 MHz",
        "playing": "▶ Memutar: {name}",
        "stopped": "⏹ Berhenti",
        "scanning": "🔍 Memindai...",
        "scanning_progress": "🔍 Pemindaian: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Ditemukan {found} stasiun",
        "settings_saved": "Pengaturan disimpan",
        "now_playing": "Sedang diputar: {text}",
    },

    "tr": {
        "app_title": "RDS'li RTL-SDR FM Radyo",
        "title": "RDS'li FM Radyo",
        "status_ready": "Hazır",
        "manual_tuning": "Manuel ayar",
        "frequency_mhz": "Frekans (MHz):",
        "tune": "Ayarla",
        "stations": "FM istasyonları",
        "stations_col_freq": "MHz",
        "stations_col_name": "İstasyon",
        "station_info": "İstasyon bilgisi",
        "scan_band": "FM bandını tara",
        "play": "Çal",
        "stop": "Durdur",
        "record_start": "Kaydı başlat",
        "record_stop": "Kaydı durdur",
        "sdr_audio_panel": "SDR ve ses",
        "gain": "RTL-SDR kazanç:",
        "volume": "Ses seviyesi:",
        "settings": "Ayarlar...",
        "log": "Günlük",
        "viz": "Ses görselleştirme",
        "spec_title": "Spektrum (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Sol",
        "right": "Sağ",
        "corr_title": "Stereo korelasyon",
        "corr_xlabel": "Sol (L)",
        "corr_ylabel": "Sağ (R)",
        "settings_title": "Ayarlar",
        "apply": "Uygula",
        "close": "Kapat",
        "group_sdr": "SDR",
        "group_audio": "Ses / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spektrum",
        "group_ui": "Arayüz",
        "language": "Dil:",
        "recordings_dir": "Kayıt klasörü:",
        "osmosdr_args": "osmosdr argümanları:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-emphasis (50 µs)",
        "rds_updates": "Çalma sırasında RDS güncelle",
        "interval_s": "Aralık (s):",
        "max_hz": "Maks Hz:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y maks (dBFS):",
        "smooth_time": "Zaman yumuşatma:",
        "smooth_freq": "Frek yumuşatma:",
        "fps": "FPS:",
        "corr_points": "Corr noktaları:",
        "corr_alpha": "Corr alfa:",
        "corr_size": "Corr boyut:",
        "err": "Hata",
        "warn": "Uyarı",
        "info": "Bilgi",
        "invalid_settings": "Geçersiz ayarlar: {e}",
        "apply_now_title": "Şimdi uygula?",
        "apply_now_msg": "Bu değişiklikler çalmayı yeniden başlatmayı gerektirir. Şimdi yeniden başlatılsın mı?",
        "scan_already": "Tarama zaten sürüyor",
        "pick_station": "Listeden bir istasyon seçin",
        "station_not_found": "İstasyon verisi bulunamadı",
        "need_playback_first": "Önce çalmayı başlatın",
        "bad_freq": "Geçersiz frekans",
        "freq_out_of_range": "Frekans aralığı dışında 88-108 MHz",
        "playing": "▶ Çalıyor: {name}",
        "stopped": "⏹ Durduruldu",
        "scanning": "🔍 Taranıyor...",
        "scanning_progress": "🔍 Tarama: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} istasyon bulundu",
        "settings_saved": "Ayarlar kaydedildi",
        "now_playing": "Çalıyor: {text}",
    },

    "vi": {
        "app_title": "Đài FM RTL-SDR với RDS",
        "title": "Đài FM với RDS",
        "status_ready": "Sẵn sàng",
        "manual_tuning": "Chỉnh tay",
        "frequency_mhz": "Tần số (MHz):",
        "tune": "Chỉnh",
        "stations": "Đài FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Đài",
        "station_info": "Thông tin đài",
        "scan_band": "Quét băng FM",
        "play": "Phát",
        "stop": "Dừng",
        "record_start": "Bắt đầu ghi",
        "record_stop": "Dừng ghi",
        "sdr_audio_panel": "SDR và âm thanh",
        "gain": "Gain RTL-SDR:",
        "volume": "Âm lượng:",
        "settings": "Cài đặt...",
        "log": "Nhật ký",
        "viz": "Hiển thị âm thanh",
        "spec_title": "Phổ (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Trái",
        "right": "Phải",
        "corr_title": "Tương quan stereo",
        "corr_xlabel": "Trái (L)",
        "corr_ylabel": "Phải (R)",
        "settings_title": "Cài đặt",
        "apply": "Áp dụng",
        "close": "Đóng",
        "group_sdr": "SDR",
        "group_audio": "Âm thanh / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Phổ",
        "group_ui": "Giao diện",
        "language": "Ngôn ngữ:",
        "recordings_dir": "Thư mục ghi âm:",
        "osmosdr_args": "tham số osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-emphasis (50 µs)",
        "rds_updates": "Cập nhật RDS khi đang phát",
        "interval_s": "Khoảng (s):",
        "max_hz": "Hz tối đa:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Làm mượt thời gian:",
        "smooth_freq": "Làm mượt tần số:",
        "fps": "FPS:",
        "corr_points": "Điểm corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Kích thước corr:",
        "err": "Lỗi",
        "warn": "Cảnh báo",
        "info": "Thông tin",
        "invalid_settings": "Cài đặt không hợp lệ: {e}",
        "apply_now_title": "Áp dụng ngay?",
        "apply_now_msg": "Các thay đổi này cần khởi động lại phát. Khởi động lại ngay?",
        "scan_already": "Đang quét",
        "pick_station": "Chọn một đài từ danh sách",
        "station_not_found": "Không tìm thấy dữ liệu đài",
        "need_playback_first": "Hãy bắt đầu phát trước",
        "bad_freq": "Tần số không hợp lệ",
        "freq_out_of_range": "Tần số ngoài khoảng 88-108 MHz",
        "playing": "▶ Đang phát: {name}",
        "stopped": "⏹ Đã dừng",
        "scanning": "🔍 Đang quét...",
        "scanning_progress": "🔍 Quét: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Tìm thấy {found} đài",
        "settings_saved": "Đã lưu cài đặt",
        "now_playing": "Đang phát: {text}",
    },

    "zh": {
        "app_title": "RTL-SDR FM 收音机 (RDS)",
        "title": "带 RDS 的 FM 收音机",
        "status_ready": "就绪",
        "manual_tuning": "手动调谐",
        "frequency_mhz": "频率 (MHz):",
        "tune": "调谐",
        "stations": "FM 电台",
        "stations_col_freq": "MHz",
        "stations_col_name": "电台",
        "station_info": "电台信息",
        "scan_band": "扫描 FM 频段",
        "play": "播放",
        "stop": "停止",
        "record_start": "开始录音",
        "record_stop": "停止录音",
        "sdr_audio_panel": "SDR 与音频",
        "gain": "RTL-SDR 增益:",
        "volume": "音量:",
        "settings": "设置...",
        "log": "日志",
        "viz": "音频可视化",
        "spec_title": "频谱 (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "左",
        "right": "右",
        "corr_title": "立体声相关",
        "corr_xlabel": "左 (L)",
        "corr_ylabel": "右 (R)",
        "settings_title": "设置",
        "apply": "应用",
        "close": "关闭",
        "group_sdr": "SDR",
        "group_audio": "音频 / 解调",
        "group_rds": "RDS",
        "group_spectrum": "频谱",
        "group_ui": "界面",
        "language": "语言:",
        "recordings_dir": "录音目录:",
        "osmosdr_args": "osmosdr 参数:",
        "ppm": "PPM:",
        "bw_khz": "带宽 (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "去加重 (50 µs)",
        "rds_updates": "播放时更新 RDS",
        "interval_s": "间隔 (s):",
        "max_hz": "最大 Hz:",
        "ymin_dbfs": "Y 最小 (dBFS):",
        "ymax_dbfs": "Y 最大 (dBFS):",
        "smooth_time": "时间平滑:",
        "smooth_freq": "频率平滑:",
        "fps": "FPS:",
        "corr_points": "相关点数:",
        "corr_alpha": "相关透明度:",
        "corr_size": "相关点大小:",
        "err": "错误",
        "warn": "警告",
        "info": "信息",
        "invalid_settings": "设置无效: {e}",
        "apply_now_title": "现在应用?",
        "apply_now_msg": "这些更改需要重启播放。现在重启?",
        "scan_already": "正在扫描",
        "pick_station": "从列表中选择电台",
        "station_not_found": "未找到电台数据",
        "need_playback_first": "请先开始播放",
        "bad_freq": "频率无效",
        "freq_out_of_range": "频率超出范围 88-108 MHz",
        "playing": "▶ 正在播放: {name}",
        "stopped": "⏹ 已停止",
        "scanning": "🔍 扫描中...",
        "scanning_progress": "🔍 扫描: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ 找到 {found} 个电台",
        "settings_saved": "设置已保存",
        "now_playing": "正在播放: {text}",
    },

    "ja": {
        "app_title": "RTL-SDR FMラジオ (RDS)",
        "title": "RDS付きFMラジオ",
        "status_ready": "準備完了",
        "manual_tuning": "手動チューニング",
        "frequency_mhz": "周波数 (MHz):",
        "tune": "同調",
        "stations": "FM局",
        "stations_col_freq": "MHz",
        "stations_col_name": "局",
        "station_info": "局情報",
        "scan_band": "FM帯をスキャン",
        "play": "再生",
        "stop": "停止",
        "record_start": "録音開始",
        "record_stop": "録音停止",
        "sdr_audio_panel": "SDR とオーディオ",
        "gain": "RTL-SDR ゲイン:",
        "volume": "音量:",
        "settings": "設定...",
        "log": "ログ",
        "viz": "オーディオ可視化",
        "spec_title": "スペクトラム (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "左",
        "right": "右",
        "corr_title": "ステレオ相関",
        "corr_xlabel": "左 (L)",
        "corr_ylabel": "右 (R)",
        "settings_title": "設定",
        "apply": "適用",
        "close": "閉じる",
        "group_sdr": "SDR",
        "group_audio": "オーディオ / 復調",
        "group_rds": "RDS",
        "group_spectrum": "スペクトラム",
        "group_ui": "UI",
        "language": "言語:",
        "recordings_dir": "録音フォルダー:",
        "osmosdr_args": "osmosdr 引数:",
        "ppm": "PPM:",
        "bw_khz": "帯域幅 (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "ディエンファシス (50 µs)",
        "rds_updates": "再生中にRDSを更新",
        "interval_s": "間隔 (s):",
        "max_hz": "最大 Hz:",
        "ymin_dbfs": "Y 最小 (dBFS):",
        "ymax_dbfs": "Y 最大 (dBFS):",
        "smooth_time": "時間平滑:",
        "smooth_freq": "周波数平滑:",
        "fps": "FPS:",
        "corr_points": "相関ポイント:",
        "corr_alpha": "相関アルファ:",
        "corr_size": "相関サイズ:",
        "err": "エラー",
        "warn": "警告",
        "info": "情報",
        "invalid_settings": "無効な設定: {e}",
        "apply_now_title": "今すぐ適用?",
        "apply_now_msg": "これらの変更には再生の再起動が必要です。今すぐ再起動しますか?",
        "scan_already": "スキャン中です",
        "pick_station": "リストから局を選択してください",
        "station_not_found": "局データが見つかりません",
        "need_playback_first": "先に再生を開始してください",
        "bad_freq": "無効な周波数",
        "freq_out_of_range": "周波数が範囲外です (88-108 MHz)",
        "playing": "▶ 再生中: {name}",
        "stopped": "⏹ 停止",
        "scanning": "🔍 スキャン中...",
        "scanning_progress": "🔍 スキャン: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} 局を検出",
        "settings_saved": "設定を保存しました",
        "now_playing": "再生中: {text}",
    },

    "ko": {
        "app_title": "RTL-SDR FM 라디오 (RDS)",
        "title": "RDS 지원 FM 라디오",
        "status_ready": "준비됨",
        "manual_tuning": "수동 튜닝",
        "frequency_mhz": "주파수 (MHz):",
        "tune": "튜닝",
        "stations": "FM 방송",
        "stations_col_freq": "MHz",
        "stations_col_name": "방송",
        "station_info": "방송 정보",
        "scan_band": "FM 대역 스캔",
        "play": "재생",
        "stop": "정지",
        "record_start": "녹음 시작",
        "record_stop": "녹음 중지",
        "sdr_audio_panel": "SDR 및 오디오",
        "gain": "RTL-SDR 게인:",
        "volume": "볼륨:",
        "settings": "설정...",
        "log": "로그",
        "viz": "오디오 시각화",
        "spec_title": "스펙트럼 (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "왼쪽",
        "right": "오른쪽",
        "corr_title": "스테레오 상관",
        "corr_xlabel": "왼쪽 (L)",
        "corr_ylabel": "오른쪽 (R)",
        "settings_title": "설정",
        "apply": "적용",
        "close": "닫기",
        "group_sdr": "SDR",
        "group_audio": "오디오 / 복조",
        "group_rds": "RDS",
        "group_spectrum": "스펙트럼",
        "group_ui": "UI",
        "language": "언어:",
        "recordings_dir": "녹음 폴더:",
        "osmosdr_args": "osmosdr 인자:",
        "ppm": "PPM:",
        "bw_khz": "대역폭 (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "디임퍼시스 (50 µs)",
        "rds_updates": "재생 중 RDS 업데이트",
        "interval_s": "간격 (s):",
        "max_hz": "최대 Hz:",
        "ymin_dbfs": "Y 최소 (dBFS):",
        "ymax_dbfs": "Y 최대 (dBFS):",
        "smooth_time": "시간 스무딩:",
        "smooth_freq": "주파수 스무딩:",
        "fps": "FPS:",
        "corr_points": "상관 점:",
        "corr_alpha": "상관 알파:",
        "corr_size": "상관 크기:",
        "err": "오류",
        "warn": "경고",
        "info": "정보",
        "invalid_settings": "잘못된 설정: {e}",
        "apply_now_title": "지금 적용할까요?",
        "apply_now_msg": "이 변경 사항은 재생을 다시 시작해야 합니다. 지금 재시작할까요?",
        "scan_already": "이미 스캔 중입니다",
        "pick_station": "목록에서 방송을 선택하세요",
        "station_not_found": "방송 데이터를 찾을 수 없습니다",
        "need_playback_first": "먼저 재생을 시작하세요",
        "bad_freq": "잘못된 주파수",
        "freq_out_of_range": "주파수가 범위를 벗어났습니다 (88-108 MHz)",
        "playing": "▶ 재생 중: {name}",
        "stopped": "⏹ 정지됨",
        "scanning": "🔍 스캔 중...",
        "scanning_progress": "🔍 스캔: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found}개 방송 발견",
        "settings_saved": "설정이 저장되었습니다",
        "now_playing": "재생 중: {text}",
    },

    # The following languages are provided with full UI coverage as well.
    # They use concise, common translations suitable for a desktop UI.
    "hi": {
        "app_title": "RTL-SDR FM रेडियो (RDS)",
        "title": "RDS के साथ FM रेडियो",
        "status_ready": "तैयार",
        "manual_tuning": "मैनुअल ट्यूनिंग",
        "frequency_mhz": "आवृत्ति (MHz):",
        "tune": "ट्यून",
        "stations": "FM स्टेशन",
        "stations_col_freq": "MHz",
        "stations_col_name": "स्टेशन",
        "station_info": "स्टेशन जानकारी",
        "scan_band": "FM बैंड स्कैन करें",
        "play": "चलाएँ",
        "stop": "रोकें",
        "record_start": "रिकॉर्डिंग शुरू करें",
        "record_stop": "रिकॉर्डिंग रोकें",
        "sdr_audio_panel": "SDR और ऑडियो",
        "gain": "RTL-SDR गेन:",
        "volume": "वॉल्यूम:",
        "settings": "सेटिंग्स...",
        "log": "लॉग",
        "viz": "ऑडियो दृश्य",
        "spec_title": "स्पेक्ट्रम (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "बायाँ",
        "right": "दायाँ",
        "corr_title": "स्टेरियो सहसंबंध",
        "corr_xlabel": "बायाँ (L)",
        "corr_ylabel": "दायाँ (R)",
        "settings_title": "सेटिंग्स",
        "apply": "लागू करें",
        "close": "बंद करें",
        "group_sdr": "SDR",
        "group_audio": "ऑडियो / डिमॉड",
        "group_rds": "RDS",
        "group_spectrum": "स्पेक्ट्रम",
        "group_ui": "UI",
        "language": "भाषा:",
        "recordings_dir": "रिकॉर्डिंग फ़ोल्डर:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "डी-एम्फ़ेसिस (50 µs)",
        "rds_updates": "चलाते समय RDS अपडेट करें",
        "interval_s": "अंतराल (s):",
        "max_hz": "अधिकतम Hz:",
        "ymin_dbfs": "Y न्यून (dBFS):",
        "ymax_dbfs": "Y अधिक (dBFS):",
        "smooth_time": "समय स्मूद:",
        "smooth_freq": "फ्रीक्वेंसी स्मूद:",
        "fps": "FPS:",
        "corr_points": "Corr पॉइंट:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr आकार:",
        "err": "त्रुटि",
        "warn": "चेतावनी",
        "info": "जानकारी",
        "invalid_settings": "अमान्य सेटिंग्स: {e}",
        "apply_now_title": "अभी लागू करें?",
        "apply_now_msg": "इन बदलावों के लिए प्लेबैक रीस्टार्ट करना होगा। अभी रीस्टार्ट करें?",
        "scan_already": "स्कैन पहले से चल रहा है",
        "pick_station": "सूची से स्टेशन चुनें",
        "station_not_found": "स्टेशन डेटा नहीं मिला",
        "need_playback_first": "पहले प्लेबैक शुरू करें",
        "bad_freq": "अमान्य आवृत्ति",
        "freq_out_of_range": "आवृत्ति सीमा से बाहर 88-108 MHz",
        "playing": "▶ चल रहा: {name}",
        "stopped": "⏹ रोका गया",
        "scanning": "🔍 स्कैन हो रहा है...",
        "scanning_progress": "🔍 स्कैन: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} स्टेशन मिले",
        "settings_saved": "सेटिंग्स सेव की गईं",
        "now_playing": "अब चल रहा: {text}",
    },

    "ar": {
        "app_title": "راديو FM RTL-SDR مع RDS",
        "title": "راديو FM مع RDS",
        "status_ready": "جاهز",
        "manual_tuning": "ضبط يدوي",
        "frequency_mhz": "التردد (MHz):",
        "tune": "ضبط",
        "stations": "محطات FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "المحطة",
        "station_info": "معلومات المحطة",
        "scan_band": "مسح نطاق FM",
        "play": "تشغيل",
        "stop": "إيقاف",
        "record_start": "بدء التسجيل",
        "record_stop": "إيقاف التسجيل",
        "sdr_audio_panel": "SDR والصوت",
        "gain": "كسب RTL-SDR:",
        "volume": "مستوى الصوت:",
        "settings": "الإعدادات...",
        "log": "السجل",
        "viz": "تصور الصوت",
        "spec_title": "الطيف (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "يسار",
        "right": "يمين",
        "corr_title": "ترابط ستيريو",
        "corr_xlabel": "يسار (L)",
        "corr_ylabel": "يمين (R)",
        "settings_title": "الإعدادات",
        "apply": "تطبيق",
        "close": "إغلاق",
        "group_sdr": "SDR",
        "group_audio": "الصوت / إزالة التضمين",
        "group_rds": "RDS",
        "group_spectrum": "الطيف",
        "group_ui": "الواجهة",
        "language": "اللغة:",
        "recordings_dir": "مجلد التسجيلات:",
        "osmosdr_args": "وسائط osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "عرض النطاق (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "إزالة التأكيد (50 µs)",
        "rds_updates": "تحديث RDS أثناء التشغيل",
        "interval_s": "الفاصل (s):",
        "max_hz": "الحد الأقصى Hz:",
        "ymin_dbfs": "Y الأدنى (dBFS):",
        "ymax_dbfs": "Y الأقصى (dBFS):",
        "smooth_time": "تنعيم الزمن:",
        "smooth_freq": "تنعيم التردد:",
        "fps": "FPS:",
        "corr_points": "نقاط الترابط:",
        "corr_alpha": "ألفا الترابط:",
        "corr_size": "حجم الترابط:",
        "err": "خطأ",
        "warn": "تحذير",
        "info": "معلومات",
        "invalid_settings": "إعدادات غير صالحة: {e}",
        "apply_now_title": "تطبيق الآن؟",
        "apply_now_msg": "هذه التغييرات تتطلب إعادة تشغيل التشغيل. إعادة التشغيل الآن؟",
        "scan_already": "المسح قيد التشغيل بالفعل",
        "pick_station": "اختر محطة من القائمة",
        "station_not_found": "لم يتم العثور على بيانات المحطة",
        "need_playback_first": "ابدأ التشغيل أولاً",
        "bad_freq": "تردد غير صالح",
        "freq_out_of_range": "التردد خارج النطاق 88-108 MHz",
        "playing": "▶ قيد التشغيل: {name}",
        "stopped": "⏹ تم الإيقاف",
        "scanning": "🔍 جارٍ المسح...",
        "scanning_progress": "🔍 المسح: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ تم العثور على {found} محطة",
        "settings_saved": "تم حفظ الإعدادات",
        "now_playing": "قيد التشغيل: {text}",
    },

    "bn": {
        "app_title": "RTL-SDR FM রেডিও (RDS)",
        "title": "RDS সহ FM রেডিও",
        "status_ready": "প্রস্তুত",
        "manual_tuning": "ম্যানুয়াল টিউনিং",
        "frequency_mhz": "ফ্রিকোয়েন্সি (MHz):",
        "tune": "টিউন",
        "stations": "FM স্টেশন",
        "stations_col_freq": "MHz",
        "stations_col_name": "স্টেশন",
        "station_info": "স্টেশন তথ্য",
        "scan_band": "FM ব্যান্ড স্ক্যান করুন",
        "play": "চালু",
        "stop": "বন্ধ",
        "record_start": "রেকর্ডিং শুরু",
        "record_stop": "রেকর্ডিং বন্ধ",
        "sdr_audio_panel": "SDR এবং অডিও",
        "gain": "RTL-SDR গেইন:",
        "volume": "ভলিউম:",
        "settings": "সেটিংস...",
        "log": "লগ",
        "viz": "অডিও ভিজুয়ালাইজেশন",
        "spec_title": "স্পেকট্রাম (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "বাম",
        "right": "ডান",
        "corr_title": "স্টেরিও করেলেশন",
        "corr_xlabel": "বাম (L)",
        "corr_ylabel": "ডান (R)",
        "settings_title": "সেটিংস",
        "apply": "প্রয়োগ",
        "close": "বন্ধ",
        "group_sdr": "SDR",
        "group_audio": "অডিও / ডিমড",
        "group_rds": "RDS",
        "group_spectrum": "স্পেকট্রাম",
        "group_ui": "UI",
        "language": "ভাষা:",
        "recordings_dir": "রেকর্ডিং ফোল্ডার:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "ডি-এমফাসিস (50 µs)",
        "rds_updates": "চালানোর সময় RDS আপডেট",
        "interval_s": "ইন্টারভাল (s):",
        "max_hz": "সর্বোচ্চ Hz:",
        "ymin_dbfs": "Y মিন (dBFS):",
        "ymax_dbfs": "Y ম্যাক্স (dBFS):",
        "smooth_time": "টাইম স্মুথ:",
        "smooth_freq": "ফ্রিক স্মুথ:",
        "fps": "FPS:",
        "corr_points": "Corr পয়েন্ট:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr সাইজ:",
        "err": "ত্রুটি",
        "warn": "সতর্কতা",
        "info": "তথ্য",
        "invalid_settings": "অবৈধ সেটিংস: {e}",
        "apply_now_title": "এখন প্রয়োগ করবেন?",
        "apply_now_msg": "এই পরিবর্তনগুলোর জন্য প্লেব্যাক রিস্টার্ট দরকার। এখন রিস্টার্ট করবেন?",
        "scan_already": "স্ক্যান চলছে",
        "pick_station": "তালিকা থেকে স্টেশন নির্বাচন করুন",
        "station_not_found": "স্টেশন ডেটা পাওয়া যায়নি",
        "need_playback_first": "আগে প্লেব্যাক শুরু করুন",
        "bad_freq": "অবৈধ ফ্রিকোয়েন্সি",
        "freq_out_of_range": "ফ্রিকোয়েন্সি সীমার বাইরে 88-108 MHz",
        "playing": "▶ চলছে: {name}",
        "stopped": "⏹ বন্ধ",
        "scanning": "🔍 স্ক্যান হচ্ছে...",
        "scanning_progress": "🔍 স্ক্যান: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} স্টেশন পাওয়া গেছে",
        "settings_saved": "সেটিংস সংরক্ষিত",
        "now_playing": "এখন চলছে: {text}",
    },

    "ur": {
        "app_title": "RTL-SDR FM ریڈیو (RDS)",
        "title": "RDS کے ساتھ FM ریڈیو",
        "status_ready": "تیار",
        "manual_tuning": "دستی ٹیوننگ",
        "frequency_mhz": "فریکوئنسی (MHz):",
        "tune": "ٹیون",
        "stations": "FM اسٹیشن",
        "stations_col_freq": "MHz",
        "stations_col_name": "اسٹیشن",
        "station_info": "اسٹیشن معلومات",
        "scan_band": "FM بینڈ اسکین کریں",
        "play": "چلائیں",
        "stop": "روکیں",
        "record_start": "ریکارڈنگ شروع کریں",
        "record_stop": "ریکارڈنگ روکیں",
        "sdr_audio_panel": "SDR اور آڈیو",
        "gain": "RTL-SDR گین:",
        "volume": "آواز:",
        "settings": "سیٹنگز...",
        "log": "لاگ",
        "viz": "آڈیو ویژولائزیشن",
        "spec_title": "اسپیکٹرم (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "بائیں",
        "right": "دائیں",
        "corr_title": "اسٹیریو کوریلیشن",
        "corr_xlabel": "بائیں (L)",
        "corr_ylabel": "دائیں (R)",
        "settings_title": "سیٹنگز",
        "apply": "لاگو کریں",
        "close": "بند کریں",
        "group_sdr": "SDR",
        "group_audio": "آڈیو / ڈیموڈ",
        "group_rds": "RDS",
        "group_spectrum": "اسپیکٹرم",
        "group_ui": "UI",
        "language": "زبان:",
        "recordings_dir": "ریکارڈنگ فولڈر:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "ڈی-ایمفیسس (50 µs)",
        "rds_updates": "پلے بیک کے دوران RDS اپڈیٹ کریں",
        "interval_s": "وقفہ (s):",
        "max_hz": "زیادہ سے زیادہ Hz:",
        "ymin_dbfs": "Y کم (dBFS):",
        "ymax_dbfs": "Y زیادہ (dBFS):",
        "smooth_time": "وقت اسموٹھ:",
        "smooth_freq": "فریک اسموٹھ:",
        "fps": "FPS:",
        "corr_points": "Corr پوائنٹس:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr سائز:",
        "err": "خرابی",
        "warn": "انتباہ",
        "info": "معلومات",
        "invalid_settings": "غلط سیٹنگز: {e}",
        "apply_now_title": "ابھی لاگو کریں؟",
        "apply_now_msg": "ان تبدیلیوں کے لیے پلے بیک دوبارہ شروع کرنا ہوگا۔ ابھی ری اسٹارٹ کریں؟",
        "scan_already": "اسکین چل رہا ہے",
        "pick_station": "فہرست سے اسٹیشن منتخب کریں",
        "station_not_found": "اسٹیشن ڈیٹا نہیں ملا",
        "need_playback_first": "پہلے پلے بیک شروع کریں",
        "bad_freq": "غلط فریکوئنسی",
        "freq_out_of_range": "فریکوئنسی حد سے باہر 88-108 MHz",
        "playing": "▶ چل رہا: {name}",
        "stopped": "⏹ روک دیا گیا",
        "scanning": "🔍 اسکین ہو رہا ہے...",
        "scanning_progress": "🔍 اسکین: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} اسٹیشن ملے",
        "settings_saved": "سیٹنگز محفوظ ہو گئیں",
        "now_playing": "اب چل رہا: {text}",
    },

    "sw": {
        "app_title": "Redio ya FM RTL-SDR yenye RDS",
        "title": "Redio ya FM yenye RDS",
        "status_ready": "Tayari",
        "manual_tuning": "Uwekaji mkono",
        "frequency_mhz": "Masafa (MHz):",
        "tune": "Weka",
        "stations": "Vituo vya FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "Kituo",
        "station_info": "Taarifa za kituo",
        "scan_band": "Changanua bendi ya FM",
        "play": "Cheza",
        "stop": "Simamisha",
        "record_start": "Anza kurekodi",
        "record_stop": "Acha kurekodi",
        "sdr_audio_panel": "SDR na sauti",
        "gain": "Gain ya RTL-SDR:",
        "volume": "Sauti:",
        "settings": "Mipangilio...",
        "log": "Log",
        "viz": "Uonyeshaji wa sauti",
        "spec_title": "Spektra (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "Kushoto",
        "right": "Kulia",
        "corr_title": "Uhusiano wa stereo",
        "corr_xlabel": "Kushoto (L)",
        "corr_ylabel": "Kulia (R)",
        "settings_title": "Mipangilio",
        "apply": "Tumia",
        "close": "Funga",
        "group_sdr": "SDR",
        "group_audio": "Sauti / Demod",
        "group_rds": "RDS",
        "group_spectrum": "Spektra",
        "group_ui": "UI",
        "language": "Lugha:",
        "recordings_dir": "Folda ya rekodi:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "De-emphasis (50 µs)",
        "rds_updates": "Sasisha RDS wakati wa kusikiliza",
        "interval_s": "Muda (s):",
        "max_hz": "Hz max:",
        "ymin_dbfs": "Y min (dBFS):",
        "ymax_dbfs": "Y max (dBFS):",
        "smooth_time": "Lainisha muda:",
        "smooth_freq": "Lainisha freq:",
        "fps": "FPS:",
        "corr_points": "Pointi corr:",
        "corr_alpha": "Alpha corr:",
        "corr_size": "Ukubwa corr:",
        "err": "Hitilafu",
        "warn": "Onyo",
        "info": "Info",
        "invalid_settings": "Mipangilio batili: {e}",
        "apply_now_title": "Tumia sasa?",
        "apply_now_msg": "Mabadiliko haya yanahitaji kuanzisha upya uchezaji. Anzisha upya sasa?",
        "scan_already": "Uchanganuzi unaendelea",
        "pick_station": "Chagua kituo kutoka kwenye orodha",
        "station_not_found": "Taarifa za kituo hazipatikani",
        "need_playback_first": "Anza kucheza kwanza",
        "bad_freq": "Masafa batili",
        "freq_out_of_range": "Masafa nje ya 88-108 MHz",
        "playing": "▶ Inacheza: {name}",
        "stopped": "⏹ Imesimama",
        "scanning": "🔍 Inachanganua...",
        "scanning_progress": "🔍 Uchanganuzi: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ Vituo {found} vimepatikana",
        "settings_saved": "Mipangilio imehifadhiwa",
        "now_playing": "Sasa inacheza: {text}",
    },

    "mr": {
        "app_title": "RTL-SDR FM रेडिओ (RDS)",
        "title": "RDS सह FM रेडिओ",
        "status_ready": "तयार",
        "manual_tuning": "हस्तचालित ट्यूनिंग",
        "frequency_mhz": "वारंवारता (MHz):",
        "tune": "ट्यून",
        "stations": "FM स्टेशन",
        "stations_col_freq": "MHz",
        "stations_col_name": "स्टेशन",
        "station_info": "स्टेशन माहिती",
        "scan_band": "FM बँड स्कॅन करा",
        "play": "प्ले",
        "stop": "थांबवा",
        "record_start": "रेकॉर्डिंग सुरू करा",
        "record_stop": "रेकॉर्डिंग थांबवा",
        "sdr_audio_panel": "SDR आणि ऑडिओ",
        "gain": "RTL-SDR गेन:",
        "volume": "आवाज:",
        "settings": "सेटिंग्ज...",
        "log": "लॉग",
        "viz": "ऑडिओ दृश्य",
        "spec_title": "स्पेक्ट्रम (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "डावा",
        "right": "उजवा",
        "corr_title": "स्टेरिओ सहसंबंध",
        "corr_xlabel": "डावा (L)",
        "corr_ylabel": "उजवा (R)",
        "settings_title": "सेटिंग्ज",
        "apply": "लागू करा",
        "close": "बंद",
        "group_sdr": "SDR",
        "group_audio": "ऑडिओ / डिमॉड",
        "group_rds": "RDS",
        "group_spectrum": "स्पेक्ट्रम",
        "group_ui": "UI",
        "language": "भाषा:",
        "recordings_dir": "रेकॉर्डिंग फोल्डर:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "डी-एम्फॅसिस (50 µs)",
        "rds_updates": "प्ले दरम्यान RDS अपडेट",
        "interval_s": "अंतर (s):",
        "max_hz": "कमाल Hz:",
        "ymin_dbfs": "Y मिन (dBFS):",
        "ymax_dbfs": "Y मॅक्स (dBFS):",
        "smooth_time": "वेळ स्मूथ:",
        "smooth_freq": "फ्रिक स्मूथ:",
        "fps": "FPS:",
        "corr_points": "Corr पॉइंट:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr साइज:",
        "err": "त्रुटी",
        "warn": "इशारा",
        "info": "माहिती",
        "invalid_settings": "अवैध सेटिंग्ज: {e}",
        "apply_now_title": "आत्ता लागू करायचे?",
        "apply_now_msg": "या बदलांसाठी प्लेबॅक रीस्टार्ट करावा लागेल. आत्ता रीस्टार्ट करायचा?",
        "scan_already": "स्कॅन सुरू आहे",
        "pick_station": "यादीतून स्टेशन निवडा",
        "station_not_found": "स्टेशन डेटा सापडला नाही",
        "need_playback_first": "आधी प्लेबॅक सुरू करा",
        "bad_freq": "अवैध वारंवारता",
        "freq_out_of_range": "वारंवारता 88-108 MHz च्या बाहेर",
        "playing": "▶ वाजत आहे: {name}",
        "stopped": "⏹ थांबवले",
        "scanning": "🔍 स्कॅन होत आहे...",
        "scanning_progress": "🔍 स्कॅन: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} स्टेशन सापडली",
        "settings_saved": "सेटिंग्ज जतन झाली",
        "now_playing": "आता वाजत आहे: {text}",
    },

    "te": {
        "app_title": "RTL-SDR FM రేడియో (RDS)",
        "title": "RDS తో FM రేడియో",
        "status_ready": "సిద్ధం",
        "manual_tuning": "మాన్యువల్ ట్యూనింగ్",
        "frequency_mhz": "ఫ్రీక్వెన్సీ (MHz):",
        "tune": "ట్యూన్",
        "stations": "FM స్టేషన్లు",
        "stations_col_freq": "MHz",
        "stations_col_name": "స్టేషన్",
        "station_info": "స్టేషన్ సమాచారం",
        "scan_band": "FM బ్యాండ్ స్కాన్ చేయండి",
        "play": "ప్లే",
        "stop": "ఆపు",
        "record_start": "రికార్డింగ్ ప్రారంభించు",
        "record_stop": "రికార్డింగ్ ఆపు",
        "sdr_audio_panel": "SDR మరియు ఆడియో",
        "gain": "RTL-SDR గెయిన్:",
        "volume": "వాల్యూమ్:",
        "settings": "సెట్టింగ్స్...",
        "log": "లాగ్",
        "viz": "ఆడియో విజువలైజేషన్",
        "spec_title": "స్పెక్ట్రం (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "ఎడమ",
        "right": "కుడి",
        "corr_title": "స్టీరియో కొరిలేషన్",
        "corr_xlabel": "ఎడమ (L)",
        "corr_ylabel": "కుడి (R)",
        "settings_title": "సెట్టింగ్స్",
        "apply": "వర్తించు",
        "close": "మూసివేయి",
        "group_sdr": "SDR",
        "group_audio": "ఆడియో / డీమాడ్",
        "group_rds": "RDS",
        "group_spectrum": "స్పెక్ట్రం",
        "group_ui": "UI",
        "language": "భాష:",
        "recordings_dir": "రికార్డింగ్స్ ఫోల్డర్:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "డి-ఎమ్ఫాసిస్ (50 µs)",
        "rds_updates": "ప్లే సమయంలో RDS అప్డేట్",
        "interval_s": "ఇంటర్వల్ (s):",
        "max_hz": "మ్యాక్స్ Hz:",
        "ymin_dbfs": "Y మిన్ (dBFS):",
        "ymax_dbfs": "Y మ్యాక్స్ (dBFS):",
        "smooth_time": "టైమ్ స్మూత్:",
        "smooth_freq": "ఫ్రీక్వెన్సీ స్మూత్:",
        "fps": "FPS:",
        "corr_points": "Corr పాయింట్స్:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr సైజ్:",
        "err": "లోపం",
        "warn": "హెచ్చరిక",
        "info": "సమాచారం",
        "invalid_settings": "చెల్లని సెట్టింగ్స్: {e}",
        "apply_now_title": "ఇప్పుడే వర్తింపచేయాలా?",
        "apply_now_msg": "ఈ మార్పులకు ప్లేబ్యాక్ రీస్టార్ట్ అవసరం. ఇప్పుడే రీస్టార్ట్ చేయాలా?",
        "scan_already": "స్కాన్ జరుగుతోంది",
        "pick_station": "జాబితా నుండి స్టేషన్ ఎంచుకోండి",
        "station_not_found": "స్టేషన్ డేటా దొరకలేదు",
        "need_playback_first": "ముందుగా ప్లే ప్రారంభించండి",
        "bad_freq": "చెల్లని ఫ్రీక్వెన్సీ",
        "freq_out_of_range": "ఫ్రీక్వెన్సీ 88-108 MHz పరిధి బయట",
        "playing": "▶ ప్లే అవుతోంది: {name}",
        "stopped": "⏹ ఆపబడింది",
        "scanning": "🔍 స్కాన్ అవుతోంది...",
        "scanning_progress": "🔍 స్కాన్: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} స్టేషన్లు కనుగొన్నాయి",
        "settings_saved": "సెట్టింగ్స్ సేవ్ అయ్యాయి",
        "now_playing": "ఇప్పుడు ప్లే అవుతోంది: {text}",
    },

    "ta": {
        "app_title": "RTL-SDR FM வானொலி (RDS)",
        "title": "RDS உடன் FM வானொலி",
        "status_ready": "தயார்",
        "manual_tuning": "கைமுறை ட்யூனிங்",
        "frequency_mhz": "அதிர்வெண் (MHz):",
        "tune": "ட்யூன்",
        "stations": "FM நிலையங்கள்",
        "stations_col_freq": "MHz",
        "stations_col_name": "நிலை",
        "station_info": "நிலைய தகவல்",
        "scan_band": "FM பேண்ட் ஸ்கேன்",
        "play": "இயக்கு",
        "stop": "நிறுத்து",
        "record_start": "பதிவு தொடங்கு",
        "record_stop": "பதிவு நிறுத்து",
        "sdr_audio_panel": "SDR மற்றும் ஆடியோ",
        "gain": "RTL-SDR கெயின்:",
        "volume": "ஒலி அளவு:",
        "settings": "அமைப்புகள்...",
        "log": "லாக்",
        "viz": "ஆடியோ காட்சி",
        "spec_title": "ஸ்பெக்ட்ரம் (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "இடது",
        "right": "வலது",
        "corr_title": "ஸ்டீரியோ தொடர்பு",
        "corr_xlabel": "இடது (L)",
        "corr_ylabel": "வலது (R)",
        "settings_title": "அமைப்புகள்",
        "apply": "பயன்படுத்து",
        "close": "மூடு",
        "group_sdr": "SDR",
        "group_audio": "ஆடியோ / டிமாட்",
        "group_rds": "RDS",
        "group_spectrum": "ஸ்பெக்ட்ரம்",
        "group_ui": "UI",
        "language": "மொழி:",
        "recordings_dir": "பதிவு கோப்புறை:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "டி-எம்ஃபாசிஸ் (50 µs)",
        "rds_updates": "இயக்கும் போது RDS புதுப்பி",
        "interval_s": "இடைவேளை (s):",
        "max_hz": "அதிகபட்ச Hz:",
        "ymin_dbfs": "Y குறை (dBFS):",
        "ymax_dbfs": "Y அதி (dBFS):",
        "smooth_time": "நேர ஸ்மூத்:",
        "smooth_freq": "அதிர்வு ஸ்மூத்:",
        "fps": "FPS:",
        "corr_points": "Corr புள்ளிகள்:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr அளவு:",
        "err": "பிழை",
        "warn": "எச்சரிக்கை",
        "info": "தகவல்",
        "invalid_settings": "தவறான அமைப்புகள்: {e}",
        "apply_now_title": "இப்போது பயன்படுத்தவா?",
        "apply_now_msg": "இந்த மாற்றங்களுக்கு பிளேபேக் ரீஸ்டார்ட் தேவை. இப்போது ரீஸ்டார்ட் செய்யலாமா?",
        "scan_already": "ஸ்கேன் நடைபெறுகிறது",
        "pick_station": "பட்டியலில் இருந்து நிலையத்தை தேர்வு செய்யவும்",
        "station_not_found": "நிலைய தரவு கிடைக்கவில்லை",
        "need_playback_first": "முதலில் பிளே தொடங்கவும்",
        "bad_freq": "தவறான அதிர்வெண்",
        "freq_out_of_range": "அதிர்வெண் 88-108 MHz வரம்பிற்கு வெளியே",
        "playing": "▶ இயங்குகிறது: {name}",
        "stopped": "⏹ நிறுத்தப்பட்டது",
        "scanning": "🔍 ஸ்கேன்...",
        "scanning_progress": "🔍 ஸ்கேன்: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} நிலையங்கள் கிடைத்தன",
        "settings_saved": "அமைப்புகள் சேமிக்கப்பட்டது",
        "now_playing": "இப்போது இயங்குவது: {text}",
    },

    "th": {
        "app_title": "วิทยุ FM RTL-SDR พร้อม RDS",
        "title": "วิทยุ FM พร้อม RDS",
        "status_ready": "พร้อม",
        "manual_tuning": "ปรับจูนด้วยตนเอง",
        "frequency_mhz": "ความถี่ (MHz):",
        "tune": "จูน",
        "stations": "สถานี FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "สถานี",
        "station_info": "ข้อมูลสถานี",
        "scan_band": "สแกนย่าน FM",
        "play": "เล่น",
        "stop": "หยุด",
        "record_start": "เริ่มบันทึก",
        "record_stop": "หยุดบันทึก",
        "sdr_audio_panel": "SDR และเสียง",
        "gain": "เกน RTL-SDR:",
        "volume": "ระดับเสียง:",
        "settings": "การตั้งค่า...",
        "log": "บันทึก",
        "viz": "การแสดงผลเสียง",
        "spec_title": "สเปกตรัม (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "ซ้าย",
        "right": "ขวา",
        "corr_title": "สหสัมพันธ์สเตอริโอ",
        "corr_xlabel": "ซ้าย (L)",
        "corr_ylabel": "ขวา (R)",
        "settings_title": "การตั้งค่า",
        "apply": "ใช้",
        "close": "ปิด",
        "group_sdr": "SDR",
        "group_audio": "เสียง / Demod",
        "group_rds": "RDS",
        "group_spectrum": "สเปกตรัม",
        "group_ui": "UI",
        "language": "ภาษา:",
        "recordings_dir": "โฟลเดอร์บันทึกเสียง:",
        "osmosdr_args": "พารามิเตอร์ osmosdr:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "ดีเอ็มฟาซิส (50 µs)",
        "rds_updates": "อัปเดต RDS ระหว่างเล่น",
        "interval_s": "ช่วงเวลา (s):",
        "max_hz": "Hz สูงสุด:",
        "ymin_dbfs": "Y ต่ำสุด (dBFS):",
        "ymax_dbfs": "Y สูงสุด (dBFS):",
        "smooth_time": "ทำให้เรียบเวลา:",
        "smooth_freq": "ทำให้เรียบความถี่:",
        "fps": "FPS:",
        "corr_points": "จุด corr:",
        "corr_alpha": "corr alpha:",
        "corr_size": "ขนาด corr:",
        "err": "ข้อผิดพลาด",
        "warn": "คำเตือน",
        "info": "ข้อมูล",
        "invalid_settings": "การตั้งค่าไม่ถูกต้อง: {e}",
        "apply_now_title": "ใช้ตอนนี้?",
        "apply_now_msg": "การเปลี่ยนแปลงนี้ต้องเริ่มเล่นใหม่ เริ่มใหม่ตอนนี้?",
        "scan_already": "กำลังสแกนอยู่แล้ว",
        "pick_station": "เลือกสถานีจากรายการ",
        "station_not_found": "ไม่พบข้อมูลสถานี",
        "need_playback_first": "เริ่มเล่นก่อน",
        "bad_freq": "ความถี่ไม่ถูกต้อง",
        "freq_out_of_range": "ความถี่นอกช่วง 88-108 MHz",
        "playing": "▶ กำลังเล่น: {name}",
        "stopped": "⏹ หยุดแล้ว",
        "scanning": "🔍 กำลังสแกน...",
        "scanning_progress": "🔍 สแกน: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ พบ {found} สถานี",
        "settings_saved": "บันทึกการตั้งค่าแล้ว",
        "now_playing": "กำลังเล่น: {text}",
    },

    "gu": {
        "app_title": "RTL-SDR FM રેડિયો (RDS)",
        "title": "RDS સાથે FM રેડિયો",
        "status_ready": "તૈયાર",
        "manual_tuning": "મેન્યુઅલ ટ્યુનિંગ",
        "frequency_mhz": "આવર્તન (MHz):",
        "tune": "ટ્યુન",
        "stations": "FM સ્ટેશનો",
        "stations_col_freq": "MHz",
        "stations_col_name": "સ્ટેશન",
        "station_info": "સ્ટેશન માહિતી",
        "scan_band": "FM બેન્ડ સ્કેન કરો",
        "play": "ચાલુ કરો",
        "stop": "બંધ કરો",
        "record_start": "રેકોર્ડિંગ શરૂ કરો",
        "record_stop": "રેકોર્ડિંગ બંધ કરો",
        "sdr_audio_panel": "SDR અને ઑડિયો",
        "gain": "RTL-SDR ગેઇન:",
        "volume": "વોલ્યૂમ:",
        "settings": "સેટિંગ્સ...",
        "log": "લોગ",
        "viz": "ઑડિયો વિઝ્યુઅલાઇઝેશન",
        "spec_title": "સ્પેક્ટ્રમ (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "ડાબું",
        "right": "જમણું",
        "corr_title": "સ્ટીરિયો કરેલેશન",
        "corr_xlabel": "ડાબું (L)",
        "corr_ylabel": "જમણું (R)",
        "settings_title": "સેટિંગ્સ",
        "apply": "લાગુ કરો",
        "close": "બંધ",
        "group_sdr": "SDR",
        "group_audio": "ઑડિયો / ડિમૉડ",
        "group_rds": "RDS",
        "group_spectrum": "સ્પેક્ટ્રમ",
        "group_ui": "UI",
        "language": "ભાષા:",
        "recordings_dir": "રેકોર્ડિંગ ફોલ્ડર:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "ડી-એમ્ફેસિસ (50 µs)",
        "rds_updates": "પ્લેબેક દરમિયાન RDS અપડેટ",
        "interval_s": "અંતરાલ (s):",
        "max_hz": "મૅક્સ Hz:",
        "ymin_dbfs": "Y મિન (dBFS):",
        "ymax_dbfs": "Y મૅક્સ (dBFS):",
        "smooth_time": "ટાઇમ સ્મૂથ:",
        "smooth_freq": "ફ્રિક સ્મૂથ:",
        "fps": "FPS:",
        "corr_points": "Corr પોઈન્ટ:",
        "corr_alpha": "Corr alpha:",
        "corr_size": "Corr સાઇઝ:",
        "err": "ભૂલ",
        "warn": "ચેતવણી",
        "info": "માહિતી",
        "invalid_settings": "અમાન્ય સેટિંગ્સ: {e}",
        "apply_now_title": "હમણાં લાગુ કરવું?",
        "apply_now_msg": "આ ફેરફારો માટે પ્લેબેક ફરી શરૂ કરવું પડશે. હમણાં ફરી શરૂ કરશો?",
        "scan_already": "સ્કેન ચાલી રહ્યું છે",
        "pick_station": "યાદીમાંથી સ્ટેશન પસંદ કરો",
        "station_not_found": "સ્ટેશન ડેટા મળ્યું નથી",
        "need_playback_first": "પહેલાં પ્લેબેક શરૂ કરો",
        "bad_freq": "અમાન્ય આવર્તન",
        "freq_out_of_range": "આવર્તન 88-108 MHz બહાર છે",
        "playing": "▶ ચલુ છે: {name}",
        "stopped": "⏹ બંધ",
        "scanning": "🔍 સ્કેન થઈ રહ્યું છે...",
        "scanning_progress": "🔍 સ્કેન: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} સ્ટેશનો મળ્યા",
        "settings_saved": "સેટિંગ્સ સંગ્રહિત",
        "now_playing": "હમણાં ચાલે છે: {text}",
    },

    "fa": {
        "app_title": "رادیو FM RTL-SDR با RDS",
        "title": "رادیو FM با RDS",
        "status_ready": "آماده",
        "manual_tuning": "تنظیم دستی",
        "frequency_mhz": "فرکانس (MHz):",
        "tune": "تنظیم",
        "stations": "ایستگاه‌های FM",
        "stations_col_freq": "MHz",
        "stations_col_name": "ایستگاه",
        "station_info": "اطلاعات ایستگاه",
        "scan_band": "اسکن باند FM",
        "play": "پخش",
        "stop": "توقف",
        "record_start": "شروع ضبط",
        "record_stop": "توقف ضبط",
        "sdr_audio_panel": "SDR و صدا",
        "gain": "گِین RTL-SDR:",
        "volume": "صدا:",
        "settings": "تنظیمات...",
        "log": "لاگ",
        "viz": "نمایش صوت",
        "spec_title": "طیف (dBFS) L/R",
        "spec_ylabel": "dBFS",
        "left": "چپ",
        "right": "راست",
        "corr_title": "هم‌بستگی استریو",
        "corr_xlabel": "چپ (L)",
        "corr_ylabel": "راست (R)",
        "settings_title": "تنظیمات",
        "apply": "اعمال",
        "close": "بستن",
        "group_sdr": "SDR",
        "group_audio": "صدا / دیمود",
        "group_rds": "RDS",
        "group_spectrum": "طیف",
        "group_ui": "رابط کاربری",
        "language": "زبان:",
        "recordings_dir": "پوشه ضبط‌ها:",
        "osmosdr_args": "osmosdr args:",
        "ppm": "PPM:",
        "bw_khz": "BW (kHz):",
        "demod_rate": "demod_rate (Hz):",
        "audio_rate": "audio_rate (Hz):",
        "deemphasis": "دی-امفاسیس (50 µs)",
        "rds_updates": "به‌روزرسانی RDS هنگام پخش",
        "interval_s": "بازه (s):",
        "max_hz": "حداکثر Hz:",
        "ymin_dbfs": "Y حداقل (dBFS):",
        "ymax_dbfs": "Y حداکثر (dBFS):",
        "smooth_time": "هموارسازی زمان:",
        "smooth_freq": "هموارسازی فرکانس:",
        "fps": "FPS:",
        "corr_points": "نقاط هم‌بستگی:",
        "corr_alpha": "آلفا هم‌بستگی:",
        "corr_size": "اندازه هم‌بستگی:",
        "err": "خطا",
        "warn": "هشدار",
        "info": "اطلاعات",
        "invalid_settings": "تنظیمات نامعتبر: {e}",
        "apply_now_title": "الان اعمال شود؟",
        "apply_now_msg": "این تغییرات نیاز به راه‌اندازی مجدد پخش دارد. الان راه‌اندازی مجدد شود؟",
        "scan_already": "اسکن در حال اجراست",
        "pick_station": "یک ایستگاه از لیست انتخاب کنید",
        "station_not_found": "اطلاعات ایستگاه پیدا نشد",
        "need_playback_first": "ابتدا پخش را شروع کنید",
        "bad_freq": "فرکانس نامعتبر",
        "freq_out_of_range": "فرکانس خارج از بازه 88-108 MHz",
        "playing": "▶ در حال پخش: {name}",
        "stopped": "⏹ متوقف شد",
        "scanning": "🔍 در حال اسکن...",
        "scanning_progress": "🔍 اسکن: {freq:.1f} MHz ({progress:.0f}%)",
        "scan_done": "✓ {found} ایستگاه پیدا شد",
        "settings_saved": "تنظیمات ذخیره شد",
        "now_playing": "در حال پخش: {text}",
    },
}

# Ensure newer UI keys exist in *all* language tables.
# We use setdefault so we never override existing translations.
I18N_EXTRA = {
    "ar": {
        "save": "حفظ",
        "dark_mode": "الوضع الداكن:",
        "fm_band": "نطاق FM:",
        "unknown": "غير معروف",
        "err_demod_audio_positive": "demod_rate/audio_rate يجب أن تكون > 0",
        "err_demod_multiple_audio": "يجب أن يكون demod_rate مضاعفًا لـ audio_rate",
        "err_ymax_gt_ymin": "يجب أن تكون Y max > Y min",
        "err_smooth_time_range": "يجب أن يكون تنعيم الوقت ضمن [0..1]",
        "err_smooth_freq_range": "يجب أن يكون تنعيم التردد ضمن [0..10]",
        "err_fps_range": "يجب أن يكون FPS ضمن [10..120]",
        "err_corr_points_range": "يجب أن تكون نقاط Corr ضمن [64..2048]",
        "err_corr_alpha_range": "يجب أن تكون Corr alpha ضمن [0.05..1]",
        "err_corr_size_range": "يجب أن يكون حجم Corr ضمن [1..8]",
        "recording_log": "تسجيل: {file}",
        "recording_status": "تسجيل: {file} ({size_mb:.2f} MB) | إدخال PCM: {mb_in:.2f} MB",
        "record_saved": "تم الحفظ: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "تم حفظ الملف: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "تم إيقاف التسجيل",
        "recording_file_prefix": "تسجيل",
        "cannot_start_recording": "لا يمكن بدء التسجيل: {e}",
    },
    "bn": {
        "save": "সংরক্ষণ",
        "dark_mode": "ডার্ক মোড:",
        "fm_band": "FM ব্যান্ড:",
        "unknown": "অজানা",
        "err_demod_audio_positive": "demod_rate/audio_rate অবশ্যই > 0 হতে হবে",
        "err_demod_multiple_audio": "demod_rate অবশ্যই audio_rate-এর গুণিতক হতে হবে",
        "err_ymax_gt_ymin": "Y max অবশ্যই Y min-এর চেয়ে বড় হতে হবে",
        "err_smooth_time_range": "সময় স্মুথিং অবশ্যই [0..1]-এর মধ্যে হতে হবে",
        "err_smooth_freq_range": "ফ্রিকোয়েন্সি স্মুথিং অবশ্যই [0..10]-এর মধ্যে হতে হবে",
        "err_fps_range": "FPS অবশ্যই [10..120]-এর মধ্যে হতে হবে",
        "err_corr_points_range": "Corr পয়েন্ট অবশ্যই [64..2048]-এর মধ্যে হতে হবে",
        "err_corr_alpha_range": "Corr alpha অবশ্যই [0.05..1]-এর মধ্যে হতে হবে",
        "err_corr_size_range": "Corr সাইজ অবশ্যই [1..8]-এর মধ্যে হতে হবে",
        "recording_log": "রেকর্ডিং: {file}",
        "recording_status": "রেকর্ডিং: {file} ({size_mb:.2f} MB) | PCM ইনপুট: {mb_in:.2f} MB",
        "record_saved": "সংরক্ষিত: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "ফাইল সংরক্ষিত: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "রেকর্ডিং বন্ধ",
        "recording_file_prefix": "রেকর্ডিং",
        "cannot_start_recording": "রেকর্ডিং শুরু করা যায়নি: {e}",
    },
    "de": {
        "save": "Speichern",
        "dark_mode": "Dunkelmodus:",
        "fm_band": "UKW-Band:",
        "unknown": "Unbekannt",
        "err_demod_audio_positive": "demod_rate/audio_rate muss > 0 sein",
        "err_demod_multiple_audio": "demod_rate muss ein Vielfaches von audio_rate sein",
        "err_ymax_gt_ymin": "Y max muss > Y min sein",
        "err_smooth_time_range": "Zeitglättung muss in [0..1] liegen",
        "err_smooth_freq_range": "Frequenzglättung muss in [0..10] liegen",
        "err_fps_range": "FPS muss in [10..120] liegen",
        "err_corr_points_range": "Corr-Punkte müssen in [64..2048] liegen",
        "err_corr_alpha_range": "Corr-Alpha muss in [0.05..1] liegen",
        "err_corr_size_range": "Corr-Größe muss in [1..8] liegen",
        "recording_log": "Aufnahme: {file}",
        "recording_status": "Aufnahme: {file} ({size_mb:.2f} MB) | PCM-Eingang: {mb_in:.2f} MB",
        "record_saved": "Gespeichert: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Datei gespeichert: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Aufnahme beendet",
        "recording_file_prefix": "aufnahme",
        "cannot_start_recording": "Aufnahme konnte nicht gestartet werden: {e}",
    },
    "es": {
        "save": "Guardar",
        "dark_mode": "Modo oscuro:",
        "fm_band": "Banda FM:",
        "unknown": "Desconocido",
        "err_demod_audio_positive": "demod_rate/audio_rate debe ser > 0",
        "err_demod_multiple_audio": "demod_rate debe ser múltiplo de audio_rate",
        "err_ymax_gt_ymin": "Y max debe ser > Y min",
        "err_smooth_time_range": "El suavizado de tiempo debe estar en [0..1]",
        "err_smooth_freq_range": "El suavizado de frecuencia debe estar en [0..10]",
        "err_fps_range": "FPS debe estar en [10..120]",
        "err_corr_points_range": "Los puntos Corr deben estar en [64..2048]",
        "err_corr_alpha_range": "Corr alpha debe estar en [0.05..1]",
        "err_corr_size_range": "El tamaño Corr debe estar en [1..8]",
        "recording_log": "Grabación: {file}",
        "recording_status": "Grabación: {file} ({size_mb:.2f} MB) | Entrada PCM: {mb_in:.2f} MB",
        "record_saved": "Guardado: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Archivo guardado: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Grabación detenida",
        "recording_file_prefix": "grabacion",
        "cannot_start_recording": "No se puede iniciar la grabación: {e}",
    },
    "fr": {
        "save": "Enregistrer",
        "dark_mode": "Mode sombre :",
        "fm_band": "Bande FM :",
        "unknown": "Inconnu",
        "err_demod_audio_positive": "demod_rate/audio_rate doit être > 0",
        "err_demod_multiple_audio": "demod_rate doit être un multiple de audio_rate",
        "err_ymax_gt_ymin": "Y max doit être > Y min",
        "err_smooth_time_range": "Le lissage temporel doit être dans [0..1]",
        "err_smooth_freq_range": "Le lissage en fréquence doit être dans [0..10]",
        "err_fps_range": "FPS doit être dans [10..120]",
        "err_corr_points_range": "Les points Corr doivent être dans [64..2048]",
        "err_corr_alpha_range": "Corr alpha doit être dans [0.05..1]",
        "err_corr_size_range": "La taille Corr doit être dans [1..8]",
        "recording_log": "Enregistrement : {file}",
        "recording_status": "Enregistrement : {file} ({size_mb:.2f} MB) | Entrée PCM : {mb_in:.2f} MB",
        "record_saved": "Enregistré : {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Fichier enregistré : {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Enregistrement arrêté",
        "recording_file_prefix": "enregistrement",
        "cannot_start_recording": "Impossible de démarrer l'enregistrement : {e}",
    },
    "it": {
        "save": "Salva",
        "dark_mode": "Modalità scura:",
        "fm_band": "Banda FM:",
        "unknown": "Sconosciuto",
        "err_demod_audio_positive": "demod_rate/audio_rate deve essere > 0",
        "err_demod_multiple_audio": "demod_rate deve essere un multiplo di audio_rate",
        "err_ymax_gt_ymin": "Y max deve essere > Y min",
        "err_smooth_time_range": "Lo smoothing temporale deve essere in [0..1]",
        "err_smooth_freq_range": "Lo smoothing in frequenza deve essere in [0..10]",
        "err_fps_range": "FPS deve essere in [10..120]",
        "err_corr_points_range": "I punti Corr devono essere in [64..2048]",
        "err_corr_alpha_range": "Corr alpha deve essere in [0.05..1]",
        "err_corr_size_range": "La dimensione Corr deve essere in [1..8]",
        "recording_file_prefix": "registrazione",
        "cannot_start_recording": "Impossibile avviare la registrazione: {e}",
    },
    "pt": {
        "save": "Salvar",
        "dark_mode": "Modo escuro:",
        "fm_band": "Banda FM:",
        "unknown": "Desconhecido",
        "err_demod_audio_positive": "demod_rate/audio_rate deve ser > 0",
        "err_demod_multiple_audio": "demod_rate deve ser múltiplo de audio_rate",
        "err_ymax_gt_ymin": "Y max deve ser > Y min",
        "err_smooth_time_range": "Suavização no tempo deve estar em [0..1]",
        "err_smooth_freq_range": "Suavização em frequência deve estar em [0..10]",
        "err_fps_range": "FPS deve estar em [10..120]",
        "err_corr_points_range": "Pontos Corr devem estar em [64..2048]",
        "err_corr_alpha_range": "Corr alpha deve estar em [0.05..1]",
        "err_corr_size_range": "Tamanho Corr deve estar em [1..8]",
        "recording_log": "Gravação: {file}",
        "recording_status": "Gravação: {file} ({size_mb:.2f} MB) | Entrada PCM: {mb_in:.2f} MB",
        "record_saved": "Salvo: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Arquivo salvo: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Gravação interrompida",
        "recording_file_prefix": "gravacao",
        "cannot_start_recording": "Não foi possível iniciar a gravação: {e}",
    },
    "ru": {
        "save": "Сохранить",
        "dark_mode": "Тёмный режим:",
        "fm_band": "Диапазон FM:",
        "unknown": "Неизвестно",
        "err_demod_audio_positive": "demod_rate/audio_rate должны быть > 0",
        "err_demod_multiple_audio": "demod_rate должен быть кратен audio_rate",
        "err_ymax_gt_ymin": "Y max должен быть > Y min",
        "err_smooth_time_range": "Сглаживание по времени должно быть в [0..1]",
        "err_smooth_freq_range": "Сглаживание по частоте должно быть в [0..10]",
        "err_fps_range": "FPS должен быть в [10..120]",
        "err_corr_points_range": "Corr точки должны быть в [64..2048]",
        "err_corr_alpha_range": "Corr alpha должен быть в [0.05..1]",
        "err_corr_size_range": "Corr размер должен быть в [1..8]",
        "recording_log": "Запись: {file}",
        "recording_status": "Запись: {file} ({size_mb:.2f} MB) | Вход PCM: {mb_in:.2f} MB",
        "record_saved": "Сохранено: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Файл сохранён: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Запись остановлена",
        "recording_file_prefix": "запись",
        "cannot_start_recording": "Не удалось начать запись: {e}",
    },
    "id": {
        "save": "Simpan",
        "dark_mode": "Mode gelap:",
        "fm_band": "Pita FM:",
        "unknown": "Tidak diketahui",
        "err_demod_audio_positive": "demod_rate/audio_rate harus > 0",
        "err_demod_multiple_audio": "demod_rate harus kelipatan dari audio_rate",
        "err_ymax_gt_ymin": "Y max harus > Y min",
        "err_smooth_time_range": "Perataan waktu harus dalam [0..1]",
        "err_smooth_freq_range": "Perataan frekuensi harus dalam [0..10]",
        "err_fps_range": "FPS harus dalam [10..120]",
        "err_corr_points_range": "Titik Corr harus dalam [64..2048]",
        "err_corr_alpha_range": "Corr alpha harus dalam [0.05..1]",
        "err_corr_size_range": "Ukuran Corr harus dalam [1..8]",
        "recording_log": "Rekam: {file}",
        "recording_status": "Rekam: {file} ({size_mb:.2f} MB) | Masukan PCM: {mb_in:.2f} MB",
        "record_saved": "Tersimpan: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "File tersimpan: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Rekaman dihentikan",
        "recording_file_prefix": "rekam",
        "cannot_start_recording": "Tidak dapat memulai rekaman: {e}",
    },
    "tr": {
        "save": "Kaydet",
        "dark_mode": "Karanlık mod:",
        "fm_band": "FM bandı:",
        "unknown": "Bilinmiyor",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 olmalı",
        "err_demod_multiple_audio": "demod_rate, audio_rate'in katı olmalı",
        "err_ymax_gt_ymin": "Y max, Y min'den büyük olmalı",
        "err_smooth_time_range": "Zaman yumuşatma [0..1] aralığında olmalı",
        "err_smooth_freq_range": "Frekans yumuşatma [0..10] aralığında olmalı",
        "err_fps_range": "FPS [10..120] aralığında olmalı",
        "err_corr_points_range": "Corr noktaları [64..2048] aralığında olmalı",
        "err_corr_alpha_range": "Corr alpha [0.05..1] aralığında olmalı",
        "err_corr_size_range": "Corr boyutu [1..8] aralığında olmalı",
        "recording_log": "Kayıt: {file}",
        "recording_status": "Kayıt: {file} ({size_mb:.2f} MB) | PCM girişi: {mb_in:.2f} MB",
        "record_saved": "Kaydedildi: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Dosya kaydedildi: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Kayıt durduruldu",
        "recording_file_prefix": "kayit",
        "cannot_start_recording": "Kayıt başlatılamadı: {e}",
    },
    "vi": {
        "save": "Lưu",
        "dark_mode": "Chế độ tối:",
        "fm_band": "Băng FM:",
        "unknown": "Không rõ",
        "err_demod_audio_positive": "demod_rate/audio_rate phải > 0",
        "err_demod_multiple_audio": "demod_rate phải là bội của audio_rate",
        "err_ymax_gt_ymin": "Y max phải > Y min",
        "err_smooth_time_range": "Làm mượt theo thời gian phải trong [0..1]",
        "err_smooth_freq_range": "Làm mượt theo tần số phải trong [0..10]",
        "err_fps_range": "FPS phải trong [10..120]",
        "err_corr_points_range": "Điểm Corr phải trong [64..2048]",
        "err_corr_alpha_range": "Corr alpha phải trong [0.05..1]",
        "err_corr_size_range": "Kích thước Corr phải trong [1..8]",
        "recording_log": "Ghi âm: {file}",
        "recording_status": "Ghi âm: {file} ({size_mb:.2f} MB) | Đầu vào PCM: {mb_in:.2f} MB",
        "record_saved": "Đã lưu: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Đã lưu tệp: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Đã dừng ghi âm",
        "recording_file_prefix": "ghi_am",
        "cannot_start_recording": "Không thể bắt đầu ghi âm: {e}",
    },
    "zh": {
        "save": "保存",
        "dark_mode": "深色模式:",
        "fm_band": "FM 波段:",
        "unknown": "未知",
        "err_demod_audio_positive": "demod_rate/audio_rate 必须 > 0",
        "err_demod_multiple_audio": "demod_rate 必须是 audio_rate 的整数倍",
        "err_ymax_gt_ymin": "Y max 必须 > Y min",
        "err_smooth_time_range": "时间平滑必须在 [0..1]",
        "err_smooth_freq_range": "频率平滑必须在 [0..10]",
        "err_fps_range": "FPS 必须在 [10..120]",
        "err_corr_points_range": "Corr 点数必须在 [64..2048]",
        "err_corr_alpha_range": "Corr alpha 必须在 [0.05..1]",
        "err_corr_size_range": "Corr 大小必须在 [1..8]",
        "recording_log": "录音: {file}",
        "recording_status": "录音: {file} ({size_mb:.2f} MB) | PCM 输入: {mb_in:.2f} MB",
        "record_saved": "已保存: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "文件已保存: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "录音已停止",
        "recording_file_prefix": "录音",
        "cannot_start_recording": "无法开始录音: {e}",
    },
    "ja": {
        "save": "保存",
        "dark_mode": "ダークモード:",
        "fm_band": "FMバンド:",
        "unknown": "不明",
        "err_demod_audio_positive": "demod_rate/audio_rate は > 0 である必要があります",
        "err_demod_multiple_audio": "demod_rate は audio_rate の倍数である必要があります",
        "err_ymax_gt_ymin": "Y max は Y min より大きくする必要があります",
        "err_smooth_time_range": "時間平滑は [0..1] の範囲にしてください",
        "err_smooth_freq_range": "周波数平滑は [0..10] の範囲にしてください",
        "err_fps_range": "FPS は [10..120] の範囲にしてください",
        "err_corr_points_range": "Corr 点数は [64..2048] の範囲にしてください",
        "err_corr_alpha_range": "Corr alpha は [0.05..1] の範囲にしてください",
        "err_corr_size_range": "Corr サイズは [1..8] の範囲にしてください",
        "recording_log": "録音: {file}",
        "recording_status": "録音: {file} ({size_mb:.2f} MB) | PCM入力: {mb_in:.2f} MB",
        "record_saved": "保存しました: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "ファイルを保存しました: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "録音を停止しました",
        "recording_file_prefix": "録音",
        "cannot_start_recording": "録音を開始できません: {e}",
    },
    "ko": {
        "save": "저장",
        "dark_mode": "다크 모드:",
        "fm_band": "FM 밴드:",
        "unknown": "알 수 없음",
        "err_demod_audio_positive": "demod_rate/audio_rate 는 > 0 이어야 합니다",
        "err_demod_multiple_audio": "demod_rate 는 audio_rate 의 배수여야 합니다",
        "err_ymax_gt_ymin": "Y max 는 Y min 보다 커야 합니다",
        "err_smooth_time_range": "시간 스무딩은 [0..1] 범위여야 합니다",
        "err_smooth_freq_range": "주파수 스무딩은 [0..10] 범위여야 합니다",
        "err_fps_range": "FPS 는 [10..120] 범위여야 합니다",
        "err_corr_points_range": "Corr 포인트는 [64..2048] 범위여야 합니다",
        "err_corr_alpha_range": "Corr alpha 는 [0.05..1] 범위여야 합니다",
        "err_corr_size_range": "Corr 크기는 [1..8] 범위여야 합니다",
        "recording_log": "녹음: {file}",
        "recording_status": "녹음: {file} ({size_mb:.2f} MB) | PCM 입력: {mb_in:.2f} MB",
        "record_saved": "저장됨: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "파일 저장됨: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "녹음이 중지되었습니다",
        "recording_file_prefix": "녹음",
        "cannot_start_recording": "녹음을 시작할 수 없습니다: {e}",
    },
    "hi": {
        "save": "सहेजें",
        "dark_mode": "डार्क मोड:",
        "fm_band": "FM बैंड:",
        "unknown": "अज्ञात",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 होना चाहिए",
        "err_demod_multiple_audio": "demod_rate, audio_rate का गुणज होना चाहिए",
        "err_ymax_gt_ymin": "Y max, Y min से बड़ा होना चाहिए",
        "err_smooth_time_range": "टाइम स्मूदिंग [0..1] में होना चाहिए",
        "err_smooth_freq_range": "फ्रीक्वेंसी स्मूदिंग [0..10] में होना चाहिए",
        "err_fps_range": "FPS [10..120] में होना चाहिए",
        "err_corr_points_range": "Corr पॉइंट्स [64..2048] में होने चाहिए",
        "err_corr_alpha_range": "Corr alpha [0.05..1] में होना चाहिए",
        "err_corr_size_range": "Corr साइज [1..8] में होना चाहिए",
        "recording_log": "रिकॉर्डिंग: {file}",
        "recording_status": "रिकॉर्डिंग: {file} ({size_mb:.2f} MB) | PCM इनपुट: {mb_in:.2f} MB",
        "record_saved": "सहेजा गया: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "फ़ाइल सहेजी गई: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "रिकॉर्डिंग बंद",
        "recording_file_prefix": "रिकॉर्डिंग",
        "cannot_start_recording": "रिकॉर्डिंग शुरू नहीं हो सकी: {e}",
    },
    "ur": {
        "save": "محفوظ کریں",
        "dark_mode": "ڈارک موڈ:",
        "fm_band": "FM بینڈ:",
        "unknown": "نامعلوم",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 ہونا چاہیے",
        "err_demod_multiple_audio": "demod_rate، audio_rate کا مضاعف ہونا چاہیے",
        "err_ymax_gt_ymin": "Y max کو Y min سے بڑا ہونا چاہیے",
        "err_smooth_time_range": "وقت کی ہمواری [0..1] میں ہونی چاہیے",
        "err_smooth_freq_range": "فریکوئنسی ہمواری [0..10] میں ہونی چاہیے",
        "err_fps_range": "FPS [10..120] میں ہونا چاہیے",
        "err_corr_points_range": "Corr پوائنٹس [64..2048] میں ہونے چاہئیں",
        "err_corr_alpha_range": "Corr alpha [0.05..1] میں ہونا چاہیے",
        "err_corr_size_range": "Corr سائز [1..8] میں ہونا چاہیے",
        "recording_log": "ریکارڈنگ: {file}",
        "recording_status": "ریکارڈنگ: {file} ({size_mb:.2f} MB) | PCM اِن پٹ: {mb_in:.2f} MB",
        "record_saved": "محفوظ کیا گیا: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "فائل محفوظ کی گئی: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "ریکارڈنگ بند",
        "recording_file_prefix": "ریکارڈنگ",
        "cannot_start_recording": "ریکارڈنگ شروع نہیں ہو سکی: {e}",
    },
    "fa": {
        "save": "ذخیره",
        "dark_mode": "حالت تیره:",
        "fm_band": "باند FM:",
        "unknown": "نامشخص",
        "err_demod_audio_positive": "demod_rate/audio_rate باید > 0 باشد",
        "err_demod_multiple_audio": "demod_rate باید مضربی از audio_rate باشد",
        "err_ymax_gt_ymin": "Y max باید > Y min باشد",
        "err_smooth_time_range": "هموارسازی زمان باید در [0..1] باشد",
        "err_smooth_freq_range": "هموارسازی فرکانس باید در [0..10] باشد",
        "err_fps_range": "FPS باید در [10..120] باشد",
        "err_corr_points_range": "نقاط Corr باید در [64..2048] باشد",
        "err_corr_alpha_range": "Corr alpha باید در [0.05..1] باشد",
        "err_corr_size_range": "اندازه Corr باید در [1..8] باشد",
        "recording_log": "ضبط: {file}",
        "recording_status": "ضبط: {file} ({size_mb:.2f} MB) | ورودی PCM: {mb_in:.2f} MB",
        "record_saved": "ذخیره شد: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "فایل ذخیره شد: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "ضبط متوقف شد",
        "recording_file_prefix": "ضبط",
        "cannot_start_recording": "نمی‌توان ضبط را شروع کرد: {e}",
    },
    "sw": {
        "save": "Hifadhi",
        "dark_mode": "Hali ya giza:",
        "fm_band": "Bendi ya FM:",
        "unknown": "Haijulikani",
        "err_demod_audio_positive": "demod_rate/audio_rate lazima iwe > 0",
        "err_demod_multiple_audio": "demod_rate lazima iwe kizidisho cha audio_rate",
        "err_ymax_gt_ymin": "Y max lazima iwe > Y min",
        "err_smooth_time_range": "Kulainisha muda lazima kuwe kwenye [0..1]",
        "err_smooth_freq_range": "Kulainisha masafa lazima kuwe kwenye [0..10]",
        "err_fps_range": "FPS lazima iwe kwenye [10..120]",
        "err_corr_points_range": "Pointi za Corr lazima ziwe kwenye [64..2048]",
        "err_corr_alpha_range": "Corr alpha lazima iwe kwenye [0.05..1]",
        "err_corr_size_range": "Ukubwa wa Corr lazima uwe kwenye [1..8]",
        "recording_log": "Rekodi: {file}",
        "recording_status": "Rekodi: {file} ({size_mb:.2f} MB) | Ingizo la PCM: {mb_in:.2f} MB",
        "record_saved": "Imehifadhiwa: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "Faili imehifadhiwa: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "Rekodi imesitishwa",
        "recording_file_prefix": "rekodi",
        "cannot_start_recording": "Haiwezi kuanza kurekodi: {e}",
    },
    "mr": {
        "save": "जतन करा",
        "dark_mode": "डार्क मोड:",
        "fm_band": "FM बँड:",
        "unknown": "अज्ञात",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 असणे आवश्यक आहे",
        "err_demod_multiple_audio": "demod_rate हे audio_rate चे गुणक असणे आवश्यक आहे",
        "err_ymax_gt_ymin": "Y max हे Y min पेक्षा मोठे असणे आवश्यक आहे",
        "err_smooth_time_range": "टाइम स्मूदिंग [0..1] मध्ये असणे आवश्यक आहे",
        "err_smooth_freq_range": "फ्रिक्वेन्सी स्मूदिंग [0..10] मध्ये असणे आवश्यक आहे",
        "err_fps_range": "FPS [10..120] मध्ये असणे आवश्यक आहे",
        "err_corr_points_range": "Corr पॉइंट्स [64..2048] मध्ये असणे आवश्यक आहे",
        "err_corr_alpha_range": "Corr alpha [0.05..1] मध्ये असणे आवश्यक आहे",
        "err_corr_size_range": "Corr साइज [1..8] मध्ये असणे आवश्यक आहे",
        "recording_log": "रेकॉर्डिंग: {file}",
        "recording_status": "रेकॉर्डिंग: {file} ({size_mb:.2f} MB) | PCM इनपुट: {mb_in:.2f} MB",
        "record_saved": "जतन केले: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "फाइल जतन केली: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "रेकॉर्डिंग थांबली",
        "recording_file_prefix": "रेकॉर्डिंग",
        "cannot_start_recording": "रेकॉर्डिंग सुरू करता आली नाही: {e}",
    },
    "te": {
        "save": "సేవ్",
        "dark_mode": "డార్క్ మోడ్:",
        "fm_band": "FM బ్యాండ్:",
        "unknown": "తెలియదు",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 ఉండాలి",
        "err_demod_multiple_audio": "demod_rate, audio_rate యొక్క గుణితం అయి ఉండాలి",
        "err_ymax_gt_ymin": "Y max, Y min కంటే పెద్దగా ఉండాలి",
        "err_smooth_time_range": "టైమ్ స్మూతింగ్ [0..1] లో ఉండాలి",
        "err_smooth_freq_range": "ఫ్రీక్వెన్సీ స్మూతింగ్ [0..10] లో ఉండాలి",
        "err_fps_range": "FPS [10..120] లో ఉండాలి",
        "err_corr_points_range": "Corr పాయింట్లు [64..2048] లో ఉండాలి",
        "err_corr_alpha_range": "Corr alpha [0.05..1] లో ఉండాలి",
        "err_corr_size_range": "Corr సైజు [1..8] లో ఉండాలి",
        "recording_log": "రికార్డింగ్: {file}",
        "recording_status": "రికార్డింగ్: {file} ({size_mb:.2f} MB) | PCM ఇన్‌పుట్: {mb_in:.2f} MB",
        "record_saved": "సేవ్ అయ్యింది: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "ఫైల్ సేవ్ అయ్యింది: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "రికార్డింగ్ ఆగింది",
        "recording_file_prefix": "రికార్డింగ్",
        "cannot_start_recording": "రికార్డింగ్ ప్రారంభించలేము: {e}",
    },
    "ta": {
        "save": "சேமி",
        "dark_mode": "இருண்ட முறை:",
        "fm_band": "FM அலைவரம்பு:",
        "unknown": "அறியப்படாதது",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 ஆக இருக்க வேண்டும்",
        "err_demod_multiple_audio": "demod_rate, audio_rate இன் பலமாக இருக்க வேண்டும்",
        "err_ymax_gt_ymin": "Y max, Y min ஐ விட பெரியதாக இருக்க வேண்டும்",
        "err_smooth_time_range": "நேர ஸ்மூத்திங் [0..1] இல் இருக்க வேண்டும்",
        "err_smooth_freq_range": "அதிர்வெண் ஸ்மூத்திங் [0..10] இல் இருக்க வேண்டும்",
        "err_fps_range": "FPS [10..120] இல் இருக்க வேண்டும்",
        "err_corr_points_range": "Corr புள்ளிகள் [64..2048] இல் இருக்க வேண்டும்",
        "err_corr_alpha_range": "Corr alpha [0.05..1] இல் இருக்க வேண்டும்",
        "err_corr_size_range": "Corr அளவு [1..8] இல் இருக்க வேண்டும்",
        "recording_log": "பதிவு: {file}",
        "recording_status": "பதிவு: {file} ({size_mb:.2f} MB) | PCM உள்ளீடு: {mb_in:.2f} MB",
        "record_saved": "சேமிக்கப்பட்டது: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "கோப்பு சேமிக்கப்பட்டது: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "பதிவு நிறுத்தப்பட்டது",
        "recording_file_prefix": "பதிவு",
        "cannot_start_recording": "பதிவை தொடங்க முடியவில்லை: {e}",
    },
    "th": {
        "save": "บันทึก",
        "dark_mode": "โหมดมืด:",
        "fm_band": "ย่าน FM:",
        "unknown": "ไม่ทราบ",
        "err_demod_audio_positive": "demod_rate/audio_rate ต้อง > 0",
        "err_demod_multiple_audio": "demod_rate ต้องเป็นพหุคูณของ audio_rate",
        "err_ymax_gt_ymin": "Y max ต้อง > Y min",
        "err_smooth_time_range": "การทำให้เรียบตามเวลา ต้องอยู่ใน [0..1]",
        "err_smooth_freq_range": "การทำให้เรียบตามความถี่ ต้องอยู่ใน [0..10]",
        "err_fps_range": "FPS ต้องอยู่ใน [10..120]",
        "err_corr_points_range": "จุด Corr ต้องอยู่ใน [64..2048]",
        "err_corr_alpha_range": "Corr alpha ต้องอยู่ใน [0.05..1]",
        "err_corr_size_range": "ขนาด Corr ต้องอยู่ใน [1..8]",
        "recording_log": "บันทึก: {file}",
        "recording_status": "บันทึก: {file} ({size_mb:.2f} MB) | อินพุต PCM: {mb_in:.2f} MB",
        "record_saved": "บันทึกแล้ว: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "บันทึกไฟล์แล้ว: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "หยุดบันทึกแล้ว",
        "recording_file_prefix": "บันทึก",
        "cannot_start_recording": "ไม่สามารถเริ่มบันทึกได้: {e}",
    },
    "gu": {
        "save": "સાચવો",
        "dark_mode": "ડાર્ક મોડ:",
        "fm_band": "FM બેન્ડ:",
        "unknown": "અજ્ઞાત",
        "err_demod_audio_positive": "demod_rate/audio_rate > 0 હોવું જોઈએ",
        "err_demod_multiple_audio": "demod_rate એ audio_rate નું ગુણાકાર હોવું જોઈએ",
        "err_ymax_gt_ymin": "Y max એ Y min કરતા મોટું હોવું જોઈએ",
        "err_smooth_time_range": "સમય સ્મૂથિંગ [0..1] માં હોવું જોઈએ",
        "err_smooth_freq_range": "ફ્રીક્વન્સી સ્મૂથિંગ [0..10] માં હોવું જોઈએ",
        "err_fps_range": "FPS [10..120] માં હોવું જોઈએ",
        "err_corr_points_range": "Corr પોઈન્ટ્સ [64..2048] માં હોવા જોઈએ",
        "err_corr_alpha_range": "Corr alpha [0.05..1] માં હોવું જોઈએ",
        "err_corr_size_range": "Corr સાઇઝ [1..8] માં હોવું જોઈએ",
        "recording_log": "રેકોર્ડિંગ: {file}",
        "recording_status": "રેકોર્ડિંગ: {file} ({size_mb:.2f} MB) | PCM ઇનપુટ: {mb_in:.2f} MB",
        "record_saved": "સાચવ્યું: {file} ({size_mb:.2f} MB)",
        "record_file_saved": "ફાઇલ સાચવ્યું: {file} ({size_mb:.2f} MB)",
        "recording_stopped": "રેકોર્ડિંગ બંધ",
        "recording_file_prefix": "રેકોર્ડિંગ",
        "cannot_start_recording": "રેકોર્ડિંગ શરૂ કરી શકાતું નથી: {e}",
    },
}

try:
    for _lang, _patch in (I18N_EXTRA or {}).items():
        if _lang in I18N and isinstance(I18N[_lang], dict):
            for _k, _v in _patch.items():
                I18N[_lang].setdefault(_k, _v)
except Exception:
    pass


class FMStation:
    def __init__(self, freq):
        self.freq = freq
        self.ps = None
        self.radiotext = None
        # Optional: RadioText Plus (RT+) if the decoder provides it.
        # This may contain structured "Now Playing" fields (artist/title).
        self.rtplus = None
        self.pi = None
        self.prog_type = None
        self.alt_freqs = []
        self.stereo = False
        self.tp = False
        self.ta = False
        self.last_seen = None
        self.rds_count = 0
        
    def update_from_rds(self, rds_data):
        """Aktualizuj dane stacji z RDS JSON"""
        self.rds_count += 1
        self.last_seen = datetime.now().isoformat()
        
        if 'ps' in rds_data:
            self.ps = rds_data['ps']
        if 'radiotext' in rds_data:
            self.radiotext = rds_data['radiotext']

        # Some decoders expose RT+ under different keys; keep the first non-empty one.
        for key in ('rtplus', 'radio_text_plus', 'radiotext_plus', 'radiotextplus', 'rt_plus'):
            if key in rds_data and rds_data.get(key):
                self.rtplus = rds_data.get(key)
                break
        if 'pi' in rds_data:
            self.pi = rds_data['pi']
        if 'prog_type' in rds_data:
            self.prog_type = rds_data['prog_type']
        if 'alt_frequencies_a' in rds_data:
            self.alt_freqs = rds_data['alt_frequencies_a']
        if 'di' in rds_data and 'stereo' in rds_data['di']:
            self.stereo = rds_data['di']['stereo']
        if 'tp' in rds_data:
            self.tp = rds_data['tp']
        if 'ta' in rds_data:
            self.ta = rds_data['ta']
            
    def to_dict(self):
        return {
            'freq': self.freq,
            'ps': self.ps,
            'radiotext': self.radiotext,
            'rtplus': self.rtplus,
            'pi': self.pi,
            'prog_type': self.prog_type,
            'alt_freqs': self.alt_freqs,
            'stereo': self.stereo,
            'tp': self.tp,
            'ta': self.ta,
            'last_seen': self.last_seen,
            'rds_count': self.rds_count
        }
        
    @staticmethod
    def from_dict(data):
        station = FMStation(data['freq'])
        station.ps = data.get('ps')
        station.radiotext = data.get('radiotext')
        station.rtplus = data.get('rtplus')
        station.pi = data.get('pi')
        station.prog_type = data.get('prog_type')
        station.alt_freqs = data.get('alt_freqs', [])
        station.stereo = data.get('stereo', False)
        station.tp = data.get('tp', False)
        station.ta = data.get('ta', False)
        station.last_seen = data.get('last_seen')
        station.rds_count = data.get('rds_count', 0)
        return station

    def get_now_playing(self):
        """Try to extract “Now Playing” from RT+ (if available)."""
        if not isinstance(self.rtplus, dict):
            # Fallback: try to parse common RadioText formats.
            # Example (RMF FM): "Teraz gramy: Artist - Title"
            rt = (self.radiotext or "").strip()
            if not rt:
                return None
            # Strip common prefixes.
            for pref in (
                "Teraz gramy:",
                "Now playing:",
                "Now Playing:",
                "Aktuell:",
                "En ce moment:",
            ):
                if rt.lower().startswith(pref.lower()):
                    rt = rt[len(pref):].strip()
                    break
            # If it looks like "Artist - Title", show it as now-playing.
            if " - " in rt:
                return rt
            return None

        # Common-ish field names (varies by decoder/station)
        title = (
            self.rtplus.get('item_title')
            or self.rtplus.get('title')
            or self.rtplus.get('song')
            or self.rtplus.get('track')
        )
        artist = (
            self.rtplus.get('item_artist')
            or self.rtplus.get('artist')
            or self.rtplus.get('performer')
        )

        if title and artist:
            return f"{artist} — {title}"
        return title or None
        
    def __str__(self):
        name = self.ps or "Unknown"
        freq_str = f"{self.freq:.1f} MHz"
        stereo_str = " [STEREO]" if self.stereo else ""
        return f"{freq_str}: {name}{stereo_str}"
    
    def get_display_name(self):
        """Display name used in the GUI."""
        name = self.ps or "Unknown"
        return f"{self.freq:.1f} MHz - {name}"


class FMDatabase:
    def __init__(self, filename):
        self.filename = filename
        self.stations = {}
        self.load()
        
    def load(self):
        """Load the station database from a JSON file."""
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for freq_str, station_data in data.items():
                        freq = float(freq_str)
                        self.stations[freq] = FMStation.from_dict(station_data)
            except Exception as e:
                print(f"Database load error: {e}")
                self.stations = {}
            
    def save(self):
        """Save the station database to a JSON file."""
        try:
            data = {str(freq): station.to_dict() 
                   for freq, station in self.stations.items()}
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Database save error: {e}")
            
    def add_or_update(self, station):
        """Add a station or update an existing one."""
        self.stations[station.freq] = station
        
    def get_stations_with_rds(self):
        """Return stations that have valid RDS (PS present)."""
        return sorted([s for s in self.stations.values() if s.ps is not None],
                     key=lambda s: s.freq)


class FMRadioGUI:
    def __init__(self, root):
        self.root = root

        # UI language (default: Polish). Will be overridden by settings.
        self.lang = "pl"
        self.ui_theme = "light"

        self.root.title("RTL-SDR FM Radio with RDS")
        self.root.geometry("1200x800")
        self.root.minsize(1100, 750)

        # Prefer a more modern ttk theme when available
        try:
            ttk.Style().theme_use('clam')
        except Exception:
            pass
        
        # Baza danych stacji
        self.db = FMDatabase(DB_FILE)

        # Mapping: GUI items -> station objects (Treeview)
        self._station_by_iid = {}
        
        # Playback processes
        self.rtl_proc = None
        self.play_proc = None
        self.playing = False
        self.scanning = False
        self.current_station = None
        self.tuned_freq_mhz = None

        # GNU Radio stereo RX
        self.gr_tb = None
        self.gr_src = None
        self._gr_blocks = None
        self._gr_stop_event = None
        self._gr_pipe_r = None
        self._gr_pipe_w = None
        self._gr_pipe_file = None
        self.audio_channels = 2  # after moving to GNU Radio, always stereo
        self.demod_rate = 240000  # Hz
        self.audio_rate = 48000   # Hz

        # Closing state (to avoid blocking the GUI)
        self._closing = False

        # Settings (runtime + persisted)
        self.osmosdr_args = "numchan=1 rtl=0"
        self.ppm = 0
        self.rf_bandwidth_hz = 200000
        self.enable_deemphasis = ENABLE_DEEMPHASIS
        self.enable_rds_updates = True
        self.rds_interval_s = 30
        self.spectrum_max_hz = SPECTRUM_MAX_HZ
        self.spectrum_ymin_dbfs = -90.0
        self.spectrum_ymax_dbfs = 0.0

        # FM band (scan + manual tuning validation)
        self.fm_band_preset = DEFAULT_FM_BAND_PRESET
        self.fm_min_khz = int(round(FM_START * 1000.0))
        self.fm_max_khz = int(round(FM_END * 1000.0))
        self.fm_step_khz = int(round(FM_STEP * 1000.0))
        self.fm_min_mhz = float(self.fm_min_khz) / 1000.0
        self.fm_max_mhz = float(self.fm_max_khz) / 1000.0
        self.fm_step_mhz = float(self.fm_step_khz) / 1000.0

        # Visualization settings (spectrum / correlation)
        self.spec_time_alpha = 0.25
        self.spec_freq_smooth_bins = 1
        self.spec_fps = 66
        self.corr_points = 256
        self.corr_point_alpha = 0.5
        self.corr_marker_size = 2
        
        # Volume (0-100)
        self.volume = 50

        # Debounced settings save (to avoid writing JSON on every slider tick)
        self._settings_save_timer = None
        
        # RTL-SDR gain (0-49.6 dB)
        self.gain = 42.1
        self.gain_change_timer = None  # timer used for gain debouncing
        
        # Recording
        self.recording = False
        self.record_proc = None
        self.record_filename = None
        self.record_size_updater = None  # timer for updating displayed file size
        self.record_bytes_written = 0
        self.record_started_at = None

        # Recordings directory (settings)
        self.recordings_dir = os.path.join((BASE_DIR if DEV_MODE else APP_DATA_DIR), "recordings")

        # Logging (thread-safe)
        self._log_queue = queue.SimpleQueue()
        self._log_flush_scheduled = False
        
        # RDS updates
        self.rds_updating = False

        # RDS backend: "rtl_fm" (external) or "gnuradio" (single-dongle, in-flowgraph MPX).
        self.rds_backend = "rtl_fm"

        # GNU Radio → redsea RDS pipeline (optional)
        self._rds_proc = None
        self._rds_audio_pipe_r = None
        self._rds_audio_pipe_w = None
        self._rds_audio_pipe_file = None
        self._rds_feeder_thread = None
        self._rds_reader_thread = None
        self._rds_last_save_ts = 0.0
        
        # Spectrum
        self.spectrum_running = False
        self.spectrum_data = np.zeros(512)
        self.audio_buffer = []  # audio buffer for spectrum
        self.audio_lock = Lock()  # thread-safe access
        self.spectrum_smooth = np.full(512, -70.0, dtype=np.float32)  # buffer for smoothing
        self.spectrum_floor_db = -80.0

        # Plot redraw coalescing (Matplotlib/TkAgg can't reliably sustain high FPS; avoid event backlog)
        self._spec_plot_latest = None
        self._spec_plot_pending = False
        self._spec_plot_last_draw_ts = 0.0

        # Stereo correlation / L-R balance (second plot)
        self._corr_points = 256

        # Load and apply settings BEFORE building the GUI (so plot axis limits are correct)
        self.settings = self._load_settings()
        self._apply_settings_to_runtime(initial=True)

        # Apply UI theme early (affects ttk styling). Widget-specific colors will be applied after create_widgets.
        try:
            self._apply_theme_to_ui()
        except Exception:
            pass

        # Matplotlib uses its own font selection (separate from Tk), so CJK glyphs may
        # be missing on charts unless we pick a CJK-capable font.
        self._configure_matplotlib_fonts()
        
        self.create_widgets()
        try:
            self._apply_theme_to_ui()
        except Exception:
            pass
        self._apply_language_to_ui()
        self.update_station_list()

    def _theme_palette(self):
        theme = str(getattr(self, "ui_theme", "light") or "light").lower()
        if theme == "dark":
            return {
                "bg": "#1e1e1e",
                "panel": "#252526",
                "fg": "#e6e6e6",
                "muted": "#b3b3b3",
                "accent": "#3a86ff",
                "border": "#3c3c3c",
                "input_bg": "#2d2d2d",
                "select_bg": "#094771",
                "select_fg": "#ffffff",
                "plot_grid": "#3c3c3c",
            }
        return {
            "bg": "#f2f2f2",
            "panel": "#ffffff",
            "fg": "#111111",
            "muted": "#333333",
            "accent": "#0b57d0",
            "border": "#c9c9c9",
            "input_bg": "#ffffff",
            "select_bg": "#cfe2ff",
            "select_fg": "#111111",
            "plot_grid": "#d0d0d0",
        }

    def _apply_theme_to_ui(self):
        """Apply light/dark theme to ttk widgets, tk widgets, and Matplotlib charts."""
        pal = self._theme_palette()
        is_dark = (str(getattr(self, "ui_theme", "light")) == "dark")

        # Root window background (tk)
        try:
            self.root.configure(bg=pal["bg"])
        except Exception:
            pass

        # ttk styling (global)
        try:
            style = ttk.Style()
            # Ensure we're on a theme that honors many style options.
            try:
                style.theme_use("clam")
            except Exception:
                pass

            style.configure(".",
                            background=pal["bg"],
                            foreground=pal["fg"],
                            fieldbackground=pal["input_bg"],
                            bordercolor=pal["border"],
                            lightcolor=pal["border"],
                            darkcolor=pal["border"],
                            troughcolor=pal["panel"])

            style.configure("TFrame", background=pal["bg"])
            style.configure("TLabelframe", background=pal["bg"], foreground=pal["fg"], bordercolor=pal["border"])
            style.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["fg"])
            style.configure("TLabel", background=pal["bg"], foreground=pal["fg"])

            style.configure("TButton", background=pal["panel"], foreground=pal["fg"], bordercolor=pal["border"])
            style.map("TButton",
                      background=[("active", pal["input_bg"])],
                      foreground=[("disabled", pal["muted"])])

            style.configure("TEntry", fieldbackground=pal["input_bg"], foreground=pal["fg"], background=pal["bg"])
            style.configure("TCombobox", fieldbackground=pal["input_bg"], foreground=pal["fg"], background=pal["bg"])
            style.map("TCombobox",
                      fieldbackground=[("readonly", pal["input_bg"])],
                      foreground=[("readonly", pal["fg"])])

            style.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"])

            style.configure("Treeview",
                            background=pal["panel"],
                            fieldbackground=pal["panel"],
                            foreground=pal["fg"],
                            bordercolor=pal["border"],
                            lightcolor=pal["border"],
                            darkcolor=pal["border"])
            style.map("Treeview",
                      background=[("selected", pal["select_bg"])],
                      foreground=[("selected", pal["select_fg"])])

            style.configure("Treeview.Heading", background=pal["bg"], foreground=pal["fg"], relief="flat")
        except Exception:
            pass

        # Settings window background (tk Toplevel)
        try:
            if hasattr(self, "_settings_win") and self._settings_win is not None:
                try:
                    self._settings_win.configure(bg=pal["bg"])
                except Exception:
                    pass
        except Exception:
            pass

        # tk widgets that don't use ttk styling
        try:
            if hasattr(self, "record_status_label") and self.record_status_label is not None:
                # Keep red text, but set background to match theme.
                self.record_status_label.configure(bg=pal["bg"])
        except Exception:
            pass

        try:
            if hasattr(self, "log_text") and self.log_text is not None:
                # scrolledtext.ScrolledText is a tk.Text
                self.log_text.configure(
                    bg=pal["panel"] if is_dark else "#ffffff",
                    fg=pal["fg"],
                    insertbackground=pal["fg"],
                    selectbackground=pal["select_bg"],
                    selectforeground=pal["select_fg"],
                )
        except Exception:
            pass

        # Matplotlib theme (figure + axes)
        try:
            import matplotlib as mpl

            mpl.rcParams["figure.facecolor"] = pal["bg"]
            mpl.rcParams["axes.facecolor"] = pal["panel"]
            mpl.rcParams["savefig.facecolor"] = pal["bg"]
            mpl.rcParams["text.color"] = pal["fg"]
            mpl.rcParams["axes.labelcolor"] = pal["fg"]
            mpl.rcParams["xtick.color"] = pal["muted"]
            mpl.rcParams["ytick.color"] = pal["muted"]
            mpl.rcParams["axes.edgecolor"] = pal["border"]
            mpl.rcParams["grid.color"] = pal["plot_grid"]

            if hasattr(self, "fig") and self.fig is not None:
                try:
                    self.fig.patch.set_facecolor(pal["bg"])
                except Exception:
                    pass
            for ax_name in ("ax_spec", "ax_corr"):
                ax = getattr(self, ax_name, None)
                if ax is None:
                    continue
                try:
                    ax.set_facecolor(pal["panel"])
                except Exception:
                    pass
                try:
                    ax.title.set_color(pal["fg"])
                    ax.xaxis.label.set_color(pal["fg"])
                    ax.yaxis.label.set_color(pal["fg"])
                except Exception:
                    pass
                try:
                    ax.tick_params(colors=pal["muted"])
                except Exception:
                    pass
                try:
                    for spine in ax.spines.values():
                        spine.set_color(pal["border"])
                except Exception:
                    pass
                try:
                    ax.grid(True, alpha=0.35)
                except Exception:
                    pass

            # Correlation plot artists (improve readability in dark mode)
            try:
                if hasattr(self, "line_corr") and self.line_corr is not None:
                    self.line_corr.set_color(pal["fg"])
                    try:
                        self.line_corr.set_markerfacecolor(pal["fg"])
                        self.line_corr.set_markeredgecolor(pal["fg"])
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                if hasattr(self, "_corr_diag") and self._corr_diag is not None:
                    self._corr_diag.set_color(pal["plot_grid"] if is_dark else pal["plot_grid"])
            except Exception:
                pass
            try:
                if hasattr(self, "_corr_zero_h") and self._corr_zero_h is not None:
                    self._corr_zero_h.set_color(pal["plot_grid"])
                if hasattr(self, "_corr_zero_v") and self._corr_zero_v is not None:
                    self._corr_zero_v.set_color(pal["plot_grid"])
            except Exception:
                pass
            try:
                if hasattr(self, "corr_text") and self.corr_text is not None:
                    self.corr_text.set_color(pal["fg"])
            except Exception:
                pass

            # Legend text colors on spectrum plot
            try:
                if hasattr(self, "ax_spec") and self.ax_spec is not None:
                    leg = self.ax_spec.get_legend()
                    if leg is not None:
                        for txt in leg.get_texts():
                            try:
                                txt.set_color(pal["fg"])
                            except Exception:
                                pass
            except Exception:
                pass

            if hasattr(self, "canvas") and self.canvas is not None:
                try:
                    self.canvas.draw_idle()
                except Exception:
                    pass
        except Exception:
            pass

    def t(self, key, **kwargs):
        """Translate UI string by key with fallback to English."""
        try:
            table = I18N.get(getattr(self, "lang", "en")) or I18N.get("en", {})
            text = table.get(key)
            if text is None:
                text = (I18N.get("en", {}) or {}).get(key, key)
            # Backward-compatible dynamic range rendering:
            # many translations historically hard-coded "88-108". If callers provide
            # min/max, patch the range in-place even if the translation string has
            # no placeholders.
            if key in ("freq_out_of_range", "log_scan_start") and ("min" in kwargs) and ("max" in kwargs):
                try:
                    min_v = float(kwargs.get("min"))
                    max_v = float(kwargs.get("max"))
                    dyn = f"{min_v:.1f}-{max_v:.1f}"
                    s = str(text)
                    s = s.replace("88-108", dyn)
                    s = s.replace("88–108", dyn)
                    text = s
                except Exception:
                    pass

            if kwargs:
                return str(text).format(**kwargs)
            return str(text)
        except Exception:
            return str(key)

    def _language_display_list(self):
        """List of language display strings for the settings combobox."""
        items = []
        for code, pl_name, native_name in TOP25_UI_LANGUAGES:
            items.append(f"{pl_name} — {native_name} ({code})")
        return items

    def _language_code_from_display(self, display):
        for code, pl_name, native_name in TOP25_UI_LANGUAGES:
            if display == f"{pl_name} — {native_name} ({code})":
                return code
        return None

    def _configure_matplotlib_fonts(self):
        """Configure Matplotlib font so CJK glyphs render on charts (legend/titles)."""
        try:
            import matplotlib as mpl
            from matplotlib import font_manager as fm
            import warnings

            lang = str(getattr(self, "lang", "en") or "en").lower()

            def is_cjk_language(code: str) -> bool:
                code = (code or "").lower()
                return code.startswith("zh") or code.startswith("ja") or code.startswith("ko")

            def lang_group(code: str) -> str:
                code = (code or "").lower()
                if code.startswith("zh") or code.startswith("ja") or code.startswith("ko"):
                    return "cjk"
                if code in ("hi", "mr"):
                    return "devanagari"
                if code == "bn":
                    return "bengali"
                if code == "te":
                    return "telugu"
                if code == "ta":
                    return "tamil"
                if code == "th":
                    return "thai"
                if code == "gu":
                    return "gujarati"
                if code in ("ar", "ur", "fa"):
                    return "arabic"
                return "latin"

            def get_font_names() -> set:
                try:
                    return {f.name for f in fm.fontManager.ttflist}
                except Exception:
                    return set()

            # Cache available font names (building this repeatedly is slow)
            if not hasattr(self, "_mpl_font_names_cache") or not isinstance(self._mpl_font_names_cache, set):
                self._mpl_font_names_cache = get_font_names()

            chosen = None
            group = lang_group(lang)

            if group == "cjk":
                # A lot of distros package Source Han / Noto CJK under JP/KR family names.
                # Even if the family says "JP", it typically includes CJK glyph coverage.
                if lang.startswith("zh"):
                    candidates = [
                        "Noto Sans CJK SC",
                        "Noto Sans CJK TC",
                        "Noto Sans CJK JP",
                        "Noto Serif CJK JP",
                        "Noto Sans SC",
                        "Noto Sans TC",
                        "WenQuanYi Zen Hei",
                        "Droid Sans Fallback",
                        "AR PL UMing CN",
                        "SimHei",
                    ]
                elif lang.startswith("ja"):
                    candidates = [
                        "Noto Sans CJK JP",
                        "Droid Sans Fallback",
                        "IPAPGothic",
                        "TakaoPGothic",
                        "VL PGothic",
                    ]
                else:  # ko
                    candidates = [
                        "Noto Sans CJK KR",
                        "Noto Sans CJK JP",
                        "Droid Sans Fallback",
                        "NanumGothic",
                        "UnDotum",
                    ]

                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

                if chosen is None:
                    # Refresh cache once (covers cases where fonts were installed while the app is running)
                    refreshed = get_font_names()
                    if refreshed and refreshed != self._mpl_font_names_cache:
                        self._mpl_font_names_cache = refreshed
                        for name in candidates:
                            if name in self._mpl_font_names_cache:
                                chosen = name
                                break

                if chosen is None:
                    # Last-resort heuristic: pick any family containing "CJK" or "Droid Sans Fallback".
                    try:
                        for name in sorted(self._mpl_font_names_cache):
                            if "droid sans fallback" in name.lower() or "cjk" in name.lower() or "source han" in name.lower():
                                chosen = name
                                break
                    except Exception:
                        pass

                if chosen is None:
                    debug_log(
                        "WARN: No CJK Matplotlib font found. Install 'fonts-noto-cjk' (or 'fonts-wqy-zenhei') and restart."
                    )

            elif group == "devanagari":
                candidates = [
                    "Lohit Devanagari",
                    "Noto Sans Devanagari",
                    "Noto Sans Devanagari UI",
                    "Noto Serif Devanagari",
                    "Noto Serif Devanagari UI",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "bengali":
                candidates = [
                    "Likhan",
                    "Noto Sans Bengali",
                    "Noto Sans Bengali UI",
                    "Noto Serif Bengali",
                    "Noto Serif Bengali UI",
                    "Lohit Bengali",
                    "Mukti",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "telugu":
                candidates = [
                    "Lohit Telugu",
                    "Noto Sans Telugu",
                    "Noto Sans Telugu UI",
                    "Noto Serif Telugu",
                    "Noto Serif Telugu UI",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "tamil":
                candidates = [
                    "Meera Inimai",
                    "Noto Sans Tamil",
                    "Noto Sans Tamil Supplement",
                    "Noto Sans Tamil UI",
                    "Noto Serif Tamil",
                    "Noto Serif Tamil UI",
                    "Lohit Tamil",
                    "Samyak Tamil",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "thai":
                candidates = [
                    "Tlwg Typo",
                    "Tlwg Typist",
                    "Tlwg Mono",
                    "Tlwg Typewriter",
                    "Noto Sans Thai",
                    "Noto Sans Thai UI",
                    "Noto Serif Thai",
                    "Noto Serif Thai UI",
                    "Garuda",
                    "Loma",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "gujarati":
                candidates = [
                    "Rasa",
                    "Kalapi",
                    "Lohit Gujarati",
                    "Noto Sans Gujarati",
                    "Noto Sans Gujarati UI",
                    "Noto Serif Gujarati",
                    "Noto Serif Gujarati UI",
                    "Samyak Gujarati",
                    "DejaVu Sans",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            elif group == "arabic":
                candidates = [
                    "DejaVu Sans",
                    "Noto Naskh Arabic",
                    "Noto Sans Arabic",
                    "Noto Kufi Arabic",
                    "Amiri",
                    "Scheherazade",
                    "Arial",
                ]
                for name in candidates:
                    if name in self._mpl_font_names_cache:
                        chosen = name
                        break

            # Apply globally (affects new artists). We rely on Matplotlib's font fallback
            # across the provided sans-serif list (important for mixed Latin + non-Latin UI).
            mpl.rcParams["font.family"] = "sans-serif"
            base_sans = ["DejaVu Sans", "Liberation Sans", "Arial"]
            self._mpl_font_family = ([chosen] if chosen else []) + base_sans
            mpl.rcParams["font.sans-serif"] = self._mpl_font_family
            mpl.rcParams["axes.unicode_minus"] = False

            # Matplotlib can emit noisy warnings for some complex scripts even when a font exists.
            # We still prefer to render with a proper font, but silence the non-actionable warning.
            try:
                warnings.filterwarnings(
                    "ignore",
                    message=r"Matplotlib currently does not support .* natively\.",
                    category=UserWarning,
                )
            except Exception:
                pass

            self._mpl_font_name = chosen
        except Exception:
            self._mpl_font_name = None
            self._mpl_font_family = ["DejaVu Sans", "Liberation Sans", "Arial"]

    def _apply_language_to_ui(self):
        """Apply current language to existing widgets/titles."""
        # Ensure Matplotlib has a font capable of rendering the selected language.
        try:
            self._configure_matplotlib_fonts()
        except Exception:
            pass

        try:
            self.root.title(self.t("app_title"))
        except Exception:
            pass

        # Title/status
        try:
            if hasattr(self, "title_label"):
                self.title_label.config(text=self.t("title"))
        except Exception:
            pass

        try:
            if hasattr(self, "status_label"):
                if getattr(self, "scanning", False):
                    self.status_label.config(text=self.t("scanning"))
                elif getattr(self, "playing", False) and getattr(self, "current_station", None) is not None:
                    name = getattr(self.current_station, "ps", None) or getattr(self.current_station, "freq", "")
                    self.status_label.config(text=self.t("playing", name=name))
                else:
                    self.status_label.config(text=self.t("status_ready"))
        except Exception:
            pass

        # Frames / labels
        for attr, key in (
            ("tune_frame", "manual_tuning"),
            ("list_frame", "stations"),
            ("info_frame", "station_info"),
            ("settings_frame", "sdr_audio_panel"),
            ("log_frame", "log"),
            ("spectrum_frame", "viz"),
        ):
            try:
                w = getattr(self, attr, None)
                if w is not None:
                    w.config(text=self.t(key))
            except Exception:
                pass

        for attr, key in (
            ("freq_label", "frequency_mhz"),
            ("gain_text_label", "gain"),
            ("volume_text_label", "volume"),
        ):
            try:
                w = getattr(self, attr, None)
                if w is not None:
                    w.config(text=self.t(key))
            except Exception:
                pass

        # Buttons
        for attr, key in (
            ("tune_button", "tune"),
            ("save_button", "save"),
            ("scan_button", "scan_band"),
            ("play_button", "play"),
            ("stop_button", "stop"),
            ("record_start_button", "record_start"),
            ("record_stop_button", "record_stop"),
            ("settings_button", "settings"),
        ):
            try:
                w = getattr(self, attr, None)
                if w is not None:
                    w.config(text=self.t(key))
            except Exception:
                pass

        # Tree headings
        try:
            if hasattr(self, "station_tree"):
                self.station_tree.heading("freq", text=self.t("stations_col_freq"))
                self.station_tree.heading("ps", text=self.t("stations_col_name"))
        except Exception:
            pass

        # Matplotlib titles/labels
        try:
            if hasattr(self, "ax_spec"):
                self.ax_spec.set_title(self.t("spec_title"), fontsize=10)
                self.ax_spec.set_ylabel(self.t("spec_ylabel"), fontsize=8)
                if hasattr(self, "line_left") and self.line_left is not None:
                    self.line_left.set_label(self.t("left"))
                if hasattr(self, "line_right") and self.line_right is not None:
                    self.line_right.set_label(self.t("right"))
                try:
                    self.ax_spec.legend(
                        loc='upper right',
                        fontsize=8,
                        frameon=False,
                    )
                except Exception:
                    pass
            if hasattr(self, "ax_corr"):
                self.ax_corr.set_title(self.t("corr_title"), fontsize=10)
                self.ax_corr.set_xlabel(self.t("corr_xlabel"), fontsize=8)
                self.ax_corr.set_ylabel(self.t("corr_ylabel"), fontsize=8)
            if hasattr(self, "canvas"):
                self.canvas.draw_idle()
        except Exception:
            pass

        # Re-apply theme after changing labels (keeps Matplotlib label colors consistent)
        try:
            self._apply_theme_to_ui()
        except Exception:
            pass

    def _default_settings(self):
        return {
            "fm_band": {
                "preset": DEFAULT_FM_BAND_PRESET,
            },
            "sdr": {
                "osmosdr_args": "numchan=1 rtl=0",
                "ppm": 0,
                "rf_bandwidth_hz": 200000,
                "gain_db": 42.1,
            },
            "ui": {
                "language": "pl",
                "theme": "light",
            },
            "recording": {
                # Can be relative (to BASE_DIR) or absolute.
                "output_dir": "recordings",
                # Recording format: "mp3" (lossy) or "flac" (lossless).
                "format": "mp3",
            },
            "audio": {
                "demod_rate_hz": 240000,
                "audio_rate_hz": 48000,
                "enable_deemphasis": True,
                "volume_percent": 50,
            },
            "rds": {
                "enable_updates_during_playback": True,
                "update_interval_s": 30,
                # Backend: "rtl_fm" (external) or "gnuradio" (single-dongle).
                "backend": "gnuradio",
            },
            "spectrum": {
                "max_hz": SPECTRUM_MAX_HZ,
                "ymin_dbfs": -90,
                "ymax_dbfs": 0,
                "time_smoothing_alpha": 0.25,
                "freq_smoothing_bins": 1,
                "fps": 66,
                "corr_points": 256,
                "corr_point_alpha": 0.5,
                "corr_marker_size": 2,
            },
        }

    def _load_settings(self):
        defaults = self._default_settings()
        if not os.path.exists(SETTINGS_FILE):
            return defaults
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
        except Exception:
            return defaults

        merged = deepcopy(defaults)
        try:
            for group, vals in (user or {}).items():
                if isinstance(vals, dict) and group in merged:
                    merged[group].update(vals)
        except Exception:
            return defaults
        return merged

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(self.t("log_settings_save_error", e=e))

    def _schedule_save_settings(self, delay_ms: int = 500):
        """Debounced settings save (GUI thread)."""
        try:
            timer = getattr(self, "_settings_save_timer", None)
            if timer:
                try:
                    self.root.after_cancel(timer)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._settings_save_timer = self.root.after(delay_ms, self._flush_scheduled_settings_save)
        except Exception:
            self._settings_save_timer = None

    def _flush_scheduled_settings_save(self):
        try:
            self._settings_save_timer = None
        except Exception:
            pass
        try:
            self._save_settings()
        except Exception:
            pass

    def _apply_settings_to_runtime(self, initial=False):
        """Apply persisted settings to runtime fields."""
        fm_band = self.settings.get("fm_band", {})
        sdr = self.settings.get("sdr", {})
        ui = self.settings.get("ui", {})
        rec = self.settings.get("recording", {})
        audio = self.settings.get("audio", {})
        rds = self.settings.get("rds", {})
        spec = self.settings.get("spectrum", {})

        # FM band preset
        try:
            preset = str(fm_band.get("preset") or DEFAULT_FM_BAND_PRESET)
        except Exception:
            preset = DEFAULT_FM_BAND_PRESET
        if preset not in FM_BAND_PRESETS:
            preset = DEFAULT_FM_BAND_PRESET

        self.fm_band_preset = preset
        p = FM_BAND_PRESETS.get(preset, FM_BAND_PRESETS[DEFAULT_FM_BAND_PRESET])
        try:
            self.fm_min_khz = int(p.get("min_khz", int(round(FM_START * 1000.0))))
            self.fm_max_khz = int(p.get("max_khz", int(round(FM_END * 1000.0))))
            self.fm_step_khz = int(p.get("step_khz", int(round(FM_STEP * 1000.0))))
        except Exception:
            self.fm_min_khz = int(round(FM_START * 1000.0))
            self.fm_max_khz = int(round(FM_END * 1000.0))
            self.fm_step_khz = int(round(FM_STEP * 1000.0))

        if self.fm_step_khz <= 0:
            self.fm_step_khz = 100
        if self.fm_max_khz < self.fm_min_khz:
            self.fm_min_khz, self.fm_max_khz = self.fm_max_khz, self.fm_min_khz

        self.fm_min_mhz = float(self.fm_min_khz) / 1000.0
        self.fm_max_mhz = float(self.fm_max_khz) / 1000.0
        self.fm_step_mhz = float(self.fm_step_khz) / 1000.0

        # Language (UI)
        lang = str(ui.get("language") or self.lang)
        if lang not in I18N:
            # Allow selecting languages not yet translated; fall back to English.
            self.lang = "en"
        else:
            self.lang = lang

        # UI theme
        try:
            theme = str(ui.get("theme") or self.ui_theme or "light").lower().strip()
        except Exception:
            theme = "light"
        if theme not in ("light", "dark"):
            theme = "light"
        self.ui_theme = theme

        # Recording directory
        try:
            out_dir = str(rec.get("output_dir") or "recordings").strip()
        except Exception:
            out_dir = "recordings"
        if not out_dir:
            out_dir = "recordings"
        if os.path.isabs(out_dir):
            self.recordings_dir = out_dir
        else:
            self.recordings_dir = os.path.join((BASE_DIR if DEV_MODE else APP_DATA_DIR), out_dir)
        try:
            os.makedirs(self.recordings_dir, exist_ok=True)
        except Exception:
            pass

        # Recording format
        try:
            rec_fmt = str(rec.get("format") or "mp3").strip().lower()
        except Exception:
            rec_fmt = "mp3"
        if rec_fmt not in ("mp3", "flac"):
            rec_fmt = "mp3"
        self.recording_format = rec_fmt

        self.osmosdr_args = str(sdr.get("osmosdr_args") or self.osmosdr_args)
        try:
            self.ppm = int(sdr.get("ppm", self.ppm))
        except Exception:
            self.ppm = 0
        try:
            self.rf_bandwidth_hz = int(sdr.get("rf_bandwidth_hz", self.rf_bandwidth_hz))
        except Exception:
            self.rf_bandwidth_hz = 200000

        # Persisted gain (main UI slider)
        try:
            current_gain = float(getattr(self, "gain", 42.1))
        except Exception:
            current_gain = 42.1
        try:
            self.gain = round(float(sdr.get("gain_db", current_gain)), 1)
        except Exception:
            self.gain = current_gain
        self.gain = float(max(0.0, min(49.6, float(self.gain))))

        try:
            self.demod_rate = int(audio.get("demod_rate_hz", self.demod_rate))
        except Exception:
            pass
        try:
            self.audio_rate = int(audio.get("audio_rate_hz", self.audio_rate))
        except Exception:
            pass
        self.enable_deemphasis = bool(audio.get("enable_deemphasis", self.enable_deemphasis))

        # Persisted volume (main UI slider)
        try:
            current_vol = int(getattr(self, "volume", 50))
        except Exception:
            current_vol = 50
        try:
            self.volume = int(audio.get("volume_percent", current_vol))
        except Exception:
            self.volume = current_vol
        self.volume = int(max(0, min(100, int(self.volume))))

        self.enable_rds_updates = bool(rds.get("enable_updates_during_playback", self.enable_rds_updates))
        try:
            self.rds_interval_s = int(rds.get("update_interval_s", self.rds_interval_s))
        except Exception:
            self.rds_interval_s = 30
        self.rds_interval_s = max(5, min(600, self.rds_interval_s))

        try:
            backend = str(rds.get("backend") or getattr(self, "rds_backend", "rtl_fm")).strip().lower()
        except Exception:
            backend = "rtl_fm"
        if backend not in ("rtl_fm", "gnuradio"):
            backend = "rtl_fm"
        self.rds_backend = backend

        try:
            self.spectrum_max_hz = int(spec.get("max_hz", self.spectrum_max_hz))
        except Exception:
            self.spectrum_max_hz = SPECTRUM_MAX_HZ
        self.spectrum_max_hz = max(1000, min(24000, self.spectrum_max_hz))

        try:
            self.spectrum_ymin_dbfs = float(spec.get("ymin_dbfs", self.spectrum_ymin_dbfs))
        except Exception:
            self.spectrum_ymin_dbfs = -90.0
        try:
            self.spectrum_ymax_dbfs = float(spec.get("ymax_dbfs", self.spectrum_ymax_dbfs))
        except Exception:
            self.spectrum_ymax_dbfs = 0.0

        if self.spectrum_ymax_dbfs <= self.spectrum_ymin_dbfs:
            self.spectrum_ymin_dbfs, self.spectrum_ymax_dbfs = -90.0, 0.0

        try:
            self.spec_time_alpha = float(spec.get("time_smoothing_alpha", self.spec_time_alpha))
        except Exception:
            self.spec_time_alpha = 0.25
        self.spec_time_alpha = float(max(0.0, min(1.0, self.spec_time_alpha)))

        try:
            self.spec_freq_smooth_bins = int(spec.get("freq_smoothing_bins", self.spec_freq_smooth_bins))
        except Exception:
            self.spec_freq_smooth_bins = 1
        self.spec_freq_smooth_bins = int(max(0, min(10, self.spec_freq_smooth_bins)))

        try:
            self.spec_fps = int(spec.get("fps", self.spec_fps))
        except Exception:
            self.spec_fps = 66
        self.spec_fps = int(max(10, min(120, self.spec_fps)))

        try:
            self.corr_points = int(spec.get("corr_points", self.corr_points))
        except Exception:
            self.corr_points = 256
        self.corr_points = int(max(64, min(2048, self.corr_points)))

        try:
            self.corr_point_alpha = float(spec.get("corr_point_alpha", self.corr_point_alpha))
        except Exception:
            self.corr_point_alpha = 0.5
        self.corr_point_alpha = float(max(0.05, min(1.0, self.corr_point_alpha)))

        try:
            self.corr_marker_size = int(spec.get("corr_marker_size", self.corr_marker_size))
        except Exception:
            self.corr_marker_size = 2
        self.corr_marker_size = int(max(1, min(8, self.corr_marker_size)))

        if not initial:
            self._apply_spectrum_axes_settings()
            self._apply_spectrum_render_settings()

    def _apply_spectrum_axes_settings(self):
        try:
            self.ax_spec.set_xlim(0, self.spectrum_max_hz)
            self.ax_spec.set_ylim(self.spectrum_ymin_dbfs, self.spectrum_ymax_dbfs)
            self.canvas.draw_idle()
        except Exception:
            pass

    def _apply_spectrum_render_settings(self):
        """Apply spectrum/correlation rendering settings (no restart required)."""
        try:
            # Correlation: point style
            if hasattr(self, 'line_corr') and self.line_corr is not None:
                self.line_corr.set_alpha(self.corr_point_alpha)
                self.line_corr.set_markersize(self.corr_marker_size)

            # Correlation: number of points
            self._corr_points = int(self.corr_points)

            # Spectrum: X axis depends on audio_rate
            if hasattr(self, 'line_left') and self.line_left is not None:
                freqs = np.fft.rfftfreq(1024, d=1.0 / float(self.audio_rate))[:512]
                self.line_left.set_xdata(freqs)
                self.line_right.set_xdata(freqs)

            self.canvas.draw_idle()
        except Exception:
            pass
        
    def create_widgets(self):
        """Create the GUI widgets."""
        
        # Main container with panels
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True)
        
        # Left panel
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=2)
        
        # Right panel - spectrum
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)
        
        # === LEFT PANEL ===
        # Use grid() for better alignment and responsiveness.
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(2, weight=5)  # lista stacji
        left_frame.rowconfigure(6, weight=3)  # log

        # Top panel - title and status
        top_frame = ttk.Frame(left_frame, padding=6)
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(0, weight=1)

        self.title_label = ttk.Label(top_frame, text=self.t("title"), font=("Arial", 16, "bold"))
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.status_label = ttk.Label(top_frame, text=self.t("status_ready"), font=("Arial", 10))
        self.status_label.grid(row=1, column=0, sticky="ew")

        # Manual tuning panel
        self.tune_frame = ttk.LabelFrame(left_frame, text=self.t("manual_tuning"), padding=6)
        self.tune_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=3)
        
        freq_input_frame = ttk.Frame(self.tune_frame)
        freq_input_frame.pack()
        
        self.freq_label = ttk.Label(freq_input_frame, text=self.t("frequency_mhz"))
        self.freq_label.pack(side=tk.LEFT, padx=5)
        
        self.freq_entry = ttk.Entry(freq_input_frame, width=10)
        self.freq_entry.pack(side=tk.LEFT, padx=5)
        self.freq_entry.insert(0, "107.5")

        self.freq_step_down_button = ttk.Button(
            freq_input_frame,
            text="-0.1",
            command=lambda: self.step_manual_frequency(-0.1),
        )
        self.freq_step_down_button.pack(side=tk.LEFT, padx=2)

        self.freq_step_up_button = ttk.Button(
            freq_input_frame,
            text="+0.1",
            command=lambda: self.step_manual_frequency(0.1),
        )
        self.freq_step_up_button.pack(side=tk.LEFT, padx=2)
        
        self.tune_button = ttk.Button(freq_input_frame, text=self.t("tune"), command=self.tune_manual_frequency)
        self.tune_button.pack(side=tk.LEFT, padx=5)

        self.save_button = ttk.Button(freq_input_frame, text=self.t("save"), command=self.save_current_station_frequency)
        self.save_button.pack(side=tk.LEFT, padx=5)
        
        # Station list panel
        self.list_frame = ttk.LabelFrame(left_frame, text=self.t("stations"), padding=6)
        self.list_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=3)

        # Modern list: Treeview with columns (full width)
        list_inner = ttk.Frame(self.list_frame)
        list_inner.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_inner, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.station_tree = ttk.Treeview(
            list_inner,
            columns=("freq", "ps", "stereo"),
            show="headings",
            selectmode="browse",
            height=14,
            yscrollcommand=scrollbar.set,
        )
        self.station_tree.heading("freq", text=self.t("stations_col_freq"))
        self.station_tree.heading("ps", text=self.t("stations_col_name"))
        self.station_tree.heading("stereo", text="")

        # Columns: fixed freq, stretchable name, minimal stereo column
        self.station_tree.column("freq", width=80, minwidth=70, anchor=tk.E, stretch=False)
        self.station_tree.column("ps", width=420, minwidth=180, anchor=tk.W, stretch=True)
        self.station_tree.column("stereo", width=90, minwidth=80, anchor=tk.W, stretch=False)

        self.station_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.station_tree.yview)

        # Selection = show info; double click = start playback
        self.station_tree.bind('<<TreeviewSelect>>', self.on_station_select)
        self.station_tree.bind('<Double-1>', self.on_station_double_click)
        
        # Station info panel (read-only, looks more "app-like")
        self.info_frame = ttk.LabelFrame(left_frame, text=self.t("station_info"), padding=6)
        self.info_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=3)

        self.info_title = ttk.Label(self.info_frame, text="", font=("Arial", 10, "bold"))
        self.info_title.pack(fill=tk.X)

        self.info_nowplaying = ttk.Label(self.info_frame, text="", font=("Arial", 9))
        self.info_nowplaying.pack(fill=tk.X, pady=(1, 0))

        self.info_radiotext = ttk.Label(self.info_frame, text="", font=("Arial", 9), wraplength=900, justify=tk.LEFT)
        self.info_radiotext.pack(fill=tk.X, pady=(1, 0))

        self.info_meta = ttk.Label(self.info_frame, text="", font=("Arial", 8))
        self.info_meta.pack(fill=tk.X, pady=(1, 0))

        # Keep text wrapping in sync with container width
        self.info_frame.bind('<Configure>', self._on_info_frame_configure)
        
        # Control panel
        control_frame = ttk.Frame(left_frame, padding=6)
        control_frame.grid(row=4, column=0, sticky="ew", padx=10, pady=3)
        control_frame.columnconfigure(0, weight=1)
        
        # Buttons
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        self.scan_button = ttk.Button(button_frame, text=self.t("scan_band"), command=self.start_scan)
        self.scan_button.pack(side=tk.LEFT, padx=5)
        
        self.play_button = ttk.Button(button_frame, text=self.t("play"), command=self.play_selected_station)
        self.play_button.pack(side=tk.LEFT, padx=5)
        
        self.stop_button = ttk.Button(button_frame, text=self.t("stop"), command=self.stop_playback, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)
        
        # Recording buttons (two separate buttons)
        self.record_start_button = ttk.Button(control_frame, text=self.t("record_start"),
                                             command=self.start_recording,
                                             state=tk.DISABLED)
        self.record_start_button.pack(side=tk.LEFT, padx=5)
        
        self.record_stop_button = ttk.Button(control_frame, text=self.t("record_stop"),
                                            command=self.stop_recording,
                                            state=tk.DISABLED)
        self.record_stop_button.pack(side=tk.LEFT, padx=5)
        
        # === SDR + audio settings panel ===
        self.settings_frame = ttk.LabelFrame(left_frame, text=self.t("sdr_audio_panel"), padding=6)
        self.settings_frame.grid(row=5, column=0, sticky="ew", padx=10, pady=3)
        
        # RTL-SDR gain
        gain_row = ttk.Frame(self.settings_frame)
        gain_row.pack(fill=tk.X, pady=2)
        
        self.gain_text_label = ttk.Label(gain_row, text=self.t("gain"), width=15)
        self.gain_text_label.pack(side=tk.LEFT)
        
        self.gain_label = ttk.Label(gain_row, text=f"{self.gain} dB", width=8)
        self.gain_label.pack(side=tk.LEFT)
        
        self.gain_scale = ttk.Scale(gain_row, from_=0, to=49.6,
                                   orient=tk.HORIZONTAL, length=200,
                                   command=self.on_gain_change)
        self.gain_scale.set(self.gain)
        self.gain_scale.pack(side=tk.LEFT, padx=5)
        
        # Volume
        volume_row = ttk.Frame(self.settings_frame)
        volume_row.pack(fill=tk.X, pady=2)
        
        self.volume_text_label = ttk.Label(volume_row, text=self.t("volume"), width=15)
        self.volume_text_label.pack(side=tk.LEFT)
        
        self.volume_label = ttk.Label(volume_row, text=f"{self.volume}%", width=8)
        self.volume_label.pack(side=tk.LEFT)
        
        self.volume_scale = ttk.Scale(volume_row, from_=0, to=100,
                                     orient=tk.HORIZONTAL, length=200,
                                     command=self.on_volume_change)
        self.volume_scale.set(self.volume)
        self.volume_scale.pack(side=tk.LEFT, padx=5)
        
        # Recording status
        # On some themes ttk.Label ignores fg color; use tk.Label so red is always visible
        self.record_status_label = tk.Label(self.settings_frame, text="", fg="red")
        self.record_status_label.pack(fill=tk.X, pady=2)

        # Separate settings window
        self.settings_button = ttk.Button(self.settings_frame, text=self.t("settings"), command=self.open_settings_window)
        self.settings_button.pack(anchor=tk.W, pady=(8, 0))
        
        # Scan log
        self.log_frame = ttk.LabelFrame(left_frame, text=self.t("log"), padding=4)
        self.log_frame.grid(row=6, column=0, sticky="nsew", padx=10, pady=3)
        
        self.log_text = scrolledtext.ScrolledText(self.log_frame, height=10, 
                                                  font=("Courier", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # === RIGHT PANEL - SPECTRUM ===
        
        self.spectrum_frame = ttk.LabelFrame(right_frame, text=self.t("viz"), padding="10")
        self.spectrum_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Matplotlib figure
        self.fig = Figure(figsize=(5, 6), dpi=80)

        # 1) Spectrum: both channels on one plot + legend
        self.ax_spec = self.fig.add_subplot(211)
        self.ax_spec.set_title(self.t('spec_title'), fontsize=10)
        self.ax_spec.set_ylabel(self.t('spec_ylabel'), fontsize=8)
        self.ax_spec.set_xlim(0, self.spectrum_max_hz)
        self.ax_spec.set_ylim(self.spectrum_ymin_dbfs, self.spectrum_ymax_dbfs)
        self.ax_spec.margins(x=0)
        self.ax_spec.grid(True, alpha=0.3)

        # 2) Second plot: stereo correlation (L vs R) + balance
        self.ax_corr = self.fig.add_subplot(212)
        self.ax_corr.set_title(self.t('corr_title'), fontsize=10)
        self.ax_corr.set_xlabel(self.t('corr_xlabel'), fontsize=8)
        self.ax_corr.set_ylabel(self.t('corr_ylabel'), fontsize=8)
        self.ax_corr.set_xlim(-1.05, 1.05)
        self.ax_corr.set_ylim(-1.05, 1.05)
        self.ax_corr.set_aspect('equal', adjustable='box')
        self.ax_corr.grid(True, alpha=0.3)
        
        self.fig.tight_layout()
        
        # Canvas
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.spectrum_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        # Initialize plot lines
        # FFT is computed for fs=48kHz, so Nyquist is 24 kHz.
        # We clamp X axis to SPECTRUM_MAX_HZ so it doesn't look like "WFM up to 25kHz".
        # IMPORTANT: use true FFT bin frequencies (not linspace), otherwise we compress 0..24kHz
        # into 0..SPECTRUM_MAX_HZ and the plot becomes misleading.
        freqs = np.fft.rfftfreq(1024, d=1.0 / float(self.audio_rate))[:512]
        self.line_left, = self.ax_spec.plot(freqs, np.full(512, -90.0), 'b-', linewidth=1, label=self.t('left'))
        self.line_right, = self.ax_spec.plot(freqs, np.full(512, -90.0), 'r-', linewidth=1, label=self.t('right'))
        self.ax_spec.legend(loc='upper right', fontsize=8, frameon=False)

        # Helper lines + correlation points
        self._corr_diag, = self.ax_corr.plot([-1, 1], [-1, 1], color='0.6', linewidth=1)
        self._corr_zero_h = self.ax_corr.axhline(0.0, color='0.85', linewidth=1)
        self._corr_zero_v = self.ax_corr.axvline(0.0, color='0.85', linewidth=1)
        self.line_corr, = self.ax_corr.plot([], [], 'k.', markersize=self.corr_marker_size, alpha=self.corr_point_alpha)
        self.corr_text = self.ax_corr.text(
            0.02, 0.98, '',
            transform=self.ax_corr.transAxes,
            ha='left', va='top', fontsize=8
        )

    def open_settings_window(self):
        """Open the settings window (Toplevel)."""
        if hasattr(self, "_settings_win") and self._settings_win is not None:
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                return
            except Exception:
                self._settings_win = None

        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title(self.t("settings_title"))
        # Keep layout simple (no scrolling UI). Fit to screen so bottom buttons stay visible.
        try:
            screen_h = int(win.winfo_screenheight() or 900)
        except Exception:
            screen_h = 900
        # Target height: ~84% of screen (+2%), clamped.
        target_h = int(max(680, min(1000, round(screen_h * 0.84))))
        min_h = int(max(600, min(840, round(screen_h * 0.72))))
        win.geometry(f"560x{target_h}")
        win.minsize(520, min_h)
        try:
            win.transient(self.root)
        except Exception:
            pass

        container = ttk.Frame(win, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        fm_band = self.settings.get("fm_band", {})
        ui = self.settings.get("ui", {})
        rec = self.settings.get("recording", {})
        sdr = self.settings.get("sdr", {})
        audio = self.settings.get("audio", {})
        rds = self.settings.get("rds", {})
        spec = self.settings.get("spectrum", {})

        # Language selection
        current_lang = str(ui.get("language", getattr(self, "lang", "pl")) or "pl")
        current_lang_disp = None
        for code, pl_name, native_name in TOP25_UI_LANGUAGES:
            if code == current_lang:
                current_lang_disp = f"{pl_name} — {native_name} ({code})"
                break
        if current_lang_disp is None:
            current_lang_disp = f"Polski — Polski (pl)"

        var_osmosdr = tk.StringVar(value=str(sdr.get("osmosdr_args", self.osmosdr_args)))
        var_ppm = tk.StringVar(value=str(sdr.get("ppm", self.ppm)))
        var_bw = tk.StringVar(value=str(int(sdr.get("rf_bandwidth_hz", self.rf_bandwidth_hz) / 1000)))

        var_lang = tk.StringVar(value=current_lang_disp)
        var_rec_dir = tk.StringVar(value=str(rec.get("output_dir", "recordings")))
        _rec_fmt = str(rec.get("format") or "mp3").strip().lower()
        if _rec_fmt not in ("mp3", "flac"):
            _rec_fmt = "mp3"
        var_rec_format = tk.StringVar(value=_rec_fmt)

        var_dark_mode = tk.BooleanVar(value=(str(ui.get("theme") or getattr(self, "ui_theme", "light")) == "dark"))

        # FM band preset
        preset_order = [
            "worldwide",
            "us_ca",
            "japan",
            "japan_wide",
            "brazil",
            "oirt",
        ]
        preset_display = {
            "worldwide": "Worldwide: 87.5–108.0 MHz (100 kHz)",
            "us_ca": "US/Canada: 87.9–107.9 MHz (200 kHz)",
            "japan": "Japan: 76.0–95.0 MHz (100 kHz)",
            "japan_wide": "Japan (wide): 76.0–99.0 MHz (100 kHz)",
            "brazil": "Brazil: 76.1–108.0 MHz (100 kHz)",
            "oirt": "OIRT (legacy): 65.8–74.0 MHz (100 kHz)",
        }
        preset_by_display = {v: k for k, v in preset_display.items()}

        current_preset = str(fm_band.get("preset") or DEFAULT_FM_BAND_PRESET)
        if current_preset not in preset_display:
            current_preset = DEFAULT_FM_BAND_PRESET
        var_fm_band = tk.StringVar(value=preset_display.get(current_preset, preset_display[DEFAULT_FM_BAND_PRESET]))

        var_demod = tk.StringVar(value=str(audio.get("demod_rate_hz", self.demod_rate)))
        var_audio = tk.StringVar(value=str(audio.get("audio_rate_hz", self.audio_rate)))
        var_deemph = tk.BooleanVar(value=bool(audio.get("enable_deemphasis", self.enable_deemphasis)))

        var_rds_enable = tk.BooleanVar(value=bool(rds.get("enable_updates_during_playback", self.enable_rds_updates)))
        var_rds_interval = tk.StringVar(value=str(rds.get("update_interval_s", self.rds_interval_s)))

        var_spec_max = tk.StringVar(value=str(spec.get("max_hz", self.spectrum_max_hz)))
        var_spec_ymin = tk.StringVar(value=str(spec.get("ymin_dbfs", self.spectrum_ymin_dbfs)))
        var_spec_ymax = tk.StringVar(value=str(spec.get("ymax_dbfs", self.spectrum_ymax_dbfs)))
        var_spec_alpha = tk.StringVar(value=str(spec.get("time_smoothing_alpha", self.spec_time_alpha)))
        var_spec_fbins = tk.StringVar(value=str(spec.get("freq_smoothing_bins", self.spec_freq_smooth_bins)))
        var_spec_fps = tk.StringVar(value=str(spec.get("fps", self.spec_fps)))
        var_corr_points = tk.StringVar(value=str(spec.get("corr_points", self.corr_points)))
        var_corr_alpha = tk.StringVar(value=str(spec.get("corr_point_alpha", self.corr_point_alpha)))
        var_corr_msize = tk.StringVar(value=str(spec.get("corr_marker_size", self.corr_marker_size)))

        lf_ui = ttk.LabelFrame(container, text=self.t("group_ui"), padding=10)
        lf_ui.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(lf_ui)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("language"), width=14).pack(side=tk.LEFT)
        ttk.Combobox(
            row,
            textvariable=var_lang,
            values=self._language_display_list(),
            state="readonly",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row = ttk.Frame(lf_ui)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("recordings_dir"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_rec_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row = ttk.Frame(lf_ui)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("recording_format"), width=14).pack(side=tk.LEFT)
        ttk.Combobox(
            row,
            textvariable=var_rec_format,
            values=["mp3", "flac"],
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT)

        row = ttk.Frame(lf_ui)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("dark_mode"), width=14).pack(side=tk.LEFT)
        ttk.Checkbutton(row, variable=var_dark_mode).pack(side=tk.LEFT)

        row = ttk.Frame(lf_ui)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("fm_band"), width=14).pack(side=tk.LEFT)
        ttk.Combobox(
            row,
            textvariable=var_fm_band,
            values=[preset_display[p] for p in preset_order if p in preset_display],
            state="readonly",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        lf_sdr = ttk.LabelFrame(container, text=self.t("group_sdr"), padding=10)
        lf_sdr.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(lf_sdr)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("osmosdr_args"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_osmosdr).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row = ttk.Frame(lf_sdr)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("ppm"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_ppm, width=8).pack(side=tk.LEFT)

        row = ttk.Frame(lf_sdr)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("bw_khz"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_bw, width=8).pack(side=tk.LEFT)

        lf_audio = ttk.LabelFrame(container, text=self.t("group_audio"), padding=10)
        lf_audio.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(lf_audio)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("demod_rate"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_demod, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_audio)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("audio_rate"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_audio, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_audio)
        row.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(row, text=self.t("deemphasis"), variable=var_deemph).pack(side=tk.LEFT)

        lf_rds = ttk.LabelFrame(container, text=self.t("group_rds"), padding=10)
        lf_rds.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(lf_rds)
        row.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(row, text=self.t("rds_updates"), variable=var_rds_enable).pack(side=tk.LEFT)

        row = ttk.Frame(lf_rds)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("interval_s"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_rds_interval, width=8).pack(side=tk.LEFT)

        lf_spec = ttk.LabelFrame(container, text=self.t("group_spectrum"), padding=10)
        lf_spec.pack(fill=tk.X)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("max_hz"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_max, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("ymin_dbfs"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_ymin, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("ymax_dbfs"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_ymax, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("smooth_time"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_alpha, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("smooth_freq"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_fbins, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("fps"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_spec_fps, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("corr_points"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_corr_points, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("corr_alpha"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_corr_alpha, width=10).pack(side=tk.LEFT)

        row = ttk.Frame(lf_spec)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=self.t("corr_size"), width=14).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var_corr_msize, width=10).pack(side=tk.LEFT)

        btns = ttk.Frame(container)
        btns.pack(fill=tk.X, pady=(12, 0))

        def _close():
            try:
                win.destroy()
            except Exception:
                pass
            self._settings_win = None

        def _apply():
            try:
                new_osmo = var_osmosdr.get().strip() or "numchan=1 rtl=0"
                new_ppm = int(var_ppm.get().strip() or "0")
                new_bw_khz = int(var_bw.get().strip() or "200")
                new_bw_hz = max(0, new_bw_khz) * 1000

                new_demod = int(var_demod.get().strip() or str(self.demod_rate))
                new_audio = int(var_audio.get().strip() or str(self.audio_rate))
                if new_demod <= 0 or new_audio <= 0:
                    raise ValueError(self.t("err_demod_audio_positive"))
                if (new_demod % new_audio) != 0:
                    raise ValueError(self.t("err_demod_multiple_audio"))

                new_deemph = bool(var_deemph.get())

                new_rds_enable = bool(var_rds_enable.get())
                new_rds_interval = int(var_rds_interval.get().strip() or "30")
                new_rds_interval = max(5, min(600, new_rds_interval))

                new_spec_max = int(var_spec_max.get().strip() or str(self.spectrum_max_hz))
                new_spec_max = max(1000, min(24000, new_spec_max))
                new_ymin = float(var_spec_ymin.get().strip() or str(self.spectrum_ymin_dbfs))
                new_ymax = float(var_spec_ymax.get().strip() or str(self.spectrum_ymax_dbfs))
                if new_ymax <= new_ymin:
                    raise ValueError(self.t("err_ymax_gt_ymin"))

                new_spec_alpha = float(var_spec_alpha.get().strip() or str(self.spec_time_alpha))
                if not (0.0 <= new_spec_alpha <= 1.0):
                    raise ValueError(self.t("err_smooth_time_range"))

                new_spec_fbins = int(var_spec_fbins.get().strip() or str(self.spec_freq_smooth_bins))
                if not (0 <= new_spec_fbins <= 10):
                    raise ValueError(self.t("err_smooth_freq_range"))

                new_spec_fps = int(var_spec_fps.get().strip() or str(self.spec_fps))
                if not (10 <= new_spec_fps <= 120):
                    raise ValueError(self.t("err_fps_range"))

                new_corr_points = int(var_corr_points.get().strip() or str(self.corr_points))
                if not (64 <= new_corr_points <= 2048):
                    raise ValueError(self.t("err_corr_points_range"))

                new_corr_alpha = float(var_corr_alpha.get().strip() or str(self.corr_point_alpha))
                if not (0.05 <= new_corr_alpha <= 1.0):
                    raise ValueError(self.t("err_corr_alpha_range"))

                new_corr_msize = int(var_corr_msize.get().strip() or str(self.corr_marker_size))
                if not (1 <= new_corr_msize <= 8):
                    raise ValueError(self.t("err_corr_size_range"))

                new_rec_dir = (var_rec_dir.get() or "recordings").strip()
                if not new_rec_dir:
                    new_rec_dir = "recordings"

                new_rec_format = str(var_rec_format.get() or "mp3").strip().lower()
                if new_rec_format not in ("mp3", "flac"):
                    new_rec_format = "mp3"

                chosen_fm_disp = str(var_fm_band.get() or "").strip()
                new_fm_preset = preset_by_display.get(chosen_fm_disp) or DEFAULT_FM_BAND_PRESET
                if new_fm_preset not in FM_BAND_PRESETS:
                    new_fm_preset = DEFAULT_FM_BAND_PRESET

                new_theme = "dark" if bool(var_dark_mode.get()) else "light"
            except Exception as e:
                messagebox.showerror(self.t("err"), self.t("invalid_settings", e=e))
                return

            # Language
            chosen_lang_disp = str(var_lang.get() or "")
            new_lang = self._language_code_from_display(chosen_lang_disp) or "en"
            prev_lang = getattr(self, "lang", "pl")
            language_changed = (new_lang != prev_lang)

            restart_needed = (
                (new_osmo != self.osmosdr_args)
                or (new_ppm != self.ppm)
                or (int(new_bw_hz) != int(self.rf_bandwidth_hz))
                or (new_demod != int(self.demod_rate))
                or (new_audio != int(self.audio_rate))
                or (new_deemph != bool(self.enable_deemphasis))
            )

            self.settings["ui"] = {
                "language": new_lang,
                "theme": new_theme,
            }
            self.settings["fm_band"] = {
                "preset": new_fm_preset,
            }
            self.settings["recording"] = {
                "output_dir": new_rec_dir,
                "format": new_rec_format,
            }
            # Preserve any extra keys (e.g. gain_db/volume_percent) while updating known ones.
            prev_sdr = dict((self.settings.get("sdr") or {}) if isinstance(self.settings.get("sdr"), dict) else {})
            prev_sdr.update({
                "osmosdr_args": new_osmo,
                "ppm": new_ppm,
                "rf_bandwidth_hz": int(new_bw_hz),
            })
            self.settings["sdr"] = prev_sdr

            prev_audio = dict((self.settings.get("audio") or {}) if isinstance(self.settings.get("audio"), dict) else {})
            prev_audio.update({
                "demod_rate_hz": int(new_demod),
                "audio_rate_hz": int(new_audio),
                "enable_deemphasis": bool(new_deemph),
            })
            self.settings["audio"] = prev_audio
            # Preserve any extra keys (e.g. backend) while updating known ones.
            prev_rds = dict((self.settings.get("rds") or {}) if isinstance(self.settings.get("rds"), dict) else {})
            prev_rds.update({
                "enable_updates_during_playback": bool(new_rds_enable),
                "update_interval_s": int(new_rds_interval),
            })
            self.settings["rds"] = prev_rds
            self.settings["spectrum"] = {
                "max_hz": int(new_spec_max),
                "ymin_dbfs": float(new_ymin),
                "ymax_dbfs": float(new_ymax),
                "time_smoothing_alpha": float(new_spec_alpha),
                "freq_smoothing_bins": int(new_spec_fbins),
                "fps": int(new_spec_fps),
                "corr_points": int(new_corr_points),
                "corr_point_alpha": float(new_corr_alpha),
                "corr_marker_size": int(new_corr_msize),
            }

            self._save_settings()
            self._apply_settings_to_runtime(initial=False)

            # Theme may change without language change, so apply it explicitly.
            try:
                self._apply_theme_to_ui()
            except Exception:
                pass

            if language_changed:
                try:
                    self._apply_language_to_ui()
                except Exception:
                    pass

            if not self.enable_rds_updates:
                self.rds_updating = False

            if self.playing and self.current_station and restart_needed:
                if messagebox.askyesno(self.t("apply_now_title"), self.t("apply_now_msg")):
                    station = self.current_station
                    self.stop_playback()
                    time.sleep(0.2)
                    self.play_station(station)

            self.log(self.t("settings_saved"))

            if language_changed:
                try:
                    geo = None
                    try:
                        geo = win.geometry()
                    except Exception:
                        geo = None
                    try:
                        win.destroy()
                    except Exception:
                        pass
                    self._settings_win = None

                    def _reopen():
                        try:
                            self.open_settings_window()
                            if geo and getattr(self, "_settings_win", None) is not None:
                                try:
                                    self._settings_win.geometry(geo)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    self.root.after(0, _reopen)
                except Exception:
                    pass

        ttk.Button(btns, text=self.t("apply"), command=_apply).pack(side=tk.LEFT)
        ttk.Button(btns, text=self.t("close"), command=_close).pack(side=tk.RIGHT)
        win.protocol("WM_DELETE_WINDOW", _close)

        # Ensure window is tall enough to show bottom buttons.
        # We avoid scrollbars (per UX choice) by auto-growing up to ~96% of screen.
        try:
            win.update_idletasks()
            req_h = int(win.winfo_reqheight() or 0)
            cur_w = int(win.winfo_width() or 560)
            cur_h = int(win.winfo_height() or 0)
            screen_h2 = int(win.winfo_screenheight() or 900)
            desired_h = min(int(screen_h2 * 0.96), req_h + 40)
            if desired_h > cur_h and desired_h > 0:
                win.geometry(f"{cur_w}x{desired_h}")
                try:
                    win.minsize(520, min(desired_h, int(screen_h2 * 0.96)))
                except Exception:
                    pass
        except Exception:
            pass
        
    def update_station_list(self):
        """Aktualizuj listę stacji w GUI"""
        stations = self.db.get_stations_with_rds()

        # Clear Treeview
        try:
            for iid in self.station_tree.get_children(""):
                self.station_tree.delete(iid)
        except Exception:
            pass
        self._station_by_iid = {}

        if not stations:
            # Placeholder: do not add a fake row (Treeview is a table)
            self.log(self.t("log_no_stations"))
            return

        for station in stations:
            iid = f"{station.freq:.1f}"
            stereo_txt = "STEREO" if station.stereo else ""
            ps_txt = station.ps or self.t("unknown")
            self._station_by_iid[iid] = station
            try:
                self.station_tree.insert("", tk.END, iid=iid, values=(f"{station.freq:.1f}", ps_txt, stereo_txt))
            except Exception:
                # If IID duplicates (rare), add a suffix
                iid2 = f"{iid}_{len(self._station_by_iid)}"
                self._station_by_iid[iid2] = station
                self.station_tree.insert("", tk.END, iid=iid2, values=(f"{station.freq:.1f}", ps_txt, stereo_txt))
    
    def log(self, message):
        """Append a message to the log."""
        # Always write to the debug file (works even if the GUI is broken)
        try:
            debug_log(f"LOG: {message}")
        except Exception:
            pass

        # GUI logging must go through the main thread (Tkinter is not thread-safe)
        self._log_queue.put(message)
        if not self._log_flush_scheduled:
            self._log_flush_scheduled = True
            try:
                self.root.after(0, self._flush_log_queue)
            except Exception:
                self._log_flush_scheduled = False

    def _flush_log_queue(self):
        self._log_flush_scheduled = False
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            while True:
                try:
                    msg = self._log_queue.get_nowait()
                except Exception:
                    break

                try:
                    self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
                    self.log_text.see(tk.END)
                except Exception:
                    # If the widget doesn't exist / is destroyed, ignore the GUI; the debug file still has the entry
                    pass
        finally:
            # If new entries arrived during flush, schedule another flush
            try:
                if not self._log_queue.empty() and not self._log_flush_scheduled:
                    self._log_flush_scheduled = True
                    self.root.after(0, self._flush_log_queue)
            except Exception:
                pass
        
    def update_station_info(self, station):
        """Update the station info panel."""
        if not station:
            self.info_title.config(text="")
            self.info_nowplaying.config(text="")
            self.info_radiotext.config(text="")
            self.info_meta.config(text="")
            return

        # Show only RDS-derived station name (PS) in the info header.
        self.info_title.config(text=f"{station.ps or self.t('unknown')}")

        now_playing = None
        try:
            now_playing = station.get_now_playing()
        except Exception:
            now_playing = None

        # Show the now-playing content directly (no localized "Now playing:" prefix).
        self.info_nowplaying.config(text=(now_playing or ""))
        self.info_radiotext.config(text=(station.radiotext or ""))

        meta_parts = []
        if station.prog_type:
            meta_parts.append(station.prog_type)
        if station.pi:
            meta_parts.append(f"PI {station.pi}")
        if station.tp:
            meta_parts.append("TP")
        if station.ta:
            meta_parts.append("TA")
        self.info_meta.config(text=(" • ".join(meta_parts) if meta_parts else ""))

    def _on_info_frame_configure(self, event):
        """Adjust radiotext wraplength to the current width."""
        try:
            # -20 px for padding/borders; avoid wraplength=0
            wl = max(200, int(event.width) - 20)
            self.info_radiotext.config(wraplength=wl)
        except Exception:
            pass

    def on_station_select(self, event=None):
        """Handle selection changes in the station list."""
        try:
            sel = self.station_tree.selection()
            if not sel:
                return
            iid = sel[0]
            station = self._station_by_iid.get(iid)
            if station:
                self.update_station_info(station)
        except Exception:
            pass
    
    def on_volume_change(self, value):
        """Handle volume changes."""
        self.volume = int(float(value))
        self.volume_label.config(text=f"{self.volume}%")
        try:
            audio = self.settings.setdefault("audio", {})
            if isinstance(audio, dict):
                audio["volume_percent"] = int(self.volume)
        except Exception:
            pass
        self._schedule_save_settings()
    
    def on_gain_change(self, value):
        """Handle RTL-SDR gain changes."""
        self.gain = round(float(value), 1)
        self.gain_label.config(text=f"{self.gain} dB")

        try:
            sdr = self.settings.setdefault("sdr", {})
            if isinstance(sdr, dict):
                sdr["gain_db"] = float(self.gain)
        except Exception:
            pass
        self._schedule_save_settings()
        
        # Cancel previous timer if it exists
        if self.gain_change_timer:
            self.root.after_cancel(self.gain_change_timer)
        
        # If playing, apply live (after debounce).
        if self.playing and self.current_station:
            self.gain_change_timer = self.root.after(1000, self.apply_gain_change)
    
    def apply_gain_change(self):
        """Apply gain change (called after the debounce timeout)."""
        if self.playing and self.current_station:
            self.log(self.t("log_apply_gain", gain=self.gain))

            # Prefer live gain change to avoid restarting the flowgraph and
            # re-opening the RTL-SDR (which can briefly fail with usb_claim_interface).
            src = getattr(self, "gr_src", None)
            if src is not None:
                try:
                    src.set_gain(float(self.gain), 0)
                except Exception:
                    try:
                        src.set_gain(float(self.gain))
                    except Exception:
                        pass
        self.gain_change_timer = None
    
    def on_station_double_click(self, event):
        """Handle double-click on a station."""
        self.play_selected_station()
    
    def tune_manual_frequency(self):
        """Tune to the manually entered frequency."""
        try:
            freq_str = self.freq_entry.get().strip()
            freq = float(freq_str)

            fm_min = float(getattr(self, "fm_min_mhz", FM_START))
            fm_max = float(getattr(self, "fm_max_mhz", FM_END))

            if not (fm_min <= freq <= fm_max):
                messagebox.showerror(self.t("err"), self.t("freq_out_of_range", min=fm_min, max=fm_max))
                return

            # If playback is active, Tune should retune the currently playing station
            # (do not change DB/station.freq unless Save is used).
            if self.playing and self.current_station is not None:
                self.play_station(self.current_station, tuned_freq_mhz=freq)
                return

            # Otherwise: tune an ad-hoc station.
            station = FMStation(freq)
            station.ps = f"FM {freq:.1f}"
            self.play_station(station)
            
        except ValueError:
            messagebox.showerror(self.t("err"), self.t("bad_freq"))

    def step_manual_frequency(self, delta_mhz: float):
        """Adjust the manual frequency entry by +/- delta_mhz (does not tune)."""
        try:
            cur_txt = (self.freq_entry.get() or "").strip()
            if cur_txt:
                cur = float(cur_txt)
            elif self.current_station is not None:
                cur = float(getattr(self.current_station, "freq", 0.0) or 0.0)
            else:
                cur = FM_START

            new_freq = round(cur + float(delta_mhz), 1)
            fm_min = float(getattr(self, "fm_min_mhz", FM_START))
            fm_max = float(getattr(self, "fm_max_mhz", FM_END))
            if new_freq < fm_min:
                new_freq = fm_min
            if new_freq > fm_max:
                new_freq = fm_max

            self.freq_entry.delete(0, tk.END)
            self.freq_entry.insert(0, f"{new_freq:.1f}")
        except Exception:
            pass

    def save_current_station_frequency(self):
        """Save manual frequency to the currently playing station, persist DB, and retune playback."""
        if not self.playing or not self.current_station:
            messagebox.showwarning(self.t("warn"), self.t("need_playback_first"))
            return

        try:
            freq_str = self.freq_entry.get().strip()
            freq = float(freq_str)
        except Exception:
            messagebox.showerror(self.t("err"), self.t("bad_freq"))
            return

        fm_min = float(getattr(self, "fm_min_mhz", FM_START))
        fm_max = float(getattr(self, "fm_max_mhz", FM_END))
        if not (fm_min <= freq <= fm_max):
            messagebox.showerror(self.t("err"), self.t("freq_out_of_range", min=fm_min, max=fm_max))
            return

        new_freq = round(float(freq), 1)
        station = self.current_station
        old_freq = float(getattr(station, "freq", new_freq) or new_freq)

        if abs(old_freq - new_freq) < 1e-9:
            # Still retune to be explicit (keeps UX consistent).
            self.play_station(station, tuned_freq_mhz=new_freq)
            return

        # Update DB key (freq is used as the primary key).
        try:
            if hasattr(self, "db") and self.db is not None:
                try:
                    if old_freq in self.db.stations:
                        self.db.stations.pop(old_freq, None)
                except Exception:
                    pass

                station.freq = new_freq
                self.db.add_or_update(station)
                self.db.save()
        except Exception:
            # Fallback: still update in-memory and retune.
            try:
                station.freq = new_freq
            except Exception:
                pass

        # Refresh list and retune immediately.
        try:
            self.update_station_list()
        except Exception:
            pass

        self.play_station(station, tuned_freq_mhz=new_freq)
    
    def play_selected_station(self):
        """Play the currently selected station."""
        try:
            sel = self.station_tree.selection()
        except Exception:
            sel = None

        if not sel:
            messagebox.showwarning(self.t("warn"), self.t("pick_station"))
            return

        iid = sel[0]
        station = self._station_by_iid.get(iid)
        if not station:
            # Fallback: try resolving by frequency.
            try:
                freq = float(iid.split('_')[0])
                station = self.db.stations.get(freq)
            except Exception:
                station = None

        if not station:
            messagebox.showwarning(self.t("warn"), self.t("station_not_found"))
            return

        self.play_station(station)
    
    def play_station(self, station, tuned_freq_mhz=None):
        """Play an FM station."""
        # Keep an explicit tuned frequency override so Tune can retune without
        # mutating the station's stored frequency (DB key).
        try:
            if tuned_freq_mhz is None:
                self.tuned_freq_mhz = float(getattr(station, 'freq', 0.0) or 0.0)
            else:
                self.tuned_freq_mhz = float(tuned_freq_mhz)
        except Exception:
            self.tuned_freq_mhz = tuned_freq_mhz

        if self.playing:
            # Switching stations needs a hard stop of the current GNU Radio flowgraph
            # before re-opening the RTL-SDR, otherwise the first attempt can fail with
            # "Failed to open rtlsdr device".
            self._switch_station_async(station)
            return

        self._start_station_playback(station)

    def _switch_station_async(self, station):
        """Stop current playback and start a new station once the SDR is released."""
        try:
            self.stop_playback(quiet=True)
        except Exception:
            pass

        stop_event = getattr(self, "_gr_stop_event", None)

        def _wait_and_start():
            try:
                if stop_event is not None:
                    stop_event.wait(timeout=3.0)
                time.sleep(0.15)
            except Exception:
                pass
            try:
                self.root.after(0, lambda: self._start_station_playback(station))
            except Exception:
                pass

        threading.Thread(target=_wait_and_start, daemon=True).start()

    def _start_station_playback(self, station):
        """Start playback for a station (assumes nothing is currently playing)."""

        play_freq = float(getattr(self, "tuned_freq_mhz", None) or station.freq)

        self.log(self.t("log_playing", freq=play_freq, ps=station.ps))
        self.log(self.t("log_gain", gain=self.gain))
        try:
            self.status_label.config(text=self.t("playing", name=(station.ps or station.freq)))
        except Exception:
            pass
        self.update_station_info(station)
        self.current_station = station
        
        try:
            if not _GNURADIO_OK:
                raise RuntimeError("Brak GNU Radio/osmosdr – nie można uruchomić stereo RX")

            # GNU Radio: stereo L/R (wfm_rcv_pll)
            self._start_gnuradio_rx(play_freq, self.gain)
            
            # sox play - stereo S16_LE @ 48k
            play_cmd = ['play', '-t', 'raw', '-r', '48k', '-e', 'signed',
                       '-b', '16', '-c', '2', '-V1', '-q',
                       '--buffer', '8192', '-']  # Larger sox buffer
            
            self.play_proc = subprocess.Popen(play_cmd,
                                             stdin=subprocess.PIPE,  # We write manually
                                             stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL,
                                             start_new_session=True,
                                             bufsize=65536)  # 64KB bufor

            # Update manual tuning field only once playback is successfully started.
            try:
                self.freq_entry.delete(0, tk.END)
                self.freq_entry.insert(0, f"{float(play_freq):.1f}")
            except Exception:
                pass
            
            # Start the thread that reads audio and feeds sox.
            self.playing = True
            self.spectrum_running = True
            self.rds_updating = True
            
            # Audio streaming thread.
            audio_thread = threading.Thread(target=self.stream_audio, daemon=True)
            audio_thread.start()
            
            # Spectrum thread (separate).
            spectrum_thread = threading.Thread(target=self.spectrum_analyzer, daemon=True)
            spectrum_thread.start()
            
            # RDS updates (optional).
            # With a single RTL-SDR, external rtl_fm cannot run while osmosdr is active.
            # Prefer GNU Radio MPX → redsea when configured.
            if getattr(self, "enable_rds_updates", True):
                if str(getattr(self, "rds_backend", "rtl_fm")) == "gnuradio":
                    # Start GNU Radio → redsea reader after flags are set (avoid race).
                    try:
                        if getattr(self, "_rds_proc", None) is not None and getattr(self._rds_proc, "stdout", None) is not None:
                            self._start_rds_reader_thread()
                    except Exception:
                        pass
                else:
                    rds_thread = threading.Thread(target=self.rds_updater, daemon=True)
                    rds_thread.start()
            
            self.play_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)
            self.scan_button.config(state=tk.DISABLED)
            self.record_start_button.config(state=tk.NORMAL)  # Enable recording start
            self.record_stop_button.config(state=tk.DISABLED)  # Stop jest disabled
            
        except Exception as e:
            self.log(self.t("log_playback_error", e=e))
            messagebox.showerror(self.t("err"), f"Nie można odtworzyć stacji: {e}")

    def _start_gnuradio_rx(self, freq_mhz, gain_db):
        """Start GNU Radio RX and expose stereo PCM (S16_LE, interleaved) via a pipe for stream_audio()."""
        self._stop_gnuradio_rx()

        # Keep strong refs to blocks; otherwise Python GC can collect them and
        # close underlying file descriptors while the flowgraph is running.
        self._gr_blocks = {}

        # Pipe used to transport PCM.
        audio_r_fd, audio_w_fd = os.pipe()
        self._gr_pipe_r = audio_r_fd
        self._gr_pipe_w = audio_w_fd
        try:
            os.set_blocking(self._gr_pipe_r, False)
        except Exception:
            pass

        try:
            debug_log(f"DEBUG: GNURadio audio pipe fds: r={audio_r_fd} w={audio_w_fd}")
        except Exception:
            pass

        deemph_tau = 50e-6 if getattr(self, "enable_deemphasis", True) else 0.0
        audio_decim = int(self.demod_rate // self.audio_rate)
        if self.demod_rate % self.audio_rate != 0:
            raise RuntimeError(f"demod_rate={self.demod_rate} musi być wielokrotnością audio_rate={self.audio_rate}")

        tb = gr.top_block()
        try:
            self._gr_blocks["tb"] = tb
        except Exception:
            pass

        # RTL-SDR source (osmosdr)
        args = getattr(self, "osmosdr_args", "numchan=1 rtl=0")
        try:
            src = osmosdr.source(args=str(args))
        except Exception:
            src = osmosdr.source(args="numchan=1")

        try:
            self._gr_blocks["src"] = src
        except Exception:
            pass

        src.set_sample_rate(self.demod_rate)
        src.set_center_freq(freq_mhz * 1e6, 0)
        try:
            src.set_freq_corr(int(getattr(self, "ppm", 0)), 0)
        except Exception:
            pass
        try:
            src.set_gain(gain_db, 0)
        except Exception:
            pass
        try:
            src.set_bandwidth(int(getattr(self, "rf_bandwidth_hz", 200000)), 0)
        except Exception:
            pass

        rx = analog.wfm_rcv_pll(int(self.demod_rate), int(audio_decim), float(deemph_tau))
        try:
            self._gr_blocks["rx"] = rx
        except Exception:
            pass

        # Optional: GNU Radio MPX (composite-ish) branch for RDS decoding (single-dongle).
        # IMPORTANT: decouple the GNU Radio sink from the redsea process to avoid BrokenPipe/abort
        # when redsea exits. We always keep the pipe read end in this process.
        rds_enabled = bool(getattr(self, "enable_rds_updates", False)) and (str(getattr(self, "rds_backend", "rtl_fm")) == "gnuradio")
        rds_sink = None
        rds_f2s = None
        rds_resamp = None
        qdemod = None
        self._rds_last_save_ts = 0.0
        if rds_enabled:
            try:
                # Pipe for GNU Radio -> Python (we feed redsea ourselves).
                rds_audio_r_fd, rds_audio_w_fd = os.pipe()
                self._rds_audio_pipe_r = rds_audio_r_fd
                self._rds_audio_pipe_w = rds_audio_w_fd
                try:
                    os.set_blocking(self._rds_audio_pipe_r, False)
                except Exception:
                    pass

                try:
                    debug_log(f"DEBUG: GNURadio RDS pipe fds: r={rds_audio_r_fd} w={rds_audio_w_fd}")
                except Exception:
                    pass

                # Open read end for the feeder thread.
                self._rds_audio_pipe_file = os.fdopen(self._rds_audio_pipe_r, 'rb', buffering=0)

                # complex -> quadrature demod (approx FM broadcast deviation 75 kHz)
                try:
                    demod_rate = int(self.demod_rate)
                except Exception:
                    demod_rate = int(RDS_SAMPLE_RATE)
                demod_gain = float(demod_rate) / (2.0 * math.pi * 75e3)
                qdemod = analog.quadrature_demod_cf(demod_gain)
                try:
                    self._gr_blocks["qdemod"] = qdemod
                except Exception:
                    pass

                # Resample to exactly 171 kHz (reduce ratio to keep resampler small)
                g = int(math.gcd(int(RDS_SAMPLE_RATE), int(demod_rate))) if demod_rate > 0 else 1
                interp = int(RDS_SAMPLE_RATE // g)
                decim = int(demod_rate // g) if g else int(demod_rate)
                if interp <= 0 or decim <= 0:
                    interp, decim = int(RDS_SAMPLE_RATE), int(max(1, demod_rate))
                rds_resamp = filter.rational_resampler_fff(
                    interpolation=interp,
                    decimation=decim,
                    taps=[],
                    fractional_bw=0.4,
                )

                try:
                    self._gr_blocks["rds_resamp"] = rds_resamp
                except Exception:
                    pass

                # float -> short PCM for redsea
                rds_f2s = blocks.float_to_short(1, 32767.0)
                rds_sink = blocks.file_descriptor_sink(gr.sizeof_short, rds_audio_w_fd)

                try:
                    self._gr_blocks["rds_f2s"] = rds_f2s
                    self._gr_blocks["rds_sink"] = rds_sink
                except Exception:
                    pass

                tb.connect((src, 0), (qdemod, 0))
                tb.connect((qdemod, 0), (rds_resamp, 0))
                tb.connect((rds_resamp, 0), (rds_f2s, 0))
                tb.connect((rds_f2s, 0), (rds_sink, 0))
            except Exception as e:
                # If anything fails, fall back silently (audio should still work).
                try:
                    debug_log(f"DEBUG: RDS(gnuradio) init failed: {type(e).__name__}: {e}")
                    debug_log(f"DEBUG: RDS(gnuradio) traceback:\n{traceback.format_exc()}")
                except Exception:
                    pass
                try:
                    self.log(f"RDS dbg: gnuradio backend init failed: {e}")
                except Exception:
                    pass
                try:
                    self._terminate_process(getattr(self, "_rds_proc", None), name="redsea")
                except Exception:
                    pass
                self._rds_proc = None
                try:
                    if getattr(self, "_rds_audio_pipe_file", None) is not None:
                        self._rds_audio_pipe_file.close()
                except Exception:
                    pass
                self._rds_audio_pipe_file = None
                try:
                    if getattr(self, "_rds_audio_pipe_w", None) is not None:
                        os.close(self._rds_audio_pipe_w)
                except Exception:
                    pass
                self._rds_audio_pipe_w = None
                self._rds_audio_pipe_r = None

        # float (-1..1) -> short (S16_LE)
        f2s_l = blocks.float_to_short(1, 32767.0)
        f2s_r = blocks.float_to_short(1, 32767.0)

        try:
            self._gr_blocks["f2s_l"] = f2s_l
            self._gr_blocks["f2s_r"] = f2s_r
        except Exception:
            pass

        inter = blocks.interleave(gr.sizeof_short)
        sink = blocks.file_descriptor_sink(gr.sizeof_short, audio_w_fd)

        try:
            self._gr_blocks["inter"] = inter
            self._gr_blocks["sink"] = sink
        except Exception:
            pass

        tb.connect((src, 0), (rx, 0))
        tb.connect((rx, 0), (f2s_l, 0))
        tb.connect((rx, 1), (f2s_r, 0))
        tb.connect((f2s_l, 0), (inter, 0))
        tb.connect((f2s_r, 0), (inter, 1))
        tb.connect((inter, 0), (sink, 0))

        tb.start()
        self.gr_tb = tb
        self.gr_src = src
        self._gr_pipe_file = os.fdopen(self._gr_pipe_r, 'rb', buffering=0)

        # If RDS is enabled, start the feeder thread that pushes samples into redsea.
        # (The JSON reader thread is started from play_station() after flags are set.)
        try:
            if rds_enabled:
                self._start_rds_feeder_thread()
        except Exception:
            pass


    def _spawn_redsea_proc(self):
        """(Re)start redsea for GNU Radio RDS. stdin expects raw int16 samples."""
        try:
            self._terminate_process(getattr(self, "_rds_proc", None), name="redsea")
        except Exception:
            pass
        self._rds_proc = None

        redsea_cmd = ['redsea', '-r', str(RDS_SAMPLE_RATE), '-E']
        try:
            proc = subprocess.Popen(
                redsea_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
        except Exception as e:
            try:
                self.log(f"RDS dbg: failed to start redsea: {e}")
            except Exception:
                pass
            return None

        self._rds_proc = proc
        try:
            self.log(f"RDS dbg: started redsea pid={proc.pid} cmd={' '.join(redsea_cmd)}")
        except Exception:
            pass

        # Ensure the JSON reader is running (it will wait for playback flags).
        try:
            self._start_rds_reader_thread()
        except Exception:
            pass
        return proc


    def _start_rds_feeder_thread(self):
        if getattr(self, "_rds_feeder_thread", None) is not None:
            return

        def _feeder():
            last_restart_ts = 0.0
            try:
                while not getattr(self, "_closing", False):
                    # Keep draining the pipe as long as the flowgraph exists to avoid pipe fill/blocked writers
                    # during stop/switching. Only *forward* to redsea when playing+rds_updating is true.
                    tb_running = getattr(self, "gr_tb", None) is not None
                    if not tb_running:
                        time.sleep(0.05)
                        continue

                    # Ensure pipe exists
                    pipe_f = getattr(self, "_rds_audio_pipe_file", None)
                    if pipe_f is None:
                        time.sleep(0.1)
                        continue

                    forward = bool(getattr(self, "playing", False) and getattr(self, "rds_updating", False))

                    # Ensure redsea is running
                    proc = getattr(self, "_rds_proc", None)
                    if (not forward) or proc is None or proc.poll() is not None or proc.stdin is None:
                        now = time.time()
                        if forward and (now - last_restart_ts >= 1.0):
                            last_restart_ts = now
                            proc = self._spawn_redsea_proc()
                        time.sleep(0.05)
                        # Even if we are not forwarding (not playing), still drain/discard below.

                    # Read samples from GNU Radio pipe and forward to redsea
                    try:
                        fd = pipe_f.fileno()
                        ready, _, _ = select.select([fd], [], [], 0.25)
                        if not ready:
                            continue
                        data = os.read(fd, 16384)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except Exception:
                        time.sleep(0.05)
                        continue

                    if not data:
                        time.sleep(0.01)
                        continue

                    # Discard if we're not currently forwarding.
                    if not forward:
                        continue

                    try:
                        proc.stdin.write(data)
                    except BrokenPipeError:
                        try:
                            self.log("RDS dbg: redsea stdin broken pipe; restarting")
                        except Exception:
                            pass
                        try:
                            self._terminate_process(proc, name="redsea")
                        except Exception:
                            pass
                        self._rds_proc = None
                        continue
                    except Exception:
                        continue
            finally:
                self._rds_feeder_thread = None

        self._rds_feeder_thread = threading.Thread(target=_feeder, daemon=True)
        self._rds_feeder_thread.start()

    def _start_rds_reader_thread(self):
        if getattr(self, "_rds_reader_thread", None) is not None:
            return

        def _reader():
            try:
                # Helpful one-time hint that live RDS is running.
                try:
                    self.log("RDS: backend=gnuradio (redsea) active")
                except Exception:
                    pass

                # Debug counters/heartbeat so it's obvious whether RDS data is flowing.
                last_output_ts = time.time()
                last_heartbeat_ts = 0.0
                lines_total = 0
                json_ok = 0
                json_err = 0
                first_keys_logged = 0

                # Keep a local copy to detect changes and avoid constant DB writes.
                last_ps = None
                last_rt = None
                last_rtplus = None

                # Wait briefly for playback flags to be set (play_station sets them after _start_gnuradio_rx).
                start_wait = time.time()
                while not getattr(self, "playing", False) and not getattr(self, "_closing", False):
                    if (time.time() - start_wait) >= 2.0:
                        break
                    time.sleep(0.05)

                # Main loop: attach to the current redsea proc when available.
                proc = None

                while getattr(self, "playing", False) and getattr(self, "rds_updating", False):
                    if proc is None or proc.poll() is not None or proc.stdout is None:
                        proc = getattr(self, "_rds_proc", None)
                        if proc is None or proc.stdout is None:
                            time.sleep(0.05)
                            continue

                    try:
                        rc = proc.poll()
                    except Exception:
                        rc = None

                    if rc is not None:
                        try:
                            self.log(f"RDS dbg: redsea exited rc={rc}")
                        except Exception:
                            pass
                        break

                    # Avoid blocking forever on read: use select heartbeat.
                    try:
                        fd = proc.stdout.fileno()
                        ready, _, _ = select.select([fd], [], [], 1.0)
                    except Exception:
                        ready = []

                    if not ready:
                        now = time.time()
                        # Log a heartbeat every ~10s if there is no output.
                        if (now - last_output_ts) >= 10.0 and (now - last_heartbeat_ts) >= 10.0:
                            try:
                                self.log(f"RDS dbg: no JSON output for {int(now - last_output_ts)}s (redsea running)")
                            except Exception:
                                pass
                            last_heartbeat_ts = now
                        continue

                    try:
                        line_b = proc.stdout.readline()
                    except Exception:
                        break
                    if not line_b:
                        try:
                            self.log("RDS dbg: redsea stdout closed")
                        except Exception:
                            pass
                        break

                    try:
                        line = line_b.decode('utf-8', errors='ignore').strip()
                    except Exception:
                        continue

                    last_output_ts = time.time()
                    lines_total += 1
                    try:
                        data = json.loads(line)
                    except Exception:
                        json_err += 1
                        now = time.time()
                        if (now - last_heartbeat_ts) >= 10.0:
                            try:
                                self.log(f"RDS dbg: JSON parse errors: {json_err} (lines={lines_total})")
                            except Exception:
                                pass
                            last_heartbeat_ts = now
                        continue

                    json_ok += 1

                    # One-time peek at available keys so we know what the decoder outputs.
                    if first_keys_logged < 2:
                        try:
                            keys = sorted(list(data.keys()))
                            keys_preview = ",".join(keys[:20]) + ("…" if len(keys) > 20 else "")
                            self.log(f"RDS dbg: keys=[{keys_preview}]")
                        except Exception:
                            pass
                        first_keys_logged += 1

                    now = time.time()
                    if (now - last_heartbeat_ts) >= 10.0:
                        try:
                            self.log(f"RDS dbg: lines={lines_total} json_ok={json_ok} json_err={json_err}")
                        except Exception:
                            pass
                        last_heartbeat_ts = now

                    st = getattr(self, "current_station", None)
                    if st is None:
                        continue

                    # Only react to useful updates.
                    interesting = False
                    if data.get('ps') or data.get('radiotext'):
                        interesting = True
                    for k in ('rtplus', 'radio_text_plus', 'radiotext_plus', 'radiotextplus', 'rt_plus'):
                        if data.get(k):
                            interesting = True
                            break
                    if any(k in data for k in ('prog_type', 'pi', 'di', 'tp', 'ta')):
                        interesting = True
                    if not interesting:
                        continue

                    try:
                        prev_ps = getattr(st, "ps", None)
                        prev_rt = getattr(st, "radiotext", None)
                        prev_rtp = getattr(st, "rtplus", None)
                        st.update_from_rds(data)
                        changed = (
                            getattr(st, "ps", None) != prev_ps
                            or getattr(st, "radiotext", None) != prev_rt
                            or getattr(st, "rtplus", None) != prev_rtp
                        )
                    except Exception:
                        continue

                    if not changed:
                        continue

                    # Log changes so it is obvious in the GUI that RDS is updating.
                    try:
                        parts = []
                        new_ps = getattr(st, "ps", None)
                        new_rt = getattr(st, "radiotext", None)
                        if new_ps and new_ps != prev_ps:
                            parts.append(f"PS={new_ps}")
                        if new_rt and new_rt != prev_rt:
                            rt_one_line = " ".join(str(new_rt).split())
                            if len(rt_one_line) > 140:
                                rt_one_line = rt_one_line[:140] + "…"
                            parts.append(f"RT={rt_one_line}")
                        if parts:
                            self.log("RDS: " + " | ".join(parts))
                    except Exception:
                        pass

                    # Update GUI (main thread)
                    try:
                        self.root.after(0, self.update_station_info, st)
                    except Exception:
                        pass

                    # Persist station DB, but throttle writes.
                    try:
                        now = time.time()
                        if now - float(getattr(self, "_rds_last_save_ts", 0.0)) >= 5.0:
                            self.db.add_or_update(st)
                            self.db.save()
                            self._rds_last_save_ts = now
                    except Exception:
                        pass

                    last_ps = getattr(st, "ps", None)
                    last_rt = getattr(st, "radiotext", None)
                    last_rtplus = getattr(st, "rtplus", None)
            finally:
                self._rds_reader_thread = None

        self._rds_reader_thread = threading.Thread(target=_reader, daemon=True)
        self._rds_reader_thread.start()

    def _stop_gnuradio_rx(self, block=False):
        """Stop GNU Radio RX and close the pipe.

        If block=False, do not block the GUI thread on tb.wait().
        """
        tb = self.gr_tb
        self.gr_tb = None
        self.gr_src = None

        stop_event = threading.Event()
        self._gr_stop_event = stop_event

        # We'll close write FDs after the flowgraph is stopped; otherwise
        # file_descriptor_sink may attempt to write to a closed FD.
        w_fd_to_close = self._gr_pipe_w
        rds_w_fd_to_close = getattr(self, "_rds_audio_pipe_w", None)
        self._gr_pipe_w = None
        self._rds_audio_pipe_w = None

        # Close the pipe first to unblock reads in stream_audio.
        if self._gr_pipe_file is not None:
            try:
                self._gr_pipe_file.close()
            except Exception:
                pass
            self._gr_pipe_file = None

        # Do NOT close the RDS read pipe here: if we close the read end before tb stops,
        # the GNU Radio file_descriptor_sink will get EPIPE and can abort the process.
        # The feeder thread will keep draining the pipe until tb is stopped.

        # Close the read end now (safe) to unblock stream_audio; keep write end
        # until GNU Radio is stopped.
        if self._gr_pipe_r is not None:
            try:
                os.close(self._gr_pipe_r)
            except Exception:
                pass
            self._gr_pipe_r = None

        if tb is None:
            stop_event.set()
            return stop_event

        # Stop redsea (if active)
        try:
            self._terminate_process(getattr(self, "_rds_proc", None), name="redsea")
        except Exception:
            pass
        self._rds_proc = None

        def _stop_wait_bg(tb_local):
            try:
                try:
                    tb_local.stop()
                except Exception:
                    pass
                try:
                    tb_local.wait()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                # Close write FD after tb is stopped to avoid sink errors.
                try:
                    if w_fd_to_close is not None:
                        os.close(w_fd_to_close)
                except Exception:
                    pass
                try:
                    if rds_w_fd_to_close is not None:
                        os.close(rds_w_fd_to_close)
                except Exception:
                    pass

                # Now it is safe to close the RDS read end/file.
                try:
                    if getattr(self, "_rds_audio_pipe_file", None) is not None:
                        self._rds_audio_pipe_file.close()
                except Exception:
                    pass
                self._rds_audio_pipe_file = None
                self._rds_audio_pipe_r = None
                try:
                    stop_event.set()
                except Exception:
                    pass

        if block:
            _stop_wait_bg(tb)
        else:
            threading.Thread(target=_stop_wait_bg, args=(tb,), daemon=True).start()

        return stop_event

    def _terminate_process(self, proc, name="proc", timeout_terminate=1.0, timeout_kill=0.5):
        """Terminate a subprocess without risking a GUI hang."""
        if proc is None:
            return

        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass

        # Prefer process group termination when possible (start_new_session=True)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=timeout_terminate)
            return
        except Exception:
            pass

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        try:
            proc.wait(timeout=timeout_kill)
        except Exception:
            pass
    
    def stop_playback(self, quiet=False):
        """Stop playback."""
        if not self.playing:
            return
        
        # FIRST flip flags so worker threads can stop
        self.playing = False
        self.spectrum_running = False
        self.rds_updating = False
        
        if not quiet:
            self.log(self.t("log_playback_stopped"))
            try:
                self.status_label.config(text=self.t("stopped"))
            except Exception:
                pass
        
        # Clear audio buffer
        with self.audio_lock:
            self.audio_buffer = []
        
        # Stop playback (sox/play) without risking a hang
        if self.play_proc:
            self._terminate_process(self.play_proc, name="play")
            self.play_proc = None
            
        # GNU Radio RX (do not block the GUI)
        self._stop_gnuradio_rx(block=False)
        self.rtl_proc = None
        
        # Clear plots
        if not quiet:
            try:
                ymin = float(getattr(self, 'spectrum_ymin_dbfs', -90.0))
                self.line_left.set_ydata(np.full(512, ymin))
                self.line_right.set_ydata(np.full(512, ymin))
                self.line_corr.set_data([], [])
                self.corr_text.set_text('')
                self.canvas.draw_idle()
            except Exception:
                pass
        
        self.current_station = None
        if not quiet:
            try:
                self.play_button.config(state=tk.NORMAL)
                self.stop_button.config(state=tk.DISABLED)
                self.scan_button.config(state=tk.NORMAL)
                self.record_start_button.config(state=tk.DISABLED)
                self.record_stop_button.config(state=tk.DISABLED)
            except Exception:
                pass
        
        # Stop recording if active
        if self.recording:
            self.stop_recording(quiet=quiet)
    
    def start_recording(self):
        """Start recording (stereo)."""
        debug_log("=" * 60)
        debug_log("DEBUG: start_recording() ROZPOCZĘCIE")
        debug_log(f"DEBUG: self.playing = {self.playing}")
        debug_log(f"DEBUG: self.current_station = {self.current_station}")
        debug_log(f"DEBUG: self.recording = {self.recording}")
        
        # FIRST ensure recording is possible — BEFORE disabling the button
        if not self.playing or not self.current_station:
            debug_log("DEBUG: NIE MOŻNA NAGRYWAĆ - brak odtwarzania lub stacji")
            messagebox.showwarning(self.t("warn"), self.t("need_playback_first"))
            return
        
        debug_log("DEBUG: Sprawdzenie odtwarzania OK - kontynuuję")

        # Pick encoder based on settings.
        rec_fmt = str(getattr(self, "recording_format", None) or (self.settings.get("recording", {}) or {}).get("format") or "mp3").strip().lower()
        if rec_fmt not in ("mp3", "flac"):
            rec_fmt = "mp3"
        encoder_tool = "flac" if rec_fmt == "flac" else "lame"
        # Ensure encoder exists before touching UI state.
        try:
            subprocess.run(["which", encoder_tool], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            messagebox.showerror(self.t("err"), self.t("missing_recording_encoder", tool=encoder_tool, format=rec_fmt.upper()))
            return
        
        # NOW disable the button to prevent repeated clicks
        debug_log("DEBUG: Wyłączam przycisk start...")
        self.record_start_button.config(state=tk.DISABLED)
        debug_log("DEBUG: Przycisk start WYŁĄCZONY")
        
        # IMPORTANT: disable the external rtl_fm RDS updater while recording!
        # With GNU Radio backend, RDS is from the same flowgraph (no second SDR client), so it can stay on.
        try:
            backend = str(getattr(self, "rds_backend", "rtl_fm"))
        except Exception:
            backend = "rtl_fm"
        if backend != "gnuradio":
            debug_log("DEBUG: Wyłączam RDS updater...")
            self.rds_updating = False
            debug_log(f"DEBUG: RDS updater wyłączony: rds_updating={self.rds_updating}")
        
        # Cancel the previous size timer if it exists (just in case)
        if self.record_size_updater:
            debug_log(f"DEBUG: Anuluję stary timer: {self.record_size_updater}")
            try:
                self.root.after_cancel(self.record_size_updater)
                debug_log("DEBUG: Timer anulowany SUKCES")
            except Exception as te:
                debug_log(f"DEBUG: Timer anulowany BŁĄD (ignoruję): {te}")
            self.record_size_updater = None
        else:
            debug_log("DEBUG: Brak starego timera do anulowania")
        
        # File name with timestamp
        debug_log("DEBUG: Generuję nazwę pliku...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        station_name = self.current_station.ps or f"{self.current_station.freq:.1f}MHz"
        debug_log(f"DEBUG: Oryginalna nazwa stacji: '{station_name}'")
        # Remove unsafe characters and trim spaces
        station_name = "".join(c for c in station_name if c.isalnum() or c in (' ', '-', '_')).strip()
        station_name = station_name.replace(' ', '_')  # replace spaces with underscores
        out_dir = getattr(self, "recordings_dir", os.path.join(BASE_DIR, "recordings"))
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        ext = "flac" if rec_fmt == "flac" else "mp3"
        prefix_raw = self.t("recording_file_prefix")
        prefix = str(prefix_raw or "recording")
        prefix = "".join(c for c in prefix if c.isalnum() or c in (' ', '-', '_')).strip()
        prefix = prefix.replace(' ', '_')
        if not prefix:
            prefix = "recording"

        filename = os.path.join(out_dir, f"{prefix}_{station_name}_{timestamp}.{ext}")
        display_name = os.path.basename(filename)
        debug_log(f"DEBUG: Wygenerowana nazwa pliku: '{filename}'")
        
        # Keep the UI log concise: show only the file name (directory is configured in Settings).
        self.log(self.t("recording_log", file=display_name))
        
        try:
            debug_log("DEBUG: ROZPOCZYNAM BLOK TRY dla subprocess...")
            # Start the encoder; it receives PCM from stream_audio()
            # PCM format: signed 16-bit little-endian, stereo, 48kHz
            if rec_fmt == "flac":
                enc_cmd = [
                    "flac",
                    "--silent",
                    "-8",
                    "--force-raw-format",
                    "--endian=little",
                    "--sign=signed",
                    "--channels=2",
                    "--bps=16",
                    "--sample-rate=48000",
                    "-o",
                    filename,
                    "-",
                ]
            else:
                enc_cmd = [
                    "lame",
                    "--quiet",
                    "-r",
                    "--signed",
                    "--little-endian",
                    "--bitwidth",
                    "16",
                    "-s",
                    "48",
                    "-m",
                    "j",
                    "--cbr",
                    "-b",
                    "192",
                    "-q",
                    "2",
                    "-",
                    filename,
                ]
            debug_log(f"DEBUG: Komenda encoder: {' '.join(enc_cmd)}")
            
            debug_log("DEBUG: Wywołuję subprocess.Popen()...")
            self.record_proc = subprocess.Popen(enc_cmd,
                                               stdin=subprocess.PIPE,
                                               stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL,
                                               start_new_session=True,
                                               bufsize=65536)
            debug_log(f"DEBUG: subprocess.Popen() SUKCES! PID={self.record_proc.pid}")
            debug_log(f"DEBUG: record_proc.poll() = {self.record_proc.poll()}")
            
            debug_log("DEBUG: Ustawiam self.recording = True...")
            self.recording = True
            debug_log(f"DEBUG: self.recording = {self.recording}")

            self.record_bytes_written = 0
            self.record_started_at = time.time()
            
            debug_log(f"DEBUG: Ustawiam self.record_filename = '{filename}'...")
            self.record_filename = filename
            debug_log(f"DEBUG: self.record_filename = '{self.record_filename}'")
            
            debug_log("DEBUG: Aktualizuję status label...")
            self.record_status_label.config(text=self.t("recording_log", file=display_name))
            debug_log("DEBUG: Status label zaktualizowany")
            
            # Enable the STOP button via after() to ensure the GUI refreshes.
            debug_log("DEBUG: Włączam przycisk STOP przez after()...")
            def enable_stop_button():
                debug_log("DEBUG: after() callback - włączam STOP button")
                self.record_stop_button.config(state=tk.NORMAL)
                debug_log("DEBUG: STOP button powinien być AKTYWNY!")
            
            self.root.after(1, enable_stop_button)  # Enable after 1ms
            debug_log("DEBUG: Zaplanowano włączenie przycisku STOP")
            
            # Start the file size update timer (1s delay).
            debug_log("DEBUG: Uruchamiam timer update_record_size (1000ms)...")
            self.record_size_updater = self.root.after(1000, self.update_record_size)
            debug_log(f"DEBUG: Timer uruchomiony: ID={self.record_size_updater}")
            
            debug_log("DEBUG: start_recording() ZAKOŃCZONE SUKCESEM")
            debug_log("=" * 60)
            
        except Exception as e:
            debug_log("DEBUG: WEJŚCIE DO BLOKU EXCEPT!")
            debug_log(f"DEBUG: Exception type: {type(e).__name__}")
            debug_log(f"DEBUG: Exception message: {e}")
            import traceback
            debug_log(f"DEBUG: Traceback:\n{traceback.format_exc()}")
            
            # On error, restore the button states.
            debug_log("DEBUG: Przywracam stan przycisków po błędzie...")
            self.record_start_button.config(state=tk.NORMAL)
            self.record_stop_button.config(state=tk.DISABLED)
            self.recording = False
            debug_log(f"DEBUG: Przyciski przywrócone: recording={self.recording}")
            self.log(self.t("log_record_error", e=e))
            messagebox.showerror(self.t("err"), self.t("cannot_start_recording", e=e))
            debug_log("=" * 60)
    
    def stop_recording(self, quiet=False):
        """Stop recording."""
        if not self.recording:
            return
        
        # FIRST: flip the flag.
        self.recording = False
        
        # Cancel the size timer.
        if self.record_size_updater:
            try:
                self.root.after_cancel(self.record_size_updater)
            except:
                pass
            self.record_size_updater = None
        
        if not quiet and not getattr(self, '_closing', False):
            self.log(self.t("recording_stopped"))
        
        # Re-enable the RDS updater if playback is running.
        if self.playing and not getattr(self, '_closing', False):
            self.rds_updating = True
        
        # MP3 FINALIZATION: do not kill LAME immediately.
        # LAME often writes proper headers/tags only after EOF on stdin.
        proc = self.record_proc
        filename = self.record_filename
        self.record_proc = None

        if proc:
            threading.Thread(
                target=self._finalize_recording_proc,
                args=(proc, filename),
                daemon=True
            ).start()
        
        # Update buttons.
        if not quiet and not getattr(self, '_closing', False):
            try:
                self.record_start_button.config(state=tk.NORMAL)    # Enable start
                self.record_stop_button.config(state=tk.DISABLED)   # Disable stop
            except Exception:
                pass
        
        # Do not use root.update() (it can hang Tk). The UI will refresh on its own.
        
        # The final status will be set by the finalize thread.
        self.record_filename = None

    def _finalize_recording_proc(self, proc, filename):
        """Close stdin and wait for the encoder to finalize the file (in background, without freezing the GUI)."""
        try:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception as e:
                debug_log(f"DEBUG: finalize: close stdin error: {e}")

            # Give the encoder a moment to finalize the file.
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                debug_log("DEBUG: finalize: encoder not finished in 3s, terminate()")
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    debug_log("DEBUG: finalize: kill()")
                    try:
                        proc.kill()
                    except Exception:
                        pass

            rc = proc.poll()
            debug_log(f"DEBUG: finalize: encoder exit code = {rc}")

        finally:
            # Update status in the GUI (main thread).
            def _update_done_label():
                try:
                    if filename and os.path.exists(filename):
                        display_name = os.path.basename(filename)
                        size = os.path.getsize(filename)
                        size_mb = size / (1024 * 1024)
                        self.record_status_label.config(text=self.t("record_saved", file=display_name, size_mb=size_mb))
                        self.log(self.t("record_file_saved", file=display_name, size_mb=size_mb))
                        self.root.after(5000, lambda: self.record_status_label.config(text=""))
                    else:
                        self.record_status_label.config(text="")
                except Exception:
                    pass

            try:
                self.root.after(0, _update_done_label)
            except Exception:
                pass
    
    def stream_audio(self):
        """Stream stereo audio to sox and buffer it for the spectrum analyzer."""
        try:
            # 1 stereo frame = 4 bytes (2x int16).
            # Slightly larger chunks reduce the chance of short underruns on startup.
            chunk_bytes = 16384
            
            while self.playing and self.play_proc:
                # Read from the GNU Radio pipe.
                audio_data = None
                if self._gr_pipe_file is not None:
                    try:
                        fd = self._gr_pipe_file.fileno()
                        ready, _, _ = select.select([fd], [], [], 0.25)
                        if ready:
                            audio_data = os.read(fd, chunk_bytes)
                        else:
                            audio_data = None
                    except (BlockingIOError, InterruptedError):
                        audio_data = None
                    except Exception:
                        audio_data = None

                if not audio_data:
                    continue

                # Align to stereo frames (4 bytes). If misaligned, drop trailing bytes.
                if len(audio_data) % 4 != 0:
                    audio_data = audio_data[:len(audio_data) - (len(audio_data) % 4)]
                    if not audio_data:
                        continue

                # Per-app software volume for playback only.
                # Recording should stay unscaled (so it is not affected by the listening volume).
                play_data = audio_data
                try:
                    vol = float(getattr(self, 'volume', 100)) / 100.0
                except Exception:
                    vol = 1.0

                if vol <= 0.0:
                    play_data = b"\x00" * len(audio_data)
                elif vol < 0.999:
                    try:
                        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                        samples *= vol
                        np.clip(samples, -32768.0, 32767.0, out=samples)
                        play_data = samples.astype(np.int16).tobytes()
                    except Exception:
                        play_data = audio_data
                
                # Send to sox (playback) — priority path.
                if self.play_proc and self.play_proc.stdin:
                    try:
                        self.play_proc.stdin.write(play_data)
                    except BrokenPipeError:
                        break
                    except Exception as e:
                        break
                
                # If recording, also send to the encoder.
                if self.recording and self.record_proc and self.record_proc.stdin:
                    # If encoder died, stop recording (but do not stop playback).
                    if self.record_proc.poll() is not None:
                        debug_log(f"DEBUG: Encoder died during recording, rc={self.record_proc.poll()}")
                        self.recording = False
                        self.root.after(0, lambda: self.record_stop_button.config(state=tk.DISABLED))
                        self.root.after(0, lambda: self.record_start_button.config(state=tk.NORMAL))
                    else:
                        try:
                            self.record_proc.stdin.write(audio_data)
                            self.record_bytes_written += len(audio_data)
                        except BrokenPipeError as e:
                            debug_log(f"DEBUG: BrokenPipe to encoder: {e}")
                            self.recording = False
                            self.root.after(0, lambda: self.record_stop_button.config(state=tk.DISABLED))
                            self.root.after(0, lambda: self.record_start_button.config(state=tk.NORMAL))
                        except Exception as e:
                            debug_log(f"DEBUG: write to encoder error: {e}")
                            # Do not interrupt playback.
                            self.recording = False
                            self.root.after(0, lambda: self.record_stop_button.config(state=tk.DISABLED))
                            self.root.after(0, lambda: self.record_start_button.config(state=tk.NORMAL))
                
                # Add to spectrum buffer (with lock and limit).
                if self.spectrum_running:
                    with self.audio_lock:
                        self.audio_buffer.append(audio_data)
                        # Limit buffer to max 10 chunks.
                        if len(self.audio_buffer) > 10:
                            self.audio_buffer = self.audio_buffer[-10:]
                        
        except Exception as e:
            self.log(self.t("log_stream_error", e=e))
        finally:
            pass
    
    def rds_updater(self):
        """RDS update thread during playback."""
        try:
            while self.rds_updating and self.current_station:
                # Fetch fresh RDS data every N seconds.
                time.sleep(int(getattr(self, "rds_interval_s", 30)))
                
                if not self.rds_updating or not self.current_station:
                    break
                
                # IMPORTANT: do not start a second rtl_fm while recording.
                if self.recording:
                    continue
                
                freq = self.current_station.freq
                
                try:
                    # Run rtl_fm + redsea briefly (10s).
                    rtl_cmd = ['rtl_fm', '-f', f'{freq}M', '-s', '171k', 
                              '-g', str(self.gain), '-']
                    redsea_cmd = ['redsea', '-r', '171000', '-E']
                    
                    rtl_proc = subprocess.Popen(rtl_cmd,
                                               stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL,
                                               bufsize=0)
                    
                    redsea_proc = subprocess.Popen(redsea_cmd,
                                                  stdin=rtl_proc.stdout,
                                                  stdout=subprocess.PIPE,
                                                  stderr=subprocess.DEVNULL,
                                                  bufsize=1,
                                                  text=True)
                    
                    rtl_proc.stdout.close()
                    
                    # Czytaj przez 10 sekund
                    start_time = time.time()
                    rds_found = False
                    
                    while time.time() - start_time < 10 and self.rds_updating:
                        ready = select.select([redsea_proc.stdout], [], [], 0.5)
                        if ready[0]:
                            line = redsea_proc.stdout.readline()
                            if line:
                                try:
                                    data = json.loads(line)

                                    # Update station data through a single path to keep fields consistent.
                                    interesting = False
                                    if data.get('ps'):
                                        interesting = True
                                    if data.get('radiotext'):
                                        interesting = True
                                    for k in ('rtplus', 'radio_text_plus', 'radiotext_plus', 'radiotextplus', 'rt_plus'):
                                        if data.get(k):
                                            interesting = True
                                            break

                                    if 'prog_type' in data or 'pi' in data or 'di' in data or 'tp' in data or 'ta' in data:
                                        interesting = True

                                    if interesting:
                                        self.current_station.update_from_rds(data)
                                        rds_found = True

                                        # Update GUI + DB.
                                        self.root.after(0, self.update_station_info, self.current_station)
                                        self.db.add_or_update(self.current_station)
                                        self.db.save()
                                except:
                                    pass
                    
                    # Cleanup
                    redsea_proc.kill()
                    rtl_proc.kill()
                    
                    if rds_found:
                        self.log(self.t("log_rds_updated", ps=self.current_station.ps))
                    
                except Exception as e:
                    pass
                    
        except Exception as e:
            pass
    
    def spectrum_analyzer(self):
        """Dedicated thread for audio spectrum analysis."""
        try:
            # Blackman window for better frequency separation.
            window = np.blackman(1024)
            nfft = 1024
            # Window coherent gain: used for a sensible amplitude scale.
            coherent_gain = float(np.sum(window) / nfft)
            
            while self.spectrum_running:
                try:
                    audio_chunks = None
                    
                    # Pull data from the buffer (thread-safe).
                    with self.audio_lock:
                        if len(self.audio_buffer) >= 2:
                            audio_chunks = self.audio_buffer[:2]
                            self.audio_buffer = self.audio_buffer[2:]
                    
                    # If no data, wait.
                    if audio_chunks is None:
                        time.sleep(0.02)
                        continue
                    
                    # Join chunks.
                    audio_data = b''.join(audio_chunks)
                    
                    # Convert to numpy (stereo interleaved S16).
                    samples = np.frombuffer(audio_data, dtype=np.int16)

                    # stereo: [L0, R0, L1, R1, ...]
                    if len(samples) >= 2048:
                        stereo = samples[:2048].reshape(-1, 2)
                        left = stereo[:, 0].astype(np.float32) / 32768.0
                        right = stereo[:, 1].astype(np.float32) / 32768.0

                        left = left - float(np.mean(left))
                        right = right - float(np.mean(right))

                        wl = left[:nfft] * window
                        wr = right[:nfft] * window

                        fft_l = np.fft.rfft(wl, n=nfft)
                        fft_r = np.fft.rfft(wr, n=nfft)
                        mag_l = np.abs(fft_l[:512])
                        mag_r = np.abs(fft_r[:512])

                        # dBFS scale:
                        # for a sine with amplitude 1.0 in time domain, |FFT| ~ coherent_gain * (N/2)
                        # => amp ~= |FFT| / (coherent_gain * (N/2))
                        ref = coherent_gain * (nfft / 2.0)
                        amp_l = mag_l / (ref + 1e-12)
                        amp_r = mag_r / (ref + 1e-12)
                        dbfs_l = 20.0 * np.log10(amp_l + 1e-12)
                        dbfs_r = 20.0 * np.log10(amp_r + 1e-12)

                        # Frequency smoothing: 0=off, 1=light, 2..=stronger.
                        def _smooth_freq(vec, bins):
                            if bins <= 0:
                                return vec
                            sm = vec.astype(np.float32, copy=True)
                            for _ in range(int(bins)):
                                sm[1:-1] = 0.25 * sm[:-2] + 0.5 * sm[1:-1] + 0.25 * sm[2:]
                            return sm

                        bins = int(getattr(self, 'spec_freq_smooth_bins', 1))
                        disp_l = _smooth_freq(dbfs_l, bins)
                        disp_r = _smooth_freq(dbfs_r, bins)

                        alpha = float(getattr(self, 'spec_time_alpha', 0.25))
                        # Time smoothing, per-channel.
                        if not hasattr(self, '_spec_smooth_l'):
                            ymin_init = float(getattr(self, 'spectrum_ymin_dbfs', -90.0))
                            self._spec_smooth_l = np.full(512, ymin_init, dtype=np.float32)
                            self._spec_smooth_r = np.full(512, ymin_init, dtype=np.float32)
                        self._spec_smooth_l = alpha * disp_l + (1.0 - alpha) * self._spec_smooth_l
                        self._spec_smooth_r = alpha * disp_r + (1.0 - alpha) * self._spec_smooth_r

                        ymin = float(getattr(self, 'spectrum_ymin_dbfs', -90.0))
                        ymax = float(getattr(self, 'spectrum_ymax_dbfs', 0.0))
                        if ymax > 0.0:
                            ymax = 0.0
                        clipped_l = np.clip(self._spec_smooth_l, ymin, ymax)
                        clipped_r = np.clip(self._spec_smooth_r, ymin, ymax)

                        # Correlation and balance.
                        rms_l = float(np.sqrt(np.mean(left * left) + 1e-12))
                        rms_r = float(np.sqrt(np.mean(right * right) + 1e-12))
                        bal_db = 20.0 * np.log10((rms_l + 1e-12) / (rms_r + 1e-12))

                        # Correlation faster than np.corrcoef():
                        # left/right are already mean-removed, so corr = E[L*R]/(stdL*stdR)
                        if rms_l < 1e-6 or rms_r < 1e-6:
                            corr = 0.0
                        else:
                            corr = float(np.mean(left * right) / (rms_l * rms_r))
                            if corr > 1.0:
                                corr = 1.0
                            elif corr < -1.0:
                                corr = -1.0

                        # Plot points (subsample).
                        corr_points = int(getattr(self, '_corr_points', 256))
                        step = max(1, int(len(left) / corr_points))
                        corr_x = left[::step][:corr_points]
                        corr_y = right[::step][:corr_points]

                        # Store the latest frame and schedule a single GUI redraw.
                        # This prevents the Tk event queue from filling up when rendering is slower than computation.
                        self._spec_plot_latest = (clipped_l, clipped_r, corr_x, corr_y, corr, bal_db)
                        self._request_spectrum_plot_update()
                    
                    fps = int(getattr(self, 'spec_fps', 66))
                    time.sleep(max(0.005, 1.0 / float(max(1, fps))))
                    
                except Exception as e:
                    time.sleep(0.02)
                    pass
                
        except Exception as e:
            self.log(self.t("log_spectrum_error", e=e))
    
    def stop_spectrum_analyzer(self):
        """Stop the spectrum analyzer."""
        self.spectrum_running = False
        # Clear plots.
        try:
            self.line_left.set_ydata(np.full(512, -90.0))
            self.line_right.set_ydata(np.full(512, -90.0))
            self.line_corr.set_data([], [])
            self.corr_text.set_text('')
            self.canvas.draw()
        except Exception:
            pass
    
    def update_spectrum_plot(self, mag_left, mag_right, corr_x=None, corr_y=None, corr=None, bal_db=None):
        """Update plots (called from the main thread)."""
        try:
            self.line_left.set_ydata(mag_left)
            self.line_right.set_ydata(mag_right)

            if corr_x is not None and corr_y is not None:
                self.line_corr.set_data(corr_x, corr_y)
                if corr is not None and bal_db is not None:
                    self.corr_text.set_text(f"corr: {corr:+.2f} | balans L/R: {bal_db:+.1f} dB")

            self.canvas.draw_idle()
        except:
            pass

    def _request_spectrum_plot_update(self):
        """Coalesce spectrum/correlation redraw requests into a single pending UI callback."""
        try:
            if getattr(self, '_spec_plot_pending', False):
                return
            self._spec_plot_pending = True
            self.root.after(0, self._flush_spectrum_plot_update)
        except Exception:
            self._spec_plot_pending = False

    def _flush_spectrum_plot_update(self):
        """Redraw plots at most at the configured FPS, dropping intermediate frames if needed."""
        try:
            self._spec_plot_pending = False

            if not getattr(self, 'spectrum_running', False):
                return
            if not hasattr(self, 'canvas') or self.canvas is None:
                return

            fps = int(getattr(self, 'spec_fps', 66))
            fps = int(max(1, min(120, fps)))
            min_dt = 1.0 / float(fps)

            now = time.time()
            last = float(getattr(self, '_spec_plot_last_draw_ts', 0.0))
            dt = now - last
            if dt < min_dt:
                delay_ms = int(max(1, (min_dt - dt) * 1000.0))
                # Keep one pending callback; delay to honor FPS.
                if not getattr(self, '_spec_plot_pending', False):
                    self._spec_plot_pending = True
                    self.root.after(delay_ms, self._flush_spectrum_plot_update)
                return

            payload = getattr(self, '_spec_plot_latest', None)
            if not payload:
                return

            self._spec_plot_last_draw_ts = now
            self.update_spectrum_plot(*payload)

            # If a newer frame arrived during the draw, schedule another flush.
            if getattr(self, '_spec_plot_latest', None) is not payload:
                self._request_spectrum_plot_update()
        except Exception:
            try:
                self._spec_plot_pending = False
            except Exception:
                pass
    
    
    def update_record_size(self):
        """Update the recorded file size."""
        # Check if we are still recording (may have been stopped in the meantime).
        if not self.recording or not self.record_filename:
            return
        
        try:
            size_bytes = 0
            if os.path.exists(self.record_filename):
                size_bytes = os.path.getsize(self.record_filename)
            size_mb = size_bytes / (1024 * 1024)
            mb_in = self.record_bytes_written / (1024 * 1024)
            display_name = os.path.basename(self.record_filename)
            self.record_status_label.config(
                text=self.t(
                    "recording_status",
                    file=display_name,
                    size_mb=size_mb,
                    mb_in=mb_in,
                )
            )
        except:
            pass
        
        # Schedule the next update in 1 second ONLY if we are still recording.
        if self.recording:
            self.record_size_updater = self.root.after(1000, self.update_record_size)
    
    def start_scan(self):
        """Start scanning in a background thread."""
        if self.scanning:
            messagebox.showinfo(self.t("info"), self.t("scan_already"))
            return
        
        if self.playing:
            self.stop_playback()
        
        # Run scanning in a background thread.
        scan_thread = threading.Thread(target=self.scan_fm_band, daemon=True)
        scan_thread.start()
    
    def scan_fm_band(self):
        """Scan the FM band (runs in a background thread)."""
        if getattr(self, '_closing', False):
            return

        self.scanning = True
        try:
            self.root.after(0, lambda: self.scan_button.config(state=tk.DISABLED))
            self.root.after(0, lambda: self.play_button.config(state=tk.DISABLED))
        except Exception:
            pass

        self.log(self.t("log_scan_start", min=self.fm_min_mhz, max=self.fm_max_mhz))
        try:
            self.root.after(0, lambda: self.status_label.config(text=self.t("scanning")))
        except Exception:
            pass
        
        freq_khz = int(getattr(self, "fm_min_khz", int(round(FM_START * 1000.0))))
        max_khz = int(getattr(self, "fm_max_khz", int(round(FM_END * 1000.0))))
        step_khz = int(getattr(self, "fm_step_khz", int(round(FM_STEP * 1000.0))))
        if step_khz <= 0:
            step_khz = 100

        total_freqs = int((max_khz - freq_khz) / step_khz) + 1
        scanned = 0
        found = 0
        
        try:
            while freq_khz <= max_khz and self.scanning and not getattr(self, '_closing', False):
                scanned += 1
                progress = (scanned / total_freqs) * 100

                freq = float(freq_khz) / 1000.0

                try:
                    f = float(freq)
                    p = float(progress)
                    self.root.after(0, lambda f=f, p=p: self.status_label.config(text=self.t("scanning_progress", freq=f, progress=p)))
                except Exception:
                    pass
                self.log(self.t("log_scan_step", scanned=scanned, total=total_freqs, freq=freq))
                
                station = self.scan_frequency_for_rds(freq)
                if station and station.ps:
                    self.db.add_or_update(station)
                    found += 1
                    self.log(self.t("log_scan_found", ps=station.ps))
                    self.root.after(0, self.update_station_list)
                
                freq_khz += step_khz
                
        except Exception as e:
            self.log(self.t("log_scan_error", e=e))
        
        self.db.save()
        self.scanning = False
        try:
            self.root.after(0, lambda: self.scan_button.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.play_button.config(state=tk.NORMAL))
        except Exception:
            pass

        self.log(self.t("log_scan_done", found=found))
        try:
            self.root.after(0, lambda found=found: self.status_label.config(text=self.t("scan_done", found=found)))
        except Exception:
            pass
        self.root.after(0, self.update_station_list)
    
    def scan_frequency_for_rds(self, freq):
        """Scan a single frequency."""
        station = FMStation(freq)
        rtl_proc = None
        redsea_proc = None
        
        try:
            rtl_cmd = ['rtl_fm', '-f', f'{freq}M', '-s', f'{RDS_SAMPLE_RATE}', 
                       '-g', str(RTL_GAIN), '-']
            redsea_cmd = ['redsea', '-r', str(RDS_SAMPLE_RATE), '-E']
            
            rtl_proc = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, 
                                        stderr=subprocess.DEVNULL, bufsize=0)
            redsea_proc = subprocess.Popen(redsea_cmd, stdin=rtl_proc.stdout,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL,
                                           text=True, bufsize=1)
            rtl_proc.stdout.close()
            
            start_time = time.time()
            rds_found = False
            
            while time.time() - start_time < SCAN_TIME and not getattr(self, '_closing', False) and self.scanning:
                ready, _, _ = select.select([redsea_proc.stdout], [], [], 0.1)
                
                if ready:
                    try:
                        line = redsea_proc.stdout.readline()
                        if not line:
                            break
                        
                        try:
                            rds_data = json.loads(line.strip())
                            station.update_from_rds(rds_data)
                            rds_found = True
                        except json.JSONDecodeError:
                            continue
                    except Exception:
                        break
            
            return station if rds_found else None
            
        except Exception as e:
            self.log(self.t("log_scan_freq_error", e=e))
            return None
        finally:
            if redsea_proc:
                try:
                    redsea_proc.kill()
                    redsea_proc.wait(timeout=0.5)
                except:
                    pass
            if rtl_proc:
                try:
                    rtl_proc.kill()
                    rtl_proc.wait(timeout=0.5)
                except:
                    pass
    
    def on_closing(self):
        """Window close handler."""
        if getattr(self, '_closing', False):
            return
        self._closing = True

        # Close the settings window if open.
        try:
            if hasattr(self, "_settings_win") and self._settings_win is not None:
                self._settings_win.destroy()
        except Exception:
            pass
        self._settings_win = None

        # Stop everything without blocking the GUI.
        try:
            self.scanning = False
        except Exception:
            pass

        try:
            if self.playing:
                self.stop_playback(quiet=True)
        except Exception:
            pass

        # Safety: terminate recording/playback processes if still running.
        try:
            if getattr(self, 'record_proc', None):
                self._terminate_process(self.record_proc, name="lame")
                self.record_proc = None
        except Exception:
            pass

        try:
            if getattr(self, 'play_proc', None):
                self._terminate_process(self.play_proc, name="play")
                self.play_proc = None
        except Exception:
            pass

        try:
            self._stop_gnuradio_rx(block=False)
        except Exception:
            pass

        # Destroy the window immediately (no modal dialogs, no waiting).
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass


def main():
    # Check external tools.
    # Only the audio output tool is a hard requirement to start the GUI.
    import shutil

    hard_required = ["play"]
    optional = ["redsea", "rtl_fm"]

    missing_hard = [t for t in hard_required if shutil.which(t) is None]
    missing_optional = [t for t in optional if shutil.which(t) is None]

    if missing_hard:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Error",
            "Missing required tool(s): " + ", ".join(missing_hard) + "\n\n"
            "Install SoX (play). On Debian/Ubuntu: sudo apt install sox",
        )
        root.destroy()
        return

    if missing_optional:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Warning",
            "Some optional tools are missing: " + ", ".join(missing_optional) + "\n\n"
            "- Without redsea: RDS decoding will be unavailable.\n"
            "- Without rtl_fm: legacy external RDS backend / some scan modes will be unavailable.\n\n"
            "You can still start the app.",
        )
        root.destroy()
    
    # Uruchom GUI
    root = tk.Tk()
    app = FMRadioGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
