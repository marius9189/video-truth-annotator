#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "opencv-python>=4.5",
#     "numpy",
# ]
# ///
"""
Video player for evaluating video ground-truth data.

Events: Takeoff, Landing, LED on, LED off (for board synchronisation).
Every event can carry an optional free-text flag (key c), e.g. the sensor
name or a note about uncertainty; used flags are proposed as autocomplete
and remembered across runs in a flags file next to the script.

Output: <video>.video_truth.csv next to the video (or in the folder given
with --output-dir). An existing file is loaded at startup and annotation
continues; a legacy <video>_events.csv next to the video is loaded as a
fallback (and from then on saved under the new name).
The CSV metadata (video file, FPS, total frames) is checked first; on a
mismatch the old CSV is left untouched and a numbered CSV is written
instead (video.video_truth.1.csv, video.video_truth.2.csv, ...). Fields
the video itself does not report are skipped in the check.
--annotator NAME stores the annotator's name in the CSV header.

Key controls: printed with --controls.

Playback mode (also selectable via the slider below the video):
  REALTIME : target time of each frame from an absolute time counter; if
             decoding cannot keep up, frames are skipped (decoded without
             display, or a seek for large gaps), never slower than real time.
  SKIP n   : every n-th frame is shown at n-fold playback speed, clocked
             against an absolute time counter as well.

Timeline:
  Clicking the bar at the bottom jumps to that position, click + drag scrubs
  through the video (pauses automatically). Hovering shows the frame and time
  under the mouse. Tick color by event type (Takeoff light blue, Landing
  magenta, LED on yellow, LED off orange), RED ticks = illogical sequence (two takeoffs or two landings directly in a row, without the
  respective counterpart in between).

Window freely resizable (WINDOW_NORMAL); 4K videos are scaled down to a
maximum of 1280x720 at startup, aspect ratio is preserved. The frame is
scaled to the window before the overlay is drawn, so the overlay has the
same size for every video resolution, and recently shown frames are cached
so stepping backwards does not need a seek.
For frame-by-frame annotation the playback mode is irrelevant:
all frames remain individually addressable (arrow keys). Letter keys also
work with Caps Lock enabled (uppercase input is normalised to lowercase).

After every change to the annotations, the data is automatically and
atomically saved to the events CSV (no data loss on crash).

Note: the CSV metadata comment lines are deliberately kept in German
(# Gesamtframes: etc.) so that the format stays compatible with the
existing truth CSVs and earlier files written by this tool.
"""

import argparse
import hashlib
import os
import sys
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from datetime import datetime

import cv2
import numpy as np

WIN_NAME = ("Video Truth Player - Space: Play/Pause, Arrows: Frame, "
            "o/l: Takeoff/Landing, t/f: LED on/off, c: Flag, u: Delete, "
            ",/.: Prev/Next mark, s: Save, q: Quit")
TRACKBAR_NAME = "Playback mode (0=real time, 1-10=skip)"

MAX_START_W = 1280  # start window: max. width
MAX_START_H = 720   # start window: max. height

# UI sizes below are in window pixels for a 1280x720 window and scale with
# the window size (clamped), independent of the video resolution.
REF_W, REF_H = 1280, 720
UI_SCALE_MIN, UI_SCALE_MAX = 0.5, 2.0
MIN_CANVAS_W, MIN_CANVAS_H = 160, 120
BAR_H = 26          # height of the timeline at the bottom
FONT = cv2.FONT_HERSHEY_SIMPLEX

VIEW_CACHE_BYTES = 256 * 1024 * 1024  # budget for cached, window-sized frames
MIN_VIEW_CACHE_FRAMES = 8
SKIP_RESYNC_S = 0.25  # SKIP: reset the clock if playback lags more than this
PAUSED_POLL_MS = 30   # key/mouse polling interval while paused

CHECK_SEEK_FRAMES = 1500  # --check-seek: frames decoded sequentially
CHECK_SEEK_SAMPLES = 6    # --check-seek: number of seek targets

MODE_REALTIME = "realtime"  # real-time playback (auto-skip when lagging)
MODE_SKIP = "skip"          # every n-th frame, n-fold speed

FLIGHT_EVENTS = ("Takeoff", "Landing")  # must strictly alternate

YELLOW = (0, 255, 255)
RED = (0, 0, 255)        # timeline: illogical sequence (overrides type color)
# timeline tick color per event type (BGR); unknown names from a CSV: grey
TICK_COLORS = {
    "Takeoff": (255, 170, 0),   # light blue
    "Landing": (255, 0, 255),   # magenta
    "LED on": (0, 255, 255),    # yellow
    "LED off": (0, 140, 255),   # orange
}
TICK_COLOR_OTHER = (200, 200, 200)
GREEN = (0, 220, 0)
CYAN = (255, 255, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
ORANGE = (0, 120, 255)   # banner: marked frame
DARK_RED = (0, 0, 170)   # banner: marked frame with illogical sequence
BAR_BG = (60, 60, 60)      # timeline: unplayed part (dark grey)
BAR_FILL = (170, 170, 170)  # timeline: played part (light grey, distinct from tick colors)

KEY_RIGHT = 2555904
KEY_LEFT = 2424832
KEY_UP = 2490368
KEY_DOWN = 2621440
KEY_PAGE_UP = 2162688
KEY_PAGE_DOWN = 2228224

CSV_SEP = ";"
COMMENT_PREFIX = "#"
TIMESTAMP_TOLERANCE_MS = 1.0  # max. deviation of the CSV timestamp column

# Free-text flag history (e.g. sensor names) proposed as autocomplete.
# Stored next to this script so it persists across videos/runs.
FLAG_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".video_truth_annotator_flags.txt")
FLAG_HISTORY_MAX = 200   # keep at most this many entries
FLAG_SUGGESTIONS = 6     # suggestions shown while typing a flag

CSV_SUFFIX = ".video_truth"     # output: <video>.video_truth.csv
LEGACY_CSV_SUFFIX = "_events"   # older versions wrote <video>_events.csv

CONTROLS = """Controls:
  Space            : Play / Pause
  Right arrow / d  : one frame forward
  Left arrow  / a  : one frame back
  PageDown / .     : jump to next annotation
  PageUp   / ,     : jump to previous annotation (or to the start)
  o                : mark Takeoff
  l                : mark Landing
  t                : mark LED on
  f                : mark LED off
  c                : set/change the free-text flag of the current mark
                     (e.g. sensor name); Enter=ok, Esc=cancel,
                     Up/Down/Tab=pick a suggestion
  u                : delete the annotation (with flag) of the current frame
  s                : save CSV manually (with status output)
  m                : toggle playback mode (REALTIME <-> SKIP)
  1 to 9, 0        : show every n-th frame (1-10), switch to SKIP
  q / ESC          : quit
Mouse: click/drag the timeline to jump/scrub, hover it to see frame and time."""


