#!/usr/bin/env python3
"""
Video player for evaluating video ground-truth data.

Events: Takeoff, Landing, LED on, LED off (for board synchronisation).

New compared to the original version:
  - At startup, an already existing <video>_events.csv is read in
    and annotation is continued (no more overwriting with an empty file).
  - Before loading, the CSV metadata is checked against the video
    (FPS, total frames, video file). If they do not match, an error
    message is printed to the console, the old CSV is left untouched
    and a new CSV with a suffix is written instead (video_events_1.csv,
    video_events_2.csv, ...). Fields that the video itself does not
    report (unknown frame rate / frame count) are skipped in the check,
    because a comparison against a fallback value would produce false
    conflicts.
  - The CSV is only rewritten when annotations actually changed
    (dirty flag); opening and closing without changes no longer
    produces new suffixed files or touches the existing one.

Controls:
  Space            : Play / Pause
  Right arrow / d  : one frame forward
  Left arrow  / a  : one frame back
  o                : mark Takeoff
  l                : mark Landing
  t                : mark LED on
  f                : mark LED off
  u                : delete the annotation of the current frame
  s                : save CSV manually (with status output)
  m                : toggle playback mode (ECHTZEIT <-> SKIP)
  1 to 9, 0        : show every n-th frame (1-10), switch to SKIP
  q / ESC          : quit

Playback mode (also selectable via the slider below the video):
  ECHTZEIT : target time of each frame from an absolute time counter; if
             decoding cannot keep up, frames are skipped
             (jump to the target frame), playback never slower than real time.
  SKIP n   : every n-th frame is shown, each shown frame stays on screen
             for one normal frame duration -> n-fold playback speed.

Timeline:
  Clicking the bar at the bottom jumps to that position, click + drag scrubs
  through the video (pauses automatically). Yellow ticks = annotated frames,
  RED ticks = illogical sequence (two takeoffs or two landings directly in a
  row, without the respective counterpart in between).

Window freely resizable (WINDOW_NORMAL); 4K videos are scaled down to a
maximum of 1280x720 at startup, aspect ratio is preserved.
For frame-by-frame annotation the playback mode is irrelevant:
all frames remain individually addressable (arrow keys). Letter keys also
work with Caps Lock enabled (uppercase input is normalised to lowercase).

After every change to the annotations, the data is automatically and
atomically saved to the events CSV (no data loss on crash).

Note: the CSV metadata comment lines are deliberately kept in German
(# Gesamtframes: etc.) so that the format stays compatible with the
existing truth CSVs and earlier files written by this tool.
"""

import os
import sys
import time
import cv2
from datetime import datetime

WIN_NAME = ("Video Truth Player - Space: Play/Pause, Arrows: Frame, "
            "o/l: Takeoff/Landing, t/f: LED on/off, u: Delete, "
            "s: Save, q: Quit")
TRACKBAR_NAME = "Playback mode (0=real time, 1-10=skip)"

BAR_H = 28          # height of the progress bar at the bottom
OVERLAY_H = 90      # height of the info overlay at the top
MAX_START_W = 1280  # start window: max. width
MAX_START_H = 720   # start window: max. height

MODE_REALTIME = "realtime"  # real-time playback (auto-skip when lagging)
MODE_SKIP = "skip"          # every n-th frame, n-fold speed

FLIGHT_EVENTS = ("Takeoff", "Landing")  # must strictly alternate

YELLOW = (0, 255, 255)  # timeline: valid annotation
RED = (0, 0, 255)       # timeline: illogical sequence

CSV_SEP = ";"
COMMENT_PREFIX = "#"
TIMESTAMP_TOLERANCE_MS = 1.0  # max. deviation of the CSV timestamp column


def normalize_key(key):
    """Normalise letter keys to lowercase so Caps Lock does not break input."""
    if ord('A') <= key <= ord('Z'):
        return key | 0x20  # ASCII: to lowercase
    return key


def fmt_time(sec):
    """German time format as in the truth CSVs (comma as decimal separator)."""
    return f"{sec:.3f}".replace(".", ",")


def events_path_for(video_path):
    """Path of the events CSV belonging to the video (without suffix)."""
    base, _ = os.path.splitext(video_path)
    return base + "_events.csv"


