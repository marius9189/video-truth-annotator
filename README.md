# Video Truth Annotator

A minimal, keyboard-driven video player for frame-accurate event annotation. Built to create ground-truth data for sensor validation — marking takeoffs, landings, and LED toggles for time synchronization — it works with any video file and outputs a clean, timestamped CSV. Every annotation is autosaved atomically, so a crash never costs you your work.

Developed for the [OpenToF](https://github.com/nrother/OpenToF/) time-of-flight measurement project, but fully usable as a standalone tool.

## Features

- **Frame-accurate stepping** — move one frame forward or backward with the arrow keys
- **Millisecond-precise timestamps** — computed from the frame index and the video's frame rate, shown live on screen
- **Play / pause** with the space bar, playback speed matched to the real frame duration
- **Timeline scrubbing** — click or drag the progress bar at the bottom of the video to jump anywhere; marked frames are shown as yellow ticks
- **Event marking** — four configurable event types: Takeoff, Landing, LED on, LED off (useful for hardware/video time synchronization via an LED)
- **Visual feedback** — the current frame shows a clear marker bar when it is annotated, plus the time distance to the next upcoming event
- **Crash-safe autosave** — the CSV is rewritten atomically after every single annotation; a half-written file can never exist
- **Rich metadata** — video file, frame rate, frame duration, total frames, and creation date are written into the CSV as comment lines

## Installation

Requires Python 3.8 or newer.

1. Install Python from [python.org](https://www.python.org/downloads/). On Windows, check **"Add Python to PATH"** in the installer.
2. Install the only dependency:

   ```bash
   pip install opencv-python
   ```

3. Save the script as `video_truth_player.py` (or download it from this repository).

## Usage

Run the script with your video file as the only argument:

```bash
python video_truth_player.py take2.mov
```

Switch to your working directory first:

```bat
cd C:\OpenToF
python video_truth_player.py take1.MOV
```

If the video path contains spaces, wrap it in quotes.

The player window opens **paused** at frame 0.

### Keyboard controls

| Key | Action |
|---|---|
| `Space` | Play / Pause |
| `→` or `D` | One frame forward |
| `←` or `A` | One frame backward |
| `O` | Mark Takeoff on the current frame |
| `L` | Mark Landing on the current frame |
| `T` | Mark LED on (for board/video sync) |
| `F` | Mark LED off |
| `U` | Remove the annotation on the current frame |
| `S` | Save CSV manually (autosave is always active) |
| `Q` / `Esc` | Quit (saves automatically) |

### Mouse controls

- **Click** anywhere in the progress bar at the bottom: jump to that position
- **Click and drag**: scrub through the video (playback pauses automatically)
- Yellow ticks on the progress bar show all annotated frames; a white marker shows the current position

### On-screen display

- Top bar: current frame number, timestamp in seconds (millisecond precision, comma decimal separator to match the ground-truth CSV format), play/pause state, and a `[MARKIERT]` (marked) indicator
- Second line: distance in milliseconds to the next annotated event
- Bottom: orange bar with the event name when the current frame is annotated

### Output format

Annotations are written to `<video>_events.csv` next to the video file (e.g. `take1.mov` → `take1_events.csv`):

```
# Video: take1.mov
# FPS: 29.970030
# Frame-Dauer: 33.367 ms
# Gesamtframes: 5400
# Video-Dauer: 180.181 s
# Erstellt: 2026-09-24T07:30:00
Frame;Timestamp;Event
286;9,568;Takeoff
304;10,109;Landing
369;12,300;LED on
```

- The `#` comment lines carry the metadata; the `Frame` column makes every timestamp exactly reproducible (`timestamp = frame / fps`), independent of rounding.
- Load it in Python with:

  ```python
  import pandas as pd
  df = pd.read_csv("take1_events.csv", sep=";", comment="#")
  ```

## Tips for accurate annotation

- **Variable frame rate (VFR) videos:** many smartphones record with a fluctuating frame rate. The tool assumes a constant rate, so convert VFR footage first:

  ```bash
  ffmpeg -i take2.mov -vf fps=30000/1001 -crf 18 take2_cfr.mp4
  ```

  A warning at startup ("unbekannte Framerate" or an implausible frame count) is a hint to check this.
- **Don't keep the CSV open in Excel** while annotating — Windows locks the file, and the tool can only write a temporary backup copy until it is closed.
- For millisecond-exact work, pause and step frame by frame instead of relying on playback timing.

## Troubleshooting

| Problem | Solution |
|---|---|
| `can't open file … No such file or directory` | Check the exact filename, including hidden extensions (Windows hides `.txt` by default) |
| `cd` doesn't switch drives (cmd) | Use `cd /d D:\OpenToF` or type `D:` first |
| Video opens but shows garbage frames | Exotic codec; convert with FFmpeg (see above) |
| Warning "unbekannte Framerate" | The container reports no frame rate; the tool assumes 30 fps — convert the video to fix the timestamps |
| Arrow keys do nothing | Use the alternative keys `A` / `D` |

## Limitations

- Timestamps are derived from the frame index, so they are only as accurate as the container's frame rate metadata.
- Text overlays use OpenCV's built-in fonts and therefore contain no special characters (German output strings are written without umlauts).
- The tool reads but does not modify the video; no re-encoding takes place.

## License

Released under the [MIT License](LICENSE).