def normalize_key(key):
    """Normalise letter keys to lowercase so Caps Lock does not break input."""
    if ord('A') <= key <= ord('Z'):
        return key | 0x20  # ASCII: to lowercase
    return key


def fmt_time(sec):
    """German time format as in the truth CSVs (comma as decimal separator)."""
    return f"{sec:.3f}".replace(".", ",")


def sanitize_flag(text):
    """Strip characters that would break the semicolon CSV line format."""
    return text.replace(CSV_SEP, ",").replace("\n", " ").replace("\r", " ").strip()


def sanitize_meta(text):
    """Metadata values must stay on one comment line."""
    return text.replace("\n", " ").replace("\r", " ").strip()


# --- Drawing helpers ---------------------------------------------------------

def ui_scale(cw, ch):
    """UI scale factor for a canvas of cw x ch window pixels."""
    return min(max(min(cw / REF_W, ch / REF_H), UI_SCALE_MIN), UI_SCALE_MAX)


def thick(s, base=2):
    return max(1, int(round(base * s)))


def text_height(scale, thickness):
    (_, th), baseline = cv2.getTextSize("Ag", FONT, scale, thickness)
    return th + baseline


def text_width(text, scale, thickness):
    return cv2.getTextSize(text, FONT, scale, thickness)[0][0]


def fit_text(text, max_w, scale, thickness, keep_end=False):
    """Shorten `text` with '...' so it fits into max_w pixels."""
    if text_width(text, scale, thickness) <= max_w:
        return text
    ell = "..."
    lo, hi = 0, len(text)  # binary search: longest slice that still fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cand = ell + text[-mid:] if keep_end else text[:mid] + ell
        if text_width(cand, scale, thickness) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        return ""
    return ell + text[-lo:] if keep_end else text[:lo] + ell


def put_text(img, text, x, y, scale, color, thickness):
    cv2.putText(img, text, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)


def put_segments(img, segments, x, y, max_x, scale, thickness):
    """Draw (text, color) segments in one line; truncate at max_x."""
    for text, color in segments:
        fitted = fit_text(text, max_x - x, scale, thickness)
        if not fitted:
            break
        put_text(img, fitted, x, y, scale, color, thickness)
        if fitted != text:
            break
        x += text_width(text, scale, thickness)


def darken(img, y0, y1, alpha=0.35):
    """Darken the rows y0..y1 (semi-transparent overlay background)."""
    roi = img[max(0, y0):max(0, y1)]
    if roi.size:
        img[max(0, y0):max(0, y1)] = cv2.convertScaleAbs(roi, alpha=alpha)


def fit_rect(frame_w, frame_h, area_w, area_h):
    """Size and offset of the frame fitted into the area (aspect preserved)."""
    s = min(area_w / frame_w, area_h / frame_h)
    vw = max(1, int(round(frame_w * s)))
    vh = max(1, int(round(frame_h * s)))
    return vw, vh, (area_w - vw) // 2, (area_h - vh) // 2


# --- Flag history and editor -------------------------------------------------

