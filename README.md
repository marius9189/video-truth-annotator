# Video Truth Annotator

A lean, keyboard-driven Python tool for **frame-by-frame annotation of trampoline videos** — built to create **video ground-truth data for flight-time (time-of-flight, ToF) measurement**.

It marks events such as **takeoff** (athlete leaves the trampoline bed), **landing** (feet touch the bed again), **LED on and LED off** (e.g. for sensor-board/video time synchronisation) and automatically writes them to a CSV file. The frame-accurate takeoff/landing pairs give you the reference flight times of every jump against which a ToF measurement system can be validated.

Developed for the [**OpenToF**](https://github.com/nrother/OpenToF/) project — an open source Time-of-Flight measurement system for trampolining — but fully usable as a standalone tool for any frame-accurate annotation task.

Built entirely on **OpenCV** – no GUI frameworks, no other dependencies.

## Features

- **Frame-accurate annotation**: every video frame can be addressed individually (arrow keys), regardless of the playback mode
- **Four event types**: Takeoff (`o`), Landing (`l`), LED on (`t`), LED off (`f`)
- **Resume existing annotations**: an already existing `<video>_events.csv` is loaded at startup and annotation continues seamlessly — nothing is ever lost by re-opening a video
- **Metadata check before loading**: the CSV metadata (video file, FPS, total frames) is validated against the video; on a mismatch the existing CSV is never touched
- **Two playback modes**:
  - **ECHTZEIT** ("real time") – playback is never slower than real time; if decoding falls behind, the player automatically jumps to the target frame (no cumulative drift)
  - **SKIP n** – every n-th frame is displayed (1–10× playback speed) for quickly reviewing long recording sessions
- **Interactive timeline** (progress bar at the bottom):
  - Click jumps to the position, click + drag scrubs through the video (pauses automatically)
  - **Yellow ticks** = annotated frames
  - **Red ticks** = illogical sequence (two takeoffs or two landings directly in a row)
- **Plausibility check**: takeoff and landing must strictly alternate — every jump is exactly one flight phase — violations are reported live in the console and shown in red in the timeline
- **Automatic, atomic saving**: after every change the data is written to the events CSV (first `.tmp`, then `os.replace()`) – **no data loss on crash**, never half-finished CSV files
- **Writes only on change**: the CSV is rewritten only when annotations actually changed; opening and closing a video without changes leaves all files untouched
- **Freely resizable window** (`WINDOW_NORMAL`); 4K videos are scaled down to max. 1280×720 at startup (aspect ratio is preserved)
- **Caps Lock friendly**: letter keys work regardless of Caps Lock state (uppercase input is normalised)

## Installation

Only Python 3 with OpenCV is required:

```bash
pip install opencv-python
```

## Usage

```bash
python video_truth_annotator.py <path/to/video.mp4>
```

Without an argument the usage is printed to the console. If the video has no frame rate, 30 fps is assumed (console warning).

For flight-time validation, a high frame rate matters: record at the highest rate your camera supports (e.g. 240 fps) so that the frame quantisation error of the takeoff/landing timestamps stays well below the tolerance you want to validate against.

## Resuming Annotations & Metadata Check

When you open a video that already has an `<video>_events.csv` next to it, the tool behaves as follows:

1. **Metadata match** (video file, FPS, total frames) → the existing annotations are loaded and annotation continues where you left off (`Loaded: … events, annotation continues`).
2. **Metadata mismatch** (e.g. the CSV belongs to a different video, a re-encoded version, or a different frame rate) → an error message with the differing fields is printed to the console. The existing CSV is **never overwritten**; instead a new CSV with a suffix is written (`video_events_1.csv`, `video_events_2.csv`, …). Autosave, manual save and the final save on exit all target that same new file, so the original stays untouched.
3. **Unreadable CSV** (corrupt file/encoding) → same behaviour as a mismatch: error message, old file kept, new suffixed CSV.

Notes:

- Fields the video itself does not report (unknown frame rate or frame count) are skipped in the check — a comparison against the fallback value (30 fps) would produce false conflicts.
- An **empty CSV** (valid metadata, no events yet) is loaded normally.
- Negative frame indices in the CSV are rejected with a console warning; the timestamp column is checked against `frame / fps` (tolerance 1 ms) — the **frame index is the source of truth**, a mismatching timestamp is only reported, not fatal.

## Keyboard Controls

| Key | Function |
|---|---|
| **Space** | Play / Pause |
| **Right arrow / `d`** | One frame forward |
| **Left arrow / `a`** | One frame back |
| **`o`** | Mark takeoff (feet leave the bed) |
| **`l`** | Mark landing (feet touch the bed) |
| **`t`** | Mark LED on |
| **`f`** | Mark LED off |
| **`u`** | Delete the annotation of the current frame |
| **`s`** | Manually save the CSV (with status output) |
| **`m`** | Toggle playback mode (ECHTZEIT ↔ SKIP) |
| **`1`–`9`, `0`** | Show every n-th frame (1–10), switches to SKIP |
| **`q` / `ESC`** | Quit (saves automatically) |

The playback mode can also be selected with the **slider below the video** (trackbar): position 0 = real time, position 1–10 = skip. When quitting via the window's X button or `q`/`ESC`, the data is saved one final time — but **only if annotations actually changed** during the session (`Finished. No changes, CSV not rewritten.` otherwise).

## CSV Output

The annotations are written to `<video>_events.csv` (semicolon-separated) next to the video:

```csv
# Video: meinvideo.mp4
# FPS: 30.000000
# Frame-Dauer: 33.333 ms        (frame duration)
# Gesamtframes: 900             (total frames)
# Video-Dauer: 30.000 s         (video duration)
# Erstellt: 2026-09-25T17:52:34 (created)
Frame;Timestamp;Event
0;0,000;LED on
120;4,000;Takeoff
450;15,000;Landing
```

- Metadata is written as comment lines at the top of the file and is cleanly skipped by pandas with `comment='#'`
- Timestamps are in seconds with a **German decimal separator (comma)**, matching the format of the truth CSVs
- Every timestamp is exactly reproducible: `timestamp = frame / fps`
- Flight time per jump is simply the difference between a landing and the preceding takeoff
- If the file is locked while saving (e.g. opened in Excel), a rescue copy is kept in `<video>_events.csv.tmp`

Load the annotations in Python:

```python
import pandas as pd

df = pd.read_csv("meinvideo_events.csv", sep=";", comment="#")
```

## Plausibility Check (Takeoff/Landing)

Takeoff and landing must **strictly alternate** — each jump consists of exactly one takeoff and one landing. If one takeoff is followed by another takeoff (or a landing by another landing), **both** annotations are marked as invalid – red in the timeline and as a warning in the console. LED events do not disturb the sequence and are not checked.

## Notes

- Closing the window (X button) exits the program cleanly and performs a final save if there were changes – even with an empty annotation list
- If the total frame count of the video is unknown, the program warns; the timeline scaling may then be inaccurate
- At the end of the video, playback pauses automatically and jumps back to the start
- Text overlays use OpenCV's built-in fonts, so German UI strings are written without umlauts

## Troubleshooting

| Problem | Solution |
|---|---|
| Command runs without any output, no window opens | The script file is incomplete — it must end with `if __name__ == "__main__": main()` (re-copy the full file) |
| `Warning: unknown frame rate, assuming 30 fps` | The container reports no frame rate; the tool assumes 30 fps — convert the video (e.g. `ffmpeg -i in.mp4 -vf fps=240 -crf 18 out.mp4`) for exact timestamps |
| Variable frame rate (VFR) footage | Many smartphones record VFR; the tool assumes a constant rate. Convert with FFmpeg first (see above) |
| `WARNING: '…' is locked (opened in Excel?)` | Close the CSV in Excel; a rescue copy is kept in `<video>_events.csv.tmp` |
| `ERROR: metadata of CSV … does not match the video` | The existing CSV was written for a different video/version — a new `_1` CSV is created automatically; sort out the old file manually if needed |
| Arrow keys do nothing | Use the alternative keys `a` / `d` |

## German Terms Used in the Code

The code and its user interface are written in German. The following translations apply:

| German term (in code / UI) | English meaning |
|---|---|
| ECHTZEIT | Real time (playback mode) |
| SKIP n / jeder n-te Frame | Skip / show every n-th frame |
| Wiedergabemodus | Playback mode |
| Fortschrittsleiste / Timeline | Progress bar / timeline |
| Pfeil rechts / Pfeil links | Right arrow / left arrow |
| Leertaste | Space bar |
| Ein Frame vor / zurück | One frame forward / back |
| Markieren / Markierung | Mark / annotation |
| Löschen | Delete |
| Speichern / Gespeichert | Save / saved |
| Manuell gespeichert | Manually saved |
| Beenden | Quit / exit |
| Warnung | Warning |
| Unlogische Sequenz | Illogical sequence |
| zwei Takeoffs oder zwei Landungen hintereinander | two takeoffs or two landings in a row |
| Fenster geschlossen | Window closed |
| Kein Frame mehr lesbar, beende | No more frames readable, quitting |
| Erstellt | Created |
| Video-Dauer | Video duration |
| Gesamtframes | Total frames |
| Frame-Dauer | Frame duration |
| Rettungskopie | Rescue copy |
| Überschrieben | Overwritten |
| Verwendung | Usage |
| Warnung: unbekannte Framerate, nehme 30 fps an | Warning: unknown frame rate, assuming 30 fps |
| zwischengespeichert | auto-saved (intermediate save) |
| Frame-Anzahl unbekannt, Timeline-Skalierung ggf. ungenau | Frame count unknown, timeline scaling may be inaccurate |

## Related Project

This annotator creates the ground-truth data for [**OpenToF**](https://github.com/nrother/OpenToF/) — an open source Time-of-Flight measurement system for trampolining that measures the flight time of every jump and aims to match the accuracy of competition ToF systems. The annotated takeoff/landing CSVs serve as the reference against which the OpenToF sensor data is validated.

## License

See [LICENSE](LICENSE) in the repository.