def load_events(csv_path, fps):
    """
    Read in an existing events CSV.

    Returns: (events_dict, metadata_dict, error)
      events_dict  : {frame_index: event_name}
      metadata_dict: {"video": ..., "fps": ..., "gesamtframes": ...} or {}
      error        : None or description of why the file was not readable

    Data validation:
      - negative frame indices are rejected (with a console message)
      - the timestamp column is compared against frame/fps; deviations
        larger than TIMESTAMP_TOLERANCE_MS are reported (the frame
        index, not the timestamp, is the source of truth)
    """
    events = {}
    metadata = {}
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
                # data line: Frame;Timestamp;Event
                parts = line.split(CSV_SEP)
                if len(parts) != 3:
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
                if event_name:
                    events[frame_idx] = event_name
        if rejected:
            print(f"Warning: {rejected} line(s) with negative frame index "
                  f"ignored in '{csv_path}'.")
        if ts_warnings:
            print(f"Warning: {ts_warnings} timestamp(s) in '{csv_path}' do "
                  f"not match frame/fps (>{TIMESTAMP_TOLERANCE_MS:.0f} ms). "
                  f"The frame index is used as source of truth.")
        return events, metadata, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, None, str(exc)


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


def find_free_events_path(video_path):
    """
    Find the next free CSV path: video_events.csv,
    then video_events_1.csv, video_events_2.csv, ...
    """
    base, _ = os.path.splitext(video_path)
    candidate = base + "_events.csv"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{base}_events_{counter}.csv"
        counter += 1
    return candidate


def save_events(video_path, events, fps, total_frames, quiet=False,
                out_path=None):
    """
    Save atomically: first .tmp, then os.replace(). Never a half-written CSV.
    out_path allows writing to a different CSV (e.g. video_events_1.csv
    after a metadata conflict).
    """
    if out_path is None:
        out_path = events_path_for(video_path)
    tmp_path = out_path + ".tmp"
    frame_ms = 1000.0 / fps

    with open(tmp_path, "w", encoding="utf-8") as f:
        # metadata as comment lines (pandas: comment='#')
        f.write(f"# Video: {os.path.basename(video_path)}\n")
        f.write(f"# FPS: {fps:.6f}\n")
        f.write(f"# Frame-Dauer: {frame_ms:.3f} ms\n")
        f.write(f"# Gesamtframes: {total_frames}\n")
        f.write(f"# Video-Dauer: {total_frames * frame_ms / 1000.0:.3f} s\n")
        f.write(f"# Erstellt: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("Frame;Timestamp;Event\n")
        for idx, name in sorted(events.items()):
            f.write(f"{idx};{fmt_time(idx / fps)};{name}\n")

    try:
        os.replace(tmp_path, out_path)  # atomic on all platforms
        if not quiet:
            print(f"Saved: {out_path} ({len(events)} events)")
    except PermissionError:
        # CSV e.g. opened in Excel -> .tmp remains as a rescue copy
        print(f"WARNING: '{out_path}' is locked (opened in Excel?). "
              f"Rescue copy: {tmp_path}")


def do_seek(cap, target):
    """Jump to a frame and return the new frame (or the old one on failure)."""
    old_pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(target, 0))
    ret, new_frame = cap.read()
    if not ret:
        # seek failed -> restore old position
        cap.set(cv2.CAP_PROP_POS_FRAMES, old_pos)
        ret, new_frame = cap.read()
        if not ret:
            return False, None
    return True, new_frame


def invalid_flight_frames(events):
    """
    Return the frame indices whose takeoff/landing sequence is illogical.

    Rule: takeoff and landing must strictly alternate. If a takeoff is
    followed by another takeoff (or a landing by a landing), BOTH
    annotations are invalid (red).
    LED events do not disturb the sequence and are not checked.
    """
    invalid = set()
    flight_seq = [(idx, name) for idx, name in sorted(events.items())
                  if name in FLIGHT_EVENTS]
    for (idx_a, name_a), (idx_b, name_b) in zip(flight_seq, flight_seq[1:]):
        if name_a == name_b:
            # two takeoffs or two landings in a row
            invalid.add(idx_a)
            invalid.add(idx_b)
    return invalid