def load_flag_history(path):
    """Read the flag history (one flag per line); [] if missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except (OSError, UnicodeDecodeError):
        return []


def save_flag_history(path, history):
    """Write the flag history atomically (tmp file + os.replace)."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for flag in history:
                f.write(flag + "\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        print(f"Warning: could not write flag history '{path}' ({exc}).")


def update_flag_history(history, flag):
    """Move `flag` to the front (case-insensitive dedupe), then cap the list."""
    key = flag.lower()
    history[:] = [f for f in history if f.lower() != key]
    history.insert(0, flag)
    del history[FLAG_HISTORY_MAX:]


def autocomplete(text, history, limit=FLAG_SUGGESTIONS):
    """History entries containing `text` (case-insensitive), recency first."""
    needle = text.lower()
    return [flag for flag in history if needle in flag.lower()][:limit]


def edit_flag_text(base_display, event_name, initial, history, bar_h):
    """
    In-window text editor for the free-text flag, with autocomplete over the
    flag history. Returns (text, committed); committed=False means aborted
    (Esc). Enter commits the typed text; an empty text removes the flag.
    Keys: printable ASCII appends, Backspace deletes, Tab completes/cycles.
    The panel is only redrawn after a key press.
    """
    text = initial
    prefix = initial   # text as typed; suggestions filter on this, so Tab
    pick = -1          # or the arrow keys can cycle through suggestions
    h, w = base_display.shape[:2]
    s = ui_scale(w, h)
    pad = int(round(12 * s))
    title_sc, input_sc, sugg_sc = 0.6 * s, 0.9 * s, 0.55 * s
    title_t, input_t, sugg_t = thick(s), thick(s), thick(s)
    title_h = text_height(title_sc, title_t)
    input_h = text_height(input_sc, input_t)
    sugg_h = text_height(sugg_sc, sugg_t) + int(round(4 * s))
    panel_h = pad + title_h + pad + input_h + pad + FLAG_SUGGESTIONS * sugg_h + pad
    y1 = h - bar_h
    y0 = max(0, y1 - panel_h)
    dirty = True
    while True:
        # window closed via X button -> abort the edit; the main loop then
        # exits cleanly (with save)
        if cv2.getWindowProperty(WIN_NAME, cv2.WND_PROP_VISIBLE) < 1:
            return initial, False
        matches = autocomplete(prefix, history)
        if dirty:
            display = base_display.copy()
            cv2.rectangle(display, (0, y0), (w, y1), (30, 30, 140), -1)
            y = y0 + pad + title_h
            put_text(display, fit_text(f"Flag for '{event_name}'  (Enter=ok, "
                                       f"Esc=cancel, Up/Down/Tab=select)",
                                       w - 2 * pad, title_sc, title_t),
                     pad, y, title_sc, WHITE, title_t)
            y += pad + input_h
            put_text(display, fit_text(text + "_", w - 2 * pad, input_sc,
                                       input_t, keep_end=True),
                     pad, y, input_sc, YELLOW, input_t)
            y += pad
            for i, candidate in enumerate(matches):
                y += sugg_h
                selected = (i == pick)
                put_text(display, fit_text(("> " if selected else "  ") + candidate,
                                           w - 2 * pad, sugg_sc, sugg_t),
                         pad, y, sugg_sc,
                         YELLOW if selected else (200, 200, 200), sugg_t)
            cv2.imshow(WIN_NAME, display)
            dirty = False
        key = cv2.waitKeyEx(PAUSED_POLL_MS) & 0xFFFFFFFF
        if key == 0xFFFFFFFF:
            continue                   # no key pressed
        dirty = True
        if key in (13, 10):            # Enter -> commit
            return text, True
        if key == 27:                  # Esc -> abort
            return initial, False
        if key == 8:                   # Backspace
            text = text[:-1]
            prefix = text
            pick = -1
        elif key == KEY_DOWN:          # next suggestion into the input
            if matches:
                pick = 0 if pick < 0 else (pick + 1) % len(matches)
                text = matches[pick]
        elif key == KEY_UP:            # previous suggestion
            if matches:
                pick = len(matches) - 1 if pick < 0 else (pick - 1) % len(matches)
                text = matches[pick]
        elif key == 9:                 # Tab -> next suggestion (cycles)
            if matches:
                pick = (pick + 1) % len(matches)
                text = matches[pick]
        elif 32 <= key <= 126:         # printable ASCII
            text += chr(key)
            prefix = text
            pick = -1


# --- CSV ---------------------------------------------------------------------

def events_path_for(video_path, counter=0, out_dir=None):
    """
    Path of the events CSV belonging to the video: video.video_truth.csv,
    or video.video_truth.<counter>.csv for counter > 0. The CSV lies in
    out_dir if given, else next to the video.
    """
    base, _ = os.path.splitext(video_path)
    if out_dir is not None:
        base = os.path.join(out_dir, os.path.basename(base))
    suffix = f"{CSV_SUFFIX}.{counter}" if counter else CSV_SUFFIX
    return base + suffix + ".csv"


def legacy_events_path_for(video_path):
    """Path of the CSV written by older versions (video_events.csv)."""
    base, _ = os.path.splitext(video_path)
    return base + LEGACY_CSV_SUFFIX + ".csv"


def load_events(csv_path, fps):
    """
    Read in an existing events CSV.

    Returns: (events_dict, metadata_dict, error, warnings)
      events_dict  : {frame_index: (event_name, flag)}
      metadata_dict: {"video": ..., "fps": ..., "gesamtframes": ...,
                      "annotator": ... (optional)} or {}
      error        : None or description of why the file was not readable
      warnings     : list of human-readable warning messages, empty if none

    Data validation:
      - negative frame indices are rejected (reported via `warnings`)
      - the timestamp column is compared against frame/fps; deviations
        larger than TIMESTAMP_TOLERANCE_MS are reported (the frame
        index, not the timestamp, is the source of truth)
      - files without the flag column (older CSVs) are read as flag ""

    Warnings are returned instead of printed so the caller can report them
    only for the file it actually uses.
    """
    events = {}
    metadata = {}
    warnings = []
    rejected = 0
    ts_warnings = 0
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(COMMENT_PREFIX):
                    # metadata line: "# key: value"
                    content = line[len(COMMENT_PREFIX):].strip()
                    if ":" in content:
                        key, _, value = content.partition(":")
                        metadata[key.strip().lower()] = value.strip()
                    continue
                # data line: Frame;Timestamp;Event;Flag (Flag optional)
                parts = line.split(CSV_SEP, 3)
                if len(parts) < 3:
                    continue  # broken line -> skip
                try:
                    frame_idx = int(parts[0].strip())
                except ValueError:
                    continue  # e.g. header line "Frame;Timestamp;Event"
                if frame_idx < 0:
                    rejected += 1
                    continue
                # plausibility of the timestamp column (source of truth is
                # the frame index; a deviation usually means a hand-edited
                # or externally converted file)
                try:
                    ts_csv = float(parts[1].strip().replace(",", "."))
                    ts_expected = frame_idx / fps
                    if abs(ts_csv - ts_expected) * 1000.0 > TIMESTAMP_TOLERANCE_MS:
                        ts_warnings += 1
                except ValueError:
                    pass  # no or malformed timestamp -> ignore, frame is valid
                event_name = parts[2].strip()
                flag = sanitize_flag(parts[3]) if len(parts) > 3 else ""
                if event_name:
                    events[frame_idx] = (event_name, flag)
        if rejected:
            warnings.append(
                f"Warning: {rejected} line(s) with negative frame index "
                f"ignored in '{csv_path}'.")
        if ts_warnings:
            warnings.append(
                f"Warning: {ts_warnings} timestamp(s) in '{csv_path}' do "
                f"not match frame/fps (>{TIMESTAMP_TOLERANCE_MS:.0f} ms). "
                f"The frame index is used as source of truth.")
        return events, metadata, None, warnings
    except (OSError, UnicodeDecodeError) as exc:
        return {}, None, str(exc), warnings


def check_metadata_match(metadata, video_path, fps, total_frames,
                          fps_known=True, frames_known=True):
    """
    Compare the CSV metadata with the opened video.

    Returns: list of error descriptions (empty = everything matches).
    Video name, FPS and total frames are checked. FPS is compared with a
    small tolerance (rounding in the CSV format: 6 decimal places).
    Fields the video itself does not report (unknown frame rate or frame
    count) are skipped: comparing them against the fallback value
    (30 fps / 1) would produce a false conflict, although the CSV is
    obviously the correct one for this video.
    The annotator is not checked (it is not a property of the video).
    """
    errors = []
    video_name = os.path.basename(video_path)

    csv_video = metadata.get("video")
    if csv_video is not None and csv_video != video_name:
        errors.append(
            f"Video name mismatch: CSV '{csv_video}' vs. video '{video_name}'")

    if fps_known:
        csv_fps = metadata.get("fps")
        if csv_fps is not None:
            try:
                if abs(float(csv_fps.replace(",", ".")) - fps) > 1e-3:
                    errors.append(
                        f"FPS mismatch: CSV {csv_fps} vs. video {fps:.6f}")
            except ValueError:
                errors.append(f"FPS in CSV not interpretable: '{csv_fps}'")

    if frames_known:
        csv_total = metadata.get("gesamtframes")
        if csv_total is not None:
            try:
                if int(csv_total) != total_frames:
                    errors.append(
                        f"Total frames mismatch: CSV {csv_total} vs. "
                        f"video {total_frames}")
            except ValueError:
                errors.append(
                    f"Total frames in CSV not interpretable: '{csv_total}'")

    return errors


def find_free_events_path(video_path, out_dir=None):
    """
    Find the next free CSV path: video.video_truth.csv,
    then video.video_truth.1.csv, video.video_truth.2.csv, ...
    """
    counter = 0
    while os.path.exists(events_path_for(video_path, counter, out_dir)):
        counter += 1
    return events_path_for(video_path, counter, out_dir)


def numbered_events_paths(video_path, out_dir=None):
    """
    Existing numbered CSVs for the video (video.video_truth.1.csv,
    video.video_truth.2.csv, ...) in the output folder first (if given),
    then next to the video. These are created after a metadata conflict and
    should be resumed, not replaced.
    """
    seen = set()
    for d in ([None] if out_dir is None else [out_dir, None]):
        counter = 1
        while True:
            path = events_path_for(video_path, counter, d)
            if not os.path.exists(path):
                break
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                yield path
            counter += 1


def select_resume_source(video_path, out_dir, default_csv, fps, total_frames,
                         fps_known, frames_known):
    """
    Pick the CSV to resume from and load it.

    Tries every existing CSV for this video, best first (the output folder or
    next to the video, numbered variants included, then a legacy
    <video>_events.csv), and returns the first one that matches the video's
    metadata. A numbered CSV written after an earlier conflict therefore
    belongs to the video and is resumed instead of forking yet another file
    on every start.

    Returns (src_csv, events, metadata, error). src_csv is None when no CSV
    exists; error is None for a readable file, else the reason it could not
    be read. If no existing file matches, the first one is returned so the
    caller can report the problem.
    """
    paths = [default_csv]
    paths.extend(numbered_events_paths(video_path, out_dir))
    if out_dir is not None:  # also look next to the video
        paths.append(events_path_for(video_path))
    paths.append(legacy_events_path_for(video_path))

    existing = []
    seen = set()
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and os.path.exists(p):
            seen.add(key)
            existing.append(p)
    if not existing:
        return None, {}, {}, None

    first = None
    for path in existing:
        loaded, metadata, error, warnings = load_events(path, fps)
        if first is None:
            first = (path, loaded, metadata, error)
        if error is None and not check_metadata_match(
                metadata, video_path, fps, total_frames,
                fps_known=fps_known, frames_known=frames_known):
            if path != first[0]:
                print(f"Note: '{first[0]}' does not match the video; "
                      f"resuming '{path}' instead.")
            for w in warnings:  # only the file actually resumed
                print(w)
            return path, loaded, metadata, None
    return first


def save_events(video_path, events, fps, total_frames, quiet=False,
                out_path=None, annotator=None):
    """
    Save atomically: first .tmp, then os.replace(). Never a half-written CSV.
    out_path allows writing to a different CSV (e.g. video.video_truth.1.csv
    after a metadata conflict). annotator (optional) is written as
    '# Annotator: ...' metadata line.
    Returns True on success, False if the file could not be written.
    """
    if out_path is None:
        out_path = events_path_for(video_path)
    tmp_path = out_path + ".tmp"
    frame_ms = 1000.0 / fps

    try:
        _write_events_file(tmp_path, video_path, events, fps, total_frames,
                           frame_ms, annotator)
    except OSError as exc:
        # e.g. read-only folder or full disk: keep running, report loudly
        print(f"WARNING: could not write '{tmp_path}' ({exc}). "
              f"Annotations are NOT saved yet.")
        return False

    try:
        os.replace(tmp_path, out_path)  # atomic on all platforms
    except PermissionError:
        # CSV e.g. opened in Excel -> .tmp remains as a rescue copy
        print(f"WARNING: '{out_path}' is locked (opened in Excel?). "
              f"Rescue copy: {tmp_path}")
        return False
    except OSError as exc:
        print(f"WARNING: could not replace '{out_path}' ({exc}). "
              f"Rescue copy: {tmp_path}")
        return False
    if not quiet:
        print(f"Saved: {out_path} ({len(events)} events)")
    return True


def _write_events_file(path, video_path, events, fps, total_frames, frame_ms,
                       annotator=None):
    """Write metadata header and event lines to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        # metadata as comment lines (pandas: comment='#')
        f.write(f"# Video: {os.path.basename(video_path)}\n")
        f.write(f"# FPS: {fps:.6f}\n")
        f.write(f"# Frame-Dauer: {frame_ms:.3f} ms\n")
        f.write(f"# Gesamtframes: {total_frames}\n")
        f.write(f"# Video-Dauer: {total_frames * frame_ms / 1000.0:.3f} s\n")
        if annotator:
            f.write(f"# Annotator: {sanitize_meta(annotator)}\n")
        f.write(f"# Erstellt: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("Frame;Timestamp;Event;Flag\n")
        for idx, (name, flag) in sorted(events.items()):
            f.write(f"{idx};{fmt_time(idx / fps)};{name};{sanitize_flag(flag)}\n")


# --- Video access ------------------------------------------------------------

class FrameSource:
    """
    Decoded-frame access with own index tracking and a cache of window-sized
    frames.

    goto(target) picks the cheapest way to the target frame: the cache, a
    sequential read, grab() (decode without colour conversion) for small gaps
    ahead, or a seek. `idx` is the index of the current frame; `next_pos` the
    index the next cap.read() returns (None = unknown, next access seeks).
    """

    def __init__(self, cap, grab_ahead_max):
        self.cap = cap
        self.grab_ahead_max = grab_ahead_max
        self.idx = -1
        self.next_pos = 0
        self.raw = None             # full-resolution frame of `idx` (or None)
        self.cache = OrderedDict()  # idx -> window-sized frame (LRU)
        self.cache_size = None
        self.cache_cap = MIN_VIEW_CACHE_FRAMES

    def _read(self):
        if self.next_pos is None:
            return False
        ok, f = self.cap.read()
        if not ok:
            self.next_pos = None
            return False
        self.idx, self.raw = self.next_pos, f
        self.next_pos += 1
        return True

    def _seek(self, target):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        self.next_pos = target
        return self._read()

    def goto(self, target):
        """Make `target` the current frame. False (state kept) on failure."""
        target = max(target, 0)
        if target == self.idx and (self.raw is not None or target in self.cache):
            return True
        if target in self.cache:
            self.cache.move_to_end(target)
            self.idx, self.raw = target, None
            return True
        gap = None if self.next_pos is None else target - self.next_pos
        if gap is not None and 0 <= gap <= self.grab_ahead_max:
            for _ in range(gap):
                if not self.cap.grab():
                    self.next_pos = None
                    return False
                self.next_pos += 1
            return self._read()
        return self._seek(target)

    def view(self, size):
        """Current frame resized to `size` (w, h), from/into the cache."""
        if size != self.cache_size:
            self.cache.clear()
            self.cache_size = size
            self.cache_cap = max(MIN_VIEW_CACHE_FRAMES,
                                 VIEW_CACHE_BYTES // (size[0] * size[1] * 3))
        v = self.cache.get(self.idx)
        if v is not None:
            self.cache.move_to_end(self.idx)
            return v
        if self.raw is None and not self._seek(self.idx):
            return np.zeros((size[1], size[0], 3), np.uint8)
        shrink = size[0] < self.raw.shape[1]
        v = cv2.resize(self.raw, size, interpolation=(
            cv2.INTER_AREA if shrink else cv2.INTER_LINEAR))
        self.cache[self.idx] = v
        while len(self.cache) > self.cache_cap:
            self.cache.popitem(last=False)
        return v


def _fingerprint(frame):
    return hashlib.blake2b(np.ascontiguousarray(frame[::4, ::4]).tobytes(),
                           digest_size=16).digest()


def check_seek_accuracy(video_path, total_frames, frames_known):
    """
    Compare seeked frames with sequentially decoded ones (first
    CHECK_SEEK_FRAMES frames). Returns True if every seek landed exactly.
    """
    cap = cv2.VideoCapture(video_path)
    limit = min(total_frames, CHECK_SEEK_FRAMES) if frames_known else CHECK_SEEK_FRAMES
    print(f"Decoding up to {limit} frames sequentially ...")
    fps_at = []
    where = {}  # fingerprint -> frame indices (static scenes repeat)
    for i in range(limit):
        ok, f = cap.read()
        if not ok:
            break
        fp = _fingerprint(f)
        fps_at.append(fp)
        where.setdefault(fp, []).append(i)
    n = len(fps_at)
    if n < 2:
        print("Too few frames to check.")
        cap.release()
        return True
    targets = sorted({int(n * (k + 1) / (CHECK_SEEK_SAMPLES + 1)) for k in
                      range(CHECK_SEEK_SAMPLES)} - {0})
    problems = 0
    for t in targets:
        cap.set(cv2.CAP_PROP_POS_FRAMES, t)
        ok, f = cap.read()
        if not ok:
            print(f"  seek to frame {t}: read failed")
            problems += 1
            continue
        fp = _fingerprint(f)
        if fp == fps_at[t]:
            continue
        problems += 1
        hits = where.get(fp)
        if hits:
            j = min(hits, key=lambda i: abs(i - t))
            print(f"  seek to frame {t} returned frame {j} (off by {j - t:+d})")
        else:
            print(f"  seek to frame {t} returned an image that matches no "
                  f"sequentially decoded frame (decoding artefacts?)")
    cap.release()
    if problems:
        print(f"Seek check: {problems}/{len(targets)} seeks NOT frame-accurate. "
              f"Re-encode the video (e.g. ffmpeg -i in.mp4 -vf fps=<fps> "
              f"-g 1 -crf 18 out.mp4) before annotating.")
        return False
    print(f"Seek check: all {len(targets)} seeks within the first {n} frames "
          f"were frame-accurate.")
    return True


# --- Annotations -------------------------------------------------------------

def neighbor_annotation(marks, idx, direction):
    """Next (+1) or previous (-1) annotated frame in sorted `marks`, or None."""
    if direction > 0:
        i = bisect_right(marks, idx)
        return marks[i] if i < len(marks) else None
    i = bisect_left(marks, idx)
    return marks[i - 1] if i > 0 else None


def invalid_flight_frames(events):
    """
    Return the frame indices whose takeoff/landing sequence is illogical.

    Rule: takeoff and landing must strictly alternate. If a takeoff is
    followed by another takeoff (or a landing by a landing), BOTH
    annotations are invalid (red).
    LED events do not disturb the sequence and are not checked.
    """
    invalid = set()
    flight_seq = [(idx, entry[0]) for idx, entry in sorted(events.items())
                  if entry[0] in FLIGHT_EVENTS]
    for (idx_a, name_a), (idx_b, name_b) in zip(flight_seq, flight_seq[1:]):
        if name_a == name_b:
            # two takeoffs or two landings in a row
            invalid.add(idx_a)
            invalid.add(idx_b)
    return invalid


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Frame-accurate video annotation (takeoff, landing, "
                    "LED on/off) to <video>.video_truth.csv.")
    parser.add_argument("video", nargs="?", help="path to the video file")
    parser.add_argument("-o", "--output-dir", metavar="DIR",
                        help="folder for the CSV (default: next to the video); "
                             "created if missing")
    parser.add_argument("--annotator", metavar="NAME",
                        help="annotator name, stored in the CSV header "
                             "(default: keep the one from an existing CSV)")
    parser.add_argument("--check-seek", action="store_true",
                        help="check whether seeking in the video is "
                             "frame-accurate, then exit")
    parser.add_argument("--controls", action="store_true",
                        help="show the key controls and exit")
    args = parser.parse_args(argv)
    if args.controls:
        print(CONTROLS)
        sys.exit(0)
    if args.video is None:
        parser.print_usage()
        sys.exit(1)
    return args


def main():
    args = parse_args()
    video_path = args.video
    out_dir = args.output_dir
    if out_dir is not None:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            print(f"Error: could not create output folder '{out_dir}' ({exc}).")
            sys.exit(1)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: could not open video '{video_path}'.")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    fps_known = fps > 0 and fps == fps  # not 0, not NaN
    if not fps_known:
        print("Warning: unknown frame rate, assuming 30 fps.")
        fps = 30.0
    frame_time_ms = 1000.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames_known = total_frames > 0  # marker for overlay display
    if not frames_known:
        print("Warning: frame count unknown, timeline disabled (frame "
              "stepping and jumping still work).")
        total_frames = 1

    if args.check_seek:
        cap.release()
        sys.exit(0 if check_seek_accuracy(video_path, total_frames,
                                          frames_known) else 2)

    # --- Load existing CSV and check metadata --------------------------------
    default_csv = events_path_for(video_path, out_dir=out_dir)
    out_csv = default_csv           # target CSV for all later saves
    events = {}                     # frame_index -> (event_name, flag)
    annotator = sanitize_meta(args.annotator) if args.annotator else None

    # source to resume from: the CSV in the output folder, else one next to
    # the video (when --output-dir is used), else a legacy <video>_events.csv.
    # A numbered file from an earlier conflict is preferred over a mismatching
    # one, so reopening does not create a new file every time.
    src_csv, loaded, metadata, error = select_resume_source(
        video_path, out_dir, default_csv, fps, total_frames,
        fps_known, frames_known)

    if src_csv is not None:
        if error is not None:
            print(f"ERROR: existing CSV '{src_csv}' could not be read "
                  f"({error}). A new CSV will be written; the existing "
                  f"file remains unchanged.")
            out_csv = find_free_events_path(video_path, out_dir)
            print(f"New CSV: {out_csv}")
        else:
            mismatches = check_metadata_match(
                metadata, video_path, fps, total_frames,
                fps_known=fps_known, frames_known=frames_known)
            if mismatches:
                print(f"ERROR: metadata of CSV '{src_csv}' does not "
                      f"match the video:")
                for m in mismatches:
                    print(f"  - {m}")
                print(f"({len(loaded)} events in the existing CSV are "
                      f"discarded, the file itself is kept.)")
                print("The existing CSV will NOT be overwritten. "
                      "Writing to a new CSV instead:")
                out_csv = find_free_events_path(video_path, out_dir)
                print(f"  {out_csv}")
            else:
                # A matching file that is not the default is resumed. If the
                # default exists as well it belongs to another video (it was
                # not chosen above), so it must not be overwritten.
                if src_csv != default_csv and os.path.exists(default_csv):
                    if src_csv == legacy_events_path_for(video_path):
                        # the legacy file is read-only: seed a new numbered CSV
                        out_csv = find_free_events_path(video_path, out_dir)
                        print(f"Note: resuming '{src_csv}'; the legacy file "
                              f"stays unchanged, changes are saved to "
                              f"'{out_csv}'.")
                    else:
                        out_csv = src_csv
                        print(f"Note: resuming '{src_csv}' (it matches the "
                              f"video); changes are written back to it. "
                              f"'{default_csv}' does not match and is left "
                              f"untouched.")
                elif src_csv != default_csv:
                    print(f"Note: found existing CSV '{src_csv}'; it is "
                          f"kept unchanged, changes are saved to "
                          f"'{default_csv}'.")
                csv_annotator = metadata.get("annotator") or None
                if annotator is None:
                    annotator = csv_annotator
                elif csv_annotator and csv_annotator != annotator:
                    print(f"Note: annotator changes from '{csv_annotator}' "
                          f"to '{annotator}' with the next save.")
                if loaded:
                    events = loaded
                    print(f"Loaded: {src_csv} ({len(events)} events, "
                          f"annotation continues)")
                else:
                    print(f"Note: '{src_csv}' contains no events. "
                          f"Starting with an empty annotation.")

    # --- Flag history (free text, e.g. sensor names) ------------------------
    flag_history = load_flag_history(FLAG_HISTORY_PATH)
    known = {f.lower() for f in flag_history}
    for _, flag in events.values():          # harvest flags from loaded CSV
        if flag and flag.lower() not in known:
            flag_history.append(flag)
            known.add(flag.lower())

    # Every change is autosaved immediately. `changed`: any change this
    # session; `unsaved`: the last save failed (e.g. CSV locked in Excel)
    # -> retried on exit.
    changed = False
    unsaved = False

    paused = True
    play_mode = MODE_SKIP   # current playback mode
    skip_step = 2           # SKIP: every n-th frame (2 = every second one)

    # small gaps are bridged by grab() instead of a seek (a seek decodes
    # again from the previous keyframe)
    src = FrameSource(cap, grab_ahead_max=max(2, int(round(fps))))
    if not src.goto(0):
        print("Error: video contains no frames.")
        sys.exit(1)
    frame_h, frame_w = src.raw.shape[:2]
    print(CONTROLS)

    # WINDOW_NORMAL instead of AUTOSIZE: window freely resizable
    cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL)

    # start size: at most MAX_START_W x MAX_START_H, aspect ratio preserved
    start_scale = min(MAX_START_W / frame_w, MAX_START_H / frame_h, 1.0)
    canvas_size = (max(MIN_CANVAS_W, int(frame_w * start_scale)),
                   max(MIN_CANVAS_H, int(frame_h * start_scale)))
    cv2.resizeWindow(WIN_NAME, *canvas_size)

    # clock base: playback time is measured from play_start at frame
    # play_start_idx (absolute, so no drift accumulates)
    play_start = None
    play_start_idx = None

    seek_state = {"seek": -1, "scrubbing": False, "hover": -1,
                  "total": total_frames, "bar_y0": canvas_size[1],
                  "width": canvas_size[0], "enabled": frames_known}

    # derived data, recomputed only when annotations change
    invalid_cache = invalid_flight_frames(events)
    marks = sorted(events)
    events_version = 0
    bar_cache = {"key": None, "grey": None, "fill": None}

    def on_mouse(event, x, y, flags, param):
        """Timeline: click/drag -> seek target, hover -> preview text."""
        if not param["enabled"]:
            return  # frame count unknown: no meaningful timeline position
        w = param["width"]
        frac = min(max(x / max(w - 1, 1), 0.0), 1.0)
        target = int(round(frac * (param["total"] - 1)))
        in_bar = y >= param["bar_y0"]
        if event == cv2.EVENT_LBUTTONDOWN and in_bar:
            param["scrubbing"] = True
            param["seek"] = target
        elif event == cv2.EVENT_MOUSEMOVE:
            if param["scrubbing"]:
                param["seek"] = target  # dragging may leave the bar
            param["hover"] = target if (in_bar or param["scrubbing"]) else -1
        elif event == cv2.EVENT_LBUTTONUP and param["scrubbing"]:
            param["scrubbing"] = False
            param["seek"] = target

    cv2.setMouseCallback(WIN_NAME, on_mouse, seek_state)

    def restart_clock():
        nonlocal play_start, play_start_idx
        play_start = time.perf_counter()
        play_start_idx = src.idx

    def jump_to_annotation(direction):
        """Seek to the next (+1) or previous (-1) annotated frame; without
        a previous mark, go to the start of the video."""
        nonlocal paused
        target = neighbor_annotation(marks, src.idx, direction)
        if target is None and direction < 0 and src.idx > 0:
            target = 0
        if target is None:
            print("No annotation in that direction.")
            return
        paused = True
        src.goto(target)

    def neighbor_segments(idx):
        """Overlay text for the previous and next mark as (text, color)
        segments; event names in their timeline color."""
        segs = []
        p = neighbor_annotation(marks, idx, -1)
        if p is None:
            segs.append(("last: none", CYAN))
        else:
            name = events[p][0]
            segs += [("last: ", CYAN),
                     (name, RED if p in invalid_cache else
                      TICK_COLORS.get(name, TICK_COLOR_OTHER)),
                     (f" {(idx - p) * frame_time_ms:.0f} ms ago", CYAN)]
        segs.append(("   |   ", (150, 150, 150)))
        n = neighbor_annotation(marks, idx, 1)
        if n is None:
            segs.append(("next: none", CYAN))
        else:
            name = events[n][0]
            segs += [("next: ", CYAN),
                     (name, RED if n in invalid_cache else
                      TICK_COLORS.get(name, TICK_COLOR_OTHER)),
                     (f" in {(n - idx) * frame_time_ms:.0f} ms", CYAN)]
        return segs

    def mode_text():
        if play_mode == MODE_REALTIME:
            return "REALTIME"
        return f"SKIP x{skip_step} (every {skip_step}.)"

    def apply_mode(new_mode, new_step, sync_trackbar):
        """Switch playback mode; restart the clock so playback does not jump."""
        nonlocal play_mode, skip_step
        if new_mode == play_mode and (new_mode == MODE_REALTIME or new_step == skip_step):
            return  # no change -> do nothing (no console spam while dragging)
        if new_mode == MODE_SKIP:
            skip_step = new_step
        play_mode = new_mode
        if not paused:
            restart_clock()
        if sync_trackbar:
            cv2.setTrackbarPos(TRACKBAR_NAME, WIN_NAME,
                               0 if play_mode == MODE_REALTIME else skip_step)
        print(f"Playback mode: {mode_text()}")

    def events_changed():
        """Refresh derived data and autosave after a change."""
        nonlocal invalid_cache, marks, events_version, changed, unsaved
        invalid_cache = invalid_flight_frames(events)
        marks = sorted(events)
        events_version += 1
        changed = True
        unsaved = not save_events(video_path, events, fps, total_frames,
                                  quiet=True, out_path=out_csv,
                                  annotator=annotator)

    def bar_layers(w, bar_h, s):
        """Timeline background/fill with ticks; rebuilt only on change."""
        key = (w, bar_h, events_version, frames_known)
        if bar_cache["key"] != key:
            grey = np.full((bar_h, w, 3), BAR_BG, np.uint8)
            fill = np.full((bar_h, w, 3), BAR_FILL, np.uint8)
            hw = max(1, int(round(2 * s)))
            scale = max(total_frames - 1, 1)
            for mi in (marks if frames_known else ()):
                mx = int(round(min(mi / scale, 1.0) * (w - 1)))
                color = (RED if mi in invalid_cache else
                         TICK_COLORS.get(events[mi][0], TICK_COLOR_OTHER))
                for layer in (grey, fill):
                    cv2.rectangle(layer, (mx - hw, 0), (mx + hw, bar_h), color, -1)
            bar_cache.update(key=key, grey=grey, fill=fill)
        return bar_cache["grey"], bar_cache["fill"]

    def render():
        """Compose the window image: frame, overlay, banner, timeline."""
        cw, ch = canvas_size
        s = ui_scale(cw, ch)
        bar_h = min(max(10, int(round(BAR_H * s))), ch)
        bar_y0 = ch - bar_h
        seek_state["bar_y0"], seek_state["width"] = bar_y0, cw
        pad = int(round(10 * s))
        idx = src.idx

        canvas = np.zeros((ch, cw, 3), np.uint8)
        vw, vh, ox, oy = fit_rect(frame_w, frame_h, cw, max(1, bar_y0))
        canvas[oy:oy + vh, ox:ox + vw] = src.view((vw, vh))

        # overlay at the top (semi-transparent): frame/time/state, next event
        s1, t1 = 0.75 * s, thick(s)
        s2, t2 = 0.6 * s, thick(s)
        h1, h2 = text_height(s1, t1), text_height(s2, t2)
        overlay_h = pad + h1 + pad + h2 + pad
        darken(canvas, 0, overlay_h)
        total_txt = f"/{total_frames}" if frames_known else ""
        info = (f"Frame {idx}{total_txt}   t = {fmt_time(idx / fps)} s   "
                f"[{'PAUSE' if paused else 'PLAY'}]   {mode_text()}")
        put_text(canvas, fit_text(info, cw - 2 * pad, s1, t1),
                 pad, pad + h1, s1, GREEN, t1)
        put_segments(canvas, neighbor_segments(idx), pad, 2 * pad + h1 + h2,
                     cw - pad, s2, t2)

        # annotation banner above the timeline (red if illogical sequence)
        if idx in events:
            name, flag = events[idx]
            bad = idx in invalid_cache
            fg = WHITE if bad else BLACK
            lines = [(f"MARKED: {name}" + ("  - ILLOGICAL SEQUENCE" if bad else ""),
                      0.95 * s, thick(s, 2.5))]
            if flag:
                lines.append((f"Flag: {flag}", 0.75 * s, thick(s)))
            heights = [text_height(sc, th) for _, sc, th in lines]
            banner_h = pad + sum(heights) + pad * len(lines)
            y0 = max(overlay_h, bar_y0 - banner_h)
            cv2.rectangle(canvas, (0, y0), (cw, bar_y0),
                          DARK_RED if bad else ORANGE, -1)
            y = y0
            for (text, sc, th), lh in zip(lines, heights):
                y += pad + lh
                put_text(canvas, fit_text(text, cw - 2 * pad, sc, th),
                         pad, y, sc, fg, th)

        # timeline: cached layers, fill up to the current position
        grey, fill = bar_layers(cw, bar_h, s)
        bar = canvas[bar_y0:]
        if frames_known:
            scale = max(total_frames - 1, 1)
            pos_x = int(round(min(idx / scale, 1.0) * (cw - 1)))
            bar[:, :pos_x] = fill[:, :pos_x]
            bar[:, pos_x:] = grey[:, pos_x:]
            hw = max(1, int(round(2 * s)))
            cv2.rectangle(bar, (pos_x - hw, 0), (pos_x + hw, bar_h), WHITE, -1)
        else:
            bar[:, :] = grey  # no frame count -> no meaningful position

        # hover: frame/time (and mark) under the mouse
        hover = seek_state["hover"]
        if frames_known and hover >= 0:
            hx = int(round(min(hover / scale, 1.0) * (cw - 1)))
            cv2.line(bar, (hx, 0), (hx, bar_h), (230, 230, 230), 1)
            tip = f"Frame {hover}  t = {fmt_time(hover / fps)} s"
            if hover in events:
                tip += f"  {events[hover][0]}"
            sc, th = 0.55 * s, thick(s)
            tw, tth = text_width(tip, sc, th), text_height(sc, th)
            bw, bh = tw + 2 * pad, tth + pad
            bx = min(max(hx - bw // 2, 0), max(cw - bw, 0))
            by = max(bar_y0 - bh - pad // 2, 0)
            cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), (40, 40, 40), -1)
            put_text(canvas, tip, bx + pad, by + bh - pad // 2 - 2, sc, WHITE, th)
        return canvas

    # Slider below the video window: position 0 = real time,
    # positions 1-10 = every n-th frame (SKIP). OpenCV HighGUI has no
    # dropdown menus; a trackbar is the closest control element.
    cv2.createTrackbar(TRACKBAR_NAME, WIN_NAME, skip_step, 10,
                       lambda pos: apply_mode(
                           MODE_REALTIME if pos <= 0 else MODE_SKIP, pos, False))

    canvas = None
    last_render_key = None
    while True:
        # window closed via X button -> exit cleanly (with save)
        if cv2.getWindowProperty(WIN_NAME, cv2.WND_PROP_VISIBLE) < 1:
            print("Window closed.")
            break

        # draw at window resolution; skip redraw if nothing visible changed
        _, _, rw, rh = cv2.getWindowImageRect(WIN_NAME)
        if rw > 0 and rh > 0:
            canvas_size = (rw, rh)
        render_key = (src.idx, events_version, paused, play_mode, skip_step,
                      canvas_size, seek_state["hover"])
        if render_key != last_render_key:
            canvas = render()
            cv2.imshow(WIN_NAME, canvas)
            last_render_key = render_key

        # --- Clocking ---
        # Both modes: the due time of the NEXT shown frame is computed from
        # the absolute clock base, we wait only the remaining time -> no
        # cumulative drift. REALTIME: next frame at 1/fps. SKIP n: n frames
        # further, still 1/fps later (n-fold speed).
        # Pause: poll briefly so mouse scrubbing keeps working.
        if paused:
            wait_ms = PAUSED_POLL_MS
        else:
            speed = 1 if play_mode == MODE_REALTIME else skip_step
            elapsed = time.perf_counter() - play_start
            due = (src.idx + speed - play_start_idx) / (fps * speed)
            if play_mode == MODE_SKIP and elapsed - due > SKIP_RESYNC_S:
                # decoding cannot keep up with n-fold speed: show every n-th
                # frame as fast as possible instead of rushing to catch up
                restart_clock()
                elapsed, due = 0.0, 1.0 / fps
            wait_ms = max(1, int(round((due - elapsed) * 1000)))
        key = cv2.waitKeyEx(wait_ms) & 0xFFFFFFFF

        # process timeline seek (pauses automatically)
        if seek_state["seek"] >= 0:
            paused = True
            src.goto(seek_state["seek"])
            seek_state["seek"] = -1

        key = normalize_key(key)  # Caps Lock: letters arrive uppercase

        if key == ord('q') or key == 27:
            break
        elif key == 32:  # space bar
            paused = not paused
            if not paused:
                restart_clock()
        elif key == KEY_RIGHT or key == ord('d'):
            # at the known end, stay put, no faulty seek
            if not (frames_known and src.idx >= total_frames - 1):
                paused = True
                src.goto(src.idx + 1)
        elif key == KEY_LEFT or key == ord('a'):
            paused = True
            if src.idx > 0:
                src.goto(src.idx - 1)
        elif key == KEY_PAGE_DOWN or key == ord('.'):
            jump_to_annotation(1)
        elif key == KEY_PAGE_UP or key == ord(','):
            jump_to_annotation(-1)
        elif key == ord('m'):
            apply_mode(MODE_SKIP if play_mode == MODE_REALTIME else MODE_REALTIME,
                       skip_step, True)
        elif ord('1') <= key <= ord('9'):  # skip step 1-9
            apply_mode(MODE_SKIP, key - ord('0'), True)
        elif key == ord('0'):  # skip step 10
            apply_mode(MODE_SKIP, 10, True)
        elif key in (ord('o'), ord('l'), ord('t'), ord('f')):
            idx = src.idx
            name = {ord('o'): "Takeoff", ord('l'): "Landing",
                    ord('t'): "LED on", ord('f'): "LED off"}[key]
            # re-marking an existing frame changes only the name, the
            # already entered flag is kept
            old_flag = events[idx][1] if idx in events else ""
            if idx in events:
                print(f"Frame {idx} was already marked as '{events[idx][0]}' "
                      f"-> overwritten")
            events[idx] = (name, old_flag)
            print(f"{name} @ t = {fmt_time(idx / fps)} s (frame {idx})")
            events_changed()
            if idx in invalid_cache:
                print("WARNING: illogical sequence - two takeoffs or two "
                      "landings in a row! (red in the timeline)")
        elif key == ord('c'):
            idx = src.idx
            if idx not in events:
                print("Current frame is not annotated - mark it first "
                      "(o/l/t/f), then press c to add a flag.")
            else:
                paused = True  # no playback while editing
                canvas = render()  # base image must show the paused state
                name, old_flag = events[idx]
                text, committed = edit_flag_text(
                    canvas, name, old_flag, flag_history,
                    canvas.shape[0] - seek_state["bar_y0"])
                last_render_key = None  # editor drew over the window
                if committed:
                    new_flag = sanitize_flag(text)
                    events[idx] = (name, new_flag)
                    if new_flag:
                        update_flag_history(flag_history, new_flag)
                        save_flag_history(FLAG_HISTORY_PATH, flag_history)
                        print(f"Flag @ frame {idx}: '{new_flag}'")
                    else:
                        print(f"Flag removed @ frame {idx}")
                    events_changed()
                else:
                    print("Flag edit cancelled.")
        elif key == ord('u'):
            idx = src.idx
            if idx in events:
                name, flag = events.pop(idx)
                suffix = f" (flag: {flag})" if flag else ""
                print(f"Deleted: {name}{suffix} @ frame {idx}")
                events_changed()
            else:
                print("Current frame is not annotated.")
        elif key == ord('s'):
            if save_events(video_path, events, fps, total_frames,
                           quiet=True, out_path=out_csv, annotator=annotator):
                print("Saved manually.")
                unsaved = False
                changed = True  # the CSV was (re)written, so not "no changes"

        if not paused:
            if play_mode == MODE_SKIP:
                # every n-th frame: the n-1 frames in between are only
                # grabbed (decoded without colour conversion)
                target = src.idx + skip_step
            else:
                # REALTIME: target frame according to the clock; if decoding
                # cannot keep up, frames are skipped (grab for small gaps,
                # seek for large ones) so playback stays in real time
                target = max(src.idx + 1, play_start_idx + int(
                    (time.perf_counter() - play_start) * fps))
            if not src.goto(target):
                # end of video -> pause, jump back to the start
                paused = True
                src.goto(0)

    cap.release()
    cv2.destroyAllWindows()

    # Changes are autosaved, so a final save is only needed to retry a
    # failed one. Without changes nothing is written (no empty .1/.2 files
    # when a video is merely opened and closed).
    if unsaved and not save_events(video_path, events, fps, total_frames,
                                   quiet=True, out_path=out_csv,
                                   annotator=annotator):
        print("Finished. WARNING: last changes could NOT be saved "
              "(see rescue copy above).")
    elif changed:
        print(f"Finished. {len(events)} events in {out_csv}")
    else:
        print("Finished. No changes, CSV not rewritten.")


if __name__ == "__main__":
    main()
