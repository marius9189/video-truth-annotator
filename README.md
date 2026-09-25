# Video Truth Annotator

A lean, keyboard-driven Python tool for **frame-by-frame annotation of trampoline videos** — built to create **video ground-truth data for flight-time (time-of-flight, ToF) measurement**.

It marks events such as **takeoff** (athlete leaves the trampoline bed), **landing** (feet touch the bed again), **LED on and LED off** (e.g. for sensor-board/video time synchronisation) and automatically writes them to a CSV file. The frame-accurate takeoff/landing pairs give you the reference flight times of every jump against which a ToF measurement system can be validated.

Developed for the [**OpenToF**](https://github.com/nrother/OpenToF/) project — an open source Time-of-Flight measurement system for trampolining — but fully usable as a standalone tool for any frame-accurate annotation task.

Built entirely on **OpenCV** – no GUI frameworks, no other dependencies.

## Features

- **Frame-accurate annotation**: every video frame can be addressed individually (arrow keys), regardless of the playback mode 
- **Four event types**: Takeoff (`o`), Landing (`l`), LED on (`t`), LED off (`f`)
- **Two playback modes**:
  - **ECHTZEIT** ("real time") – playback is never slower than real time; if decoding falls behind, the player automatically jumps to the target frame (no cumulative drift)
  - **SKIP n** – every n-th frame is displayed (1–10× playback speed) for quickly reviewing long recording sessions
- **Interactive timeline** (progress bar at the bottom):
  - Click jumps to the position, click + drag scrubs through the video (pauses automatically)
  - **Yellow ticks** = annotated frames
  - **Red ticks** = illogical sequence (two takeoffs or two landings directly in a row)
- **Plausibility check**: takeoff and landing must strictly alternate — every jump is exactly one flight phase — violations are reported live in the console and shown in red in the timeline
- **Automatic, atomic saving**: after every change the data is written to `<video>_events.csv` (first `.tmp`, then `os.replace()`) – **no data loss on crash**, never half-finished CSV files

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

The playback mode can also be selected with the **slider below the video** (trackbar): position 0 = real time, position 1–10 = skip. When quitting via the window's X button or `q`/`ESC`, the data is always saved one final time.

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
- If the file is locked while saving (e.g. opened in Excel), a rescue copy is kept in `<video>_events.csv.tmp`

Load the annotations in Python:

```python
import pandas as pd

df = pd.read_csv("meinvideo_events.csv", sep=";", comment="#")
```

## Plausibility Check (Takeoff/Landing)

Takeoff and landing must **strictly alternate** — each jump consists of exactly one takeoff and one landing. If one takeoff is followed by another takeoff (or a landing by another landing), **both** annotations are marked as invalid – red in the timeline and as a warning in the console. LED events do not disturb the sequence and are not checked.

## Notes

- Closing the window (X button) exits the program cleanly and performs a final save – even with an empty annotation list
- If the total frame count of the video is unknown, the program warns; the timeline scaling may then be inaccurate
- At the end of the video, playback pauses automatically and jumps back to the start
- Text overlays use OpenCV's built-in fonts, so German UI strings are written without umlauts

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

This annotator creates the ground-truth data for [**OpenToF**](https://github.com/nrother/OpenToF/) — an open source Time-of-Flight measurement system for trampolining that measures the flight time of every jump. The annotated takeoff/landing CSVs serve as the reference against which the OpenToF sensor data is validated.

## License

See [LICENSE](LICENSE) in the repository.