def main():
    if len(sys.argv) < 2:
        print("Usage: python video_truth_annotator.py <video>")
        sys.exit(1)

    video_path = sys.argv[1]
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
        print("Warning: frame count unknown, timeline scaling may be inaccurate.")
        total_frames = 1

    # --- Load existing CSV and check metadata --------------------------------
    default_csv = events_path_for(video_path)
    out_csv = default_csv           # target CSV for all later saves
    events = {}                     # frame_index -> event_name

    if os.path.exists(default_csv):
        loaded, metadata, error = load_events(default_csv, fps)
        if error is not None:
            print(f"ERROR: existing CSV '{default_csv}' could not be read "
                  f"({error}). A new CSV will be written; the existing "
                  f"file remains unchanged.")
            out_csv = find_free_events_path(video_path)
            print(f"New CSV: {out_csv}")
        else:
            mismatches = check_metadata_match(
                metadata, video_path, fps, total_frames,
                fps_known=fps_known, frames_known=frames_known)
            if mismatches:
                print(f"ERROR: metadata of CSV '{default_csv}' does not "
                      f"match the video:")
                for m in mismatches:
                    print(f"  - {m}")
                print(f"({len(loaded)} events in the existing CSV are "
                      f"discarded, the file itself is kept.)")
                print("The existing CSV will NOT be overwritten. "
                      "Writing to a new CSV instead:")
                out_csv = find_free_events_path(video_path)
                print(f"  {out_csv}")
            elif loaded:
                events = loaded
                print(f"Loaded: {default_csv} ({len(events)} events, "
                      f"annotation continues)")
            else:
                print(f"Note: '{default_csv}' contains no events. "
                      f"Starting with an empty annotation.")

    # Only write the CSV again if annotations actually changed
    dirty = False

    paused = True
    play_mode = MODE_SKIP   # current playback mode
    skip_step = 2           # SKIP: every n-th frame (2 = every second one)

    ret, frame = cap.read()
    if not ret:
        print("Error: video contains no frames.")
        sys.exit(1)

    # WINDOW_NORMAL instead of AUTOSIZE: window freely resizable, image scales
    cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL)

    # start size: at most MAX_START_W x MAX_START_H, aspect ratio preserved
    frame_h, frame_w = frame.shape[:2]
    start_scale = min(MAX_START_W / frame_w, MAX_START_H / frame_h, 1.0)
    cv2.resizeWindow(WIN_NAME, int(frame_w * start_scale),
                     int(frame_h * start_scale))

    # real-time clocking: base time and frame index at play start
    play_start = None       # performance counter value at play start
    play_start_idx = None   # frame index at play start

    seek_state = {"seek": -1, "scrubbing": False, "total": total_frames}

    # invalid-sequence set: recomputed only when annotations change,
    # not on every displayed frame
    invalid_cache = invalid_flight_frames(events)

    def on_mouse(event, x, y, flags, param):
        """Click/drag in the progress bar -> set seek target."""
        if frame is None:
            return
        h, w = frame.shape[:2]
        bar_y0 = h - BAR_H
        if y < bar_y0:
            return  # click was in the image, not in the bar
        frac = min(max(x / w, 0.0), 1.0)
        target = int(frac * (param["total"] - 1))
        if event == cv2.EVENT_LBUTTONDOWN:
            param["scrubbing"] = True
            param["seek"] = target
        elif event == cv2.EVENT_MOUSEMOVE and param["scrubbing"]:
            param["seek"] = target
        elif event == cv2.EVENT_LBUTTONUP:
            param["scrubbing"] = False
            param["seek"] = target

    cv2.setMouseCallback(WIN_NAME, on_mouse, seek_state)

    def cur_index():
        """Index of the currently displayed frame."""
        return int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1

    def next_event_text(idx):
        """Short text: next annotated event including time distance."""
        upcoming = [(i, n) for i, n in sorted(events.items()) if i > idx]
        if upcoming:
            i, n = upcoming[0]
            return f"next: {n} in {(i - idx) * frame_time_ms:.0f} ms"
        return "no further events"

    def mode_text():
        if play_mode == MODE_REALTIME:
            return "ECHTZEIT"
        return f"SKIP x{skip_step} (every {skip_step}.)"

    def set_playback_mode(pos):
        """Trackbar callback: 0 = real time, 1-10 = every n-th frame (SKIP)."""
        nonlocal play_mode, skip_step, play_start, play_start_idx
        new_mode = MODE_REALTIME if pos <= 0 else MODE_SKIP
        if new_mode == play_mode and (new_mode == MODE_REALTIME or pos == skip_step):
            return  # no change -> do nothing (no console spam while dragging)
        if new_mode == MODE_SKIP:
            skip_step = pos
        play_mode = new_mode
        if not paused and play_mode == MODE_REALTIME:
            # reset the clock base so the clock does not jump on switching
            play_start = time.perf_counter()
            play_start_idx = cur_index()
        print(f"Playback mode: {mode_text()}")

    def set_mode_from_keys(new_mode, new_step):
        """Set mode via keyboard and synchronise the visible trackbar."""
        nonlocal play_mode, skip_step, play_start, play_start_idx
        if new_mode == MODE_SKIP:
            skip_step = new_step
        if new_mode == play_mode and (new_mode == MODE_REALTIME or new_step == skip_step):
            return
        play_mode = new_mode
        if not paused and play_mode == MODE_REALTIME:
            play_start = time.perf_counter()
            play_start_idx = cur_index()
        cv2.setTrackbarPos(TRACKBAR_NAME, WIN_NAME,
                           0 if play_mode == MODE_REALTIME else skip_step)
        print(f"Playback mode: {mode_text()}")

    # Slider below the video window: position 0 = real time,
    # positions 1-10 = every n-th frame (SKIP). OpenCV HighGUI has no
    # dropdown menus; a trackbar is the closest control element.
    cv2.createTrackbar(TRACKBAR_NAME, WIN_NAME, 0, 10, set_playback_mode)

    while True:
        if frame is None:
            # video became unreadable -> exit cleanly
            print("No more frames readable, quitting.")
            break

        # window closed via X button -> exit cleanly (with save)
        if cv2.getWindowProperty(WIN_NAME, cv2.WND_PROP_VISIBLE) < 1:
            print("Window closed.")
            break

        display = frame.copy()
        idx = cur_index()
        h, w = display.shape[:2]

        # progress bar at the bottom: grey background, green fill,
        # yellow ticks for annotated frames, red for illogical sequences
        bar_y0 = h - BAR_H
        scale = max(total_frames - 1, 1)
        cv2.rectangle(display, (0, bar_y0), (w, h), (60, 60, 60), -1)
        pos_x = int(min(idx / scale, 1.0) * w)
        cv2.rectangle(display, (0, bar_y0), (pos_x, h), (0, 200, 0), -1)
        for mi in events:
            mx = int(min(mi / scale, 1.0) * w)
            color = RED if mi in invalid_cache else YELLOW
            cv2.rectangle(display, (mx - 2, bar_y0), (mx + 2, h), color, -1)

        # position marker (white)
        cv2.rectangle(display, (pos_x - 2, bar_y0), (pos_x + 2, h),
                      (255, 255, 255), -1)

        # annotation banner (orange) above the bar
        if idx in events:
            cv2.rectangle(display, (0, h - BAR_H - 80), (w, h - BAR_H),
                          (0, 120, 255), -1)
            cv2.putText(display, f"MARKED: {events[idx]}",
                        (10, h - BAR_H - 24), cv2.FONT_HERSHEY_SIMPLEX,
                        1.6, (0, 0, 0), 4, cv2.LINE_AA)

        # overlay at the top: frame, time, status, playback mode
        total_txt = f"/{total_frames}" if frames_known else ""
        if idx in events:
            marked = ("  [MARKED - ILLOGICAL SEQUENCE]" if idx in invalid_cache
                      else "  [MARKED]")
        else:
            marked = ""
        info = (f"Frame {idx}{total_txt}  "
                f"t = {fmt_time(idx / fps)} s  "
                f"[{'PAUSE' if paused else 'PLAY'}] {mode_text()}{marked}")
        cv2.rectangle(display, (0, 0), (w, OVERLAY_H), (0, 0, 0), -1)
        cv2.putText(display, info, (10, 40), cv2.FONT_HERSHEY_SIMPLEX,
                    1.3, (0, 255, 0), 4, cv2.LINE_AA)
        cv2.putText(display, next_event_text(idx), (10, 84),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 0), 3, cv2.LINE_AA)

        cv2.imshow(WIN_NAME, display)

        # --- Clocking ---
        # ECHTZEIT: compute the target time of the NEXT frame absolutely and
        # wait only the remaining time -> no cumulative drift.
        # SKIP n: each shown frame stays for one normal frame duration
        # -> n-fold speed.
        # Pause: poll briefly so mouse scrubbing keeps working.
        if paused:
            key = cv2.waitKeyEx(30) & 0xFFFFFFFF
        elif play_mode == MODE_SKIP:
            key = cv2.waitKeyEx(max(1, int(round(frame_time_ms)))) & 0xFFFFFFFF
        else:  # MODE_REALTIME
            elapsed = time.perf_counter() - play_start
            due = (cur_index() + 1 - play_start_idx) / fps  # target time of next frame
            key = cv2.waitKeyEx(max(1, int(round((due - elapsed) * 1000)))) & 0xFFFFFFFF

        # process timeline seek (pauses automatically)
        if seek_state["seek"] >= 0:
            paused = True
            ret, frame = do_seek(cap, seek_state["seek"])
            seek_state["seek"] = -1

        key = normalize_key(key)  # Caps Lock: letters arrive uppercase

        if key == ord('q') or key == 27:
            break
        elif key == 32:  # space bar
            paused = not paused
            if not paused:
                play_start = time.perf_counter()
                play_start_idx = cur_index()
        elif key == 2555904 or key == ord('d'):  # right arrow / d
            # at the known end, stay put, no faulty seek
            if frames_known and cur_index() >= total_frames - 1:
                pass
            else:
                paused = True
                ret, frame = cap.read()
                if not ret:
                    ret, frame = do_seek(cap, cur_index())
        elif key == 2424832 or key == ord('a'):  # left arrow / a
            paused = True
            ret, frame = do_seek(cap, cur_index() - 1)
        elif key == ord('m'):
            # toggle mode
            set_mode_from_keys(
                MODE_SKIP if play_mode == MODE_REALTIME else MODE_REALTIME,
                skip_step)
        elif ord('1') <= key <= ord('9'):  # skip step 1-9
            set_mode_from_keys(MODE_SKIP, key - ord('0'))
        elif key == ord('0'):  # skip step 10
            set_mode_from_keys(MODE_SKIP, 10)
        elif key in (ord('o'), ord('l'), ord('t'), ord('f')):
            idx = cur_index()
            name = {ord('o'): "Takeoff", ord('l'): "Landing",
                    ord('t'): "LED on", ord('f'): "LED off"}[key]
            if idx in events:
                print(f"Frame {idx} was already marked as '{events[idx]}' "
                      f"-> overwritten")
            events[idx] = name
            print(f"{name} @ t = {fmt_time(idx / fps)} s (frame {idx})")
            invalid_cache = invalid_flight_frames(events)
            if idx in invalid_cache:
                print("WARNING: illogical sequence - two takeoffs or two "
                      "landings in a row! (red in the timeline)")
            dirty = True
            save_events(video_path, events, fps, total_frames, quiet=True,
                        out_path=out_csv)  # autosave
        elif key == ord('u'):
            idx = cur_index()
            if idx in events:
                print(f"Deleted: {events.pop(idx)} @ frame {idx}")
                invalid_cache = invalid_flight_frames(events)
                dirty = True
                save_events(video_path, events, fps, total_frames, quiet=True,
                            out_path=out_csv)  # autosave
            else:
                print("Current frame is not annotated.")
        elif key == ord('s'):
            save_events(video_path, events, fps, total_frames, quiet=True,
                        out_path=out_csv)
            print("Saved manually.")
            dirty = False  # just saved -> nothing unsaved anymore

        if not paused:
            if play_mode == MODE_SKIP:
                # show every n-th frame: read n frames sequentially,
                # display the last one. Sequential reading is usually faster
                # than a seek; only the display is skipped.
                for _ in range(skip_step):
                    ret, frame = cap.read()
                    if not ret:
                        break
                if not ret:
                    # end of video -> pause, jump back to the start
                    paused = True
                    ret, frame = do_seek(cap, 0)
            else:
                # ECHTZEIT: target frame according to the clock; if decoding
                # cannot keep up, jump directly to the target frame (frames
                # are skipped) so playback stays in real time.
                target_idx = play_start_idx + int(
                    (time.perf_counter() - play_start) * fps)
                if target_idx > cur_index() + 1:
                    ok, new_frame = do_seek(cap, target_idx)
                    if ok:
                        frame = new_frame
                    else:
                        # jump failed (usually end of video)
                        paused = True
                        ret, frame = do_seek(cap, 0)
                else:
                    # on schedule: continue reading sequentially (faster than seek)
                    ret, frame = cap.read()
                    if not ret:
                        # end of video -> pause, jump back to the start
                        paused = True
                        ret, frame = do_seek(cap, 0)

    cap.release()
    cv2.destroyAllWindows()

    # Final save - only if annotations actually changed since loading.
    # In a conflict case (suffixed CSV) this prevents empty _1/_2 files
    # when the user merely opened and closed the video.
    if dirty:
        save_events(video_path, events, fps, total_frames, quiet=True,
                    out_path=out_csv)
        print(f"Finished. {len(events)} events in {out_csv}")
    else:
        print("Finished. No changes, CSV not rewritten.")
if __name__ == "__main__":
    main()
