#!/usr/bin/env python3
"""
Video-Player zur Auswertung der Video-Truth-Daten.

Ereignisse: Takeoff, Landing, LED on, LED off (fuer Board-Sync).

Steuerung:
  Leertaste        : Play / Pause
  Pfeil rechts / d : ein Frame vor
  Pfeil links / a  : ein Frame zurueck
  o                : Takeoff markieren
  l                : Landing markieren
  t                : LED on markieren
  f                : LED off markieren
  u                : Markierung des aktuellen Frames loeschen
  s                : CSV manuell speichern (mit Statusausgabe)
  q / ESC          : Beenden

Timeline: Klick in die Leiste unten springt an die Stelle, Klick + Ziehen
scrubt durchs Video (pausiert automatisch). Gelbe Striche = markierte Frames.

Nach jeder Aenderung an den Markierungen wird automatisch und atomar
in <video>_events.csv zwischengespeichert (kein Datenverlust bei Crash).
"""

import os
import sys
import cv2
from datetime import datetime

WIN_NAME = ("Video Truth Player - Leertaste: Play/Pause, Pfeile: Frame, "
            "o/l: Takeoff/Landing, t/f: LED on/off, u: Loeschen, "
            "s: Speichern, q: Ende")

BAR_H = 14       # Hoehe der Fortschrittsleiste unten
OVERLAY_H = 46   # Hoehe des Info-Overlays oben


def fmt_time(sec):
    """Deutsche Zeitangabe wie in den Truth-CSVs (Komma als Dezimaltrennzeichen)."""
    return f"{sec:.3f}".replace(".", ",")


def save_events(video_path, events, fps, total_frames, quiet=False):
    """Atomar speichern: erst .tmp, dann os.replace(). Nie halbfertige CSV."""
    base, _ = os.path.splitext(video_path)
    out_path = base + "_events.csv"
    tmp_path = base + "_events.csv.tmp"
    frame_ms = 1000.0 / fps
    with open(tmp_path, "w", encoding="utf-8") as f:
        # Metadaten als Kommentar-Zeilen (pandas: comment='#')
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
        os.replace(tmp_path, out_path)  # atomar auf allen Plattformen
        if not quiet:
            print(f"Gespeichert: {out_path} ({len(events)} Ereignisse)")
    except PermissionError:
        # CSV z. B. in Excel geoeffnet -> .tmp bleibt als Rettungskopie
        print(f"WARNUNG: '{out_path}' ist gesperrt (in Excel geoeffnet?). "
              f"Rettungskopie: {tmp_path}")


def do_seek(cap, target):
    """An einen Frame springen und neuen Frame zurueckgeben (oder alten bei Fehler)."""
    old_pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(target, 0))
    ret, new_frame = cap.read()
    if not ret:
        # Seek fehlgeschlagen -> alte Position wiederherstellen
        cap.set(cv2.CAP_PROP_POS_FRAMES, old_pos)
        ret, new_frame = cap.read()
        if not ret:
            return False, None
    return True, new_frame


def main():
    if len(sys.argv) < 2:
        print("Verwendung: python video_truth_player.py <videodatei>")
        sys.exit(1)

    video_path = sys.argv[1]
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Fehler: Video '{video_path}' konnte nicht geoeffnet werden.")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or fps != fps:  # 0 oder NaN
        print("Warnung: unbekannte Framerate, nehme 30 fps an.")
        fps = 30.0
    frame_time_ms = 1000.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames_known = total_frames > 0   # Fix 3: Merker fuer Overlay-Anzeige
    if not frames_known:
        print("Warnung: Frame-Anzahl unbekannt, Timeline-Skalierung ggf. ungenau.")
        total_frames = 1

    paused = True
    events = {}  # frame_index -> event_name
    ret, frame = cap.read()
    if not ret:
        print("Fehler: Video enthaelt keine Frames.")
        sys.exit(1)

    cv2.namedWindow(WIN_NAME, cv2.WINDOW_AUTOSIZE)

    seek_state = {"seek": -1, "scrubbing": False, "total": total_frames}

    def on_mouse(event, x, y, flags, param):
        if frame is None:
            return
        h, w = frame.shape[:2]
        bar_y0 = h - BAR_H
        if y < bar_y0:
            return
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
        """Index des aktuell angezeigten Frames."""
        return int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1

    def next_event_text(idx):
        upcoming = [(i, n) for i, n in sorted(events.items()) if i > idx]
        if upcoming:
            i, n = upcoming[0]
            return f"naechstes: {n} in {(i - idx) * frame_time_ms:.0f} ms"
        return "keine weiteren Ereignisse"

    while True:
        if frame is None:  # Video unlesbar geworden -> sauber beenden
            print("Kein Frame mehr lesbar, beende.")
            break

        # Fenster per X-Button geschlossen -> sauber beenden (mit Speichern)
        if cv2.getWindowProperty(WIN_NAME, cv2.WND_PROP_VISIBLE) < 1:
            print("Fenster geschlossen.")
            break

        display = frame.copy()
        idx = cur_index()
        h, w = display.shape[:2]

        # Fortschrittsleiste unten: grauer Grund, gruener Verlauf,
        # gelbe Striche fuer markierte Frames
        bar_y0 = h - BAR_H
        scale = max(total_frames - 1, 1)
        cv2.rectangle(display, (0, bar_y0), (w, h), (60, 60, 60), -1)
        pos_x = int(min(idx / scale, 1.0) * w)
        cv2.rectangle(display, (0, bar_y0), (pos_x, h), (0, 200, 0), -1)
        for mi in events:
            mx = int(min(mi / scale, 1.0) * w)
            cv2.rectangle(display, (mx - 1, bar_y0), (mx + 1, h), (0, 255, 255), -1)
        # Positionsmarke (weiss)
        cv2.rectangle(display, (pos_x - 1, bar_y0), (pos_x + 1, h), (255, 255, 255), -1)

        # Markierungs-Balken (orange) ueber der Leiste
        if idx in events:
            cv2.rectangle(display, (0, h - BAR_H - 40),
                          (w, h - BAR_H), (0, 120, 255), -1)
            cv2.putText(display, f"MARKIERT: {events[idx]}",
                        (10, h - BAR_H - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA)

        # Overlay oben: Frame, Zeit, Status
        # Fix 3: ohne bekannte Frame-Anzeige nur "Frame <idx>" statt "/1"
        total_txt = f"/{total_frames}" if frames_known else ""
        cv2.rectangle(display, (0, 0), (w, OVERLAY_H), (0, 0, 0), -1)
        marked = "  [MARKIERT]" if idx in events else ""
        info = (f"Frame {idx}{total_txt}   "
                f"t = {fmt_time(idx / fps)} s   "
                f"[{'PAUSE' if paused else 'PLAY'}]{marked}")
        cv2.putText(display, info, (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.putText(display, next_event_text(idx), (10, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)
        cv2.imshow(WIN_NAME, display)

        # Fix 1: bei Pause pollen (30 ms), damit Scrubbing per Maus
        # auf allen Backends funktioniert; sonst echte Frame-Dauer
        delay = max(1, int(round(frame_time_ms))) if not paused else 30
        key = cv2.waitKeyEx(delay) & 0xFFFFFFFF

        # Timeline-Seek verarbeiten (pausiert automatisch)
        if seek_state["seek"] >= 0:
            paused = True
            ret, frame = do_seek(cap, seek_state["seek"])
            seek_state["seek"] = -1

        if key == ord('q') or key == 27:
            break
        elif key == 32:  # Leertaste
            paused = not paused
        elif key == 2555904 or key == ord('d'):  # Pfeil rechts / d
            # Fix 2: am bekannten Ende direkt stehen bleiben, kein Fehl-Seek
            if frames_known and cur_index() >= total_frames - 1:
                pass
            else:
                paused = True
                ret, frame = cap.read()
                if not ret:
                    ret, frame = do_seek(cap, cur_index())
        elif key == 2424832 or key == ord('a'):  # Pfeil links / a
            paused = True
            ret, frame = do_seek(cap, cur_index() - 1)
        elif key in (ord('o'), ord('l'), ord('t'), ord('f')):
            idx = cur_index()
            name = {ord('o'): "Takeoff",
                    ord('l'): "Landing",
                    ord('t'): "LED on",
                    ord('f'): "LED off"}[key]
            if idx in events:
                print(f"Frame {idx} war als '{events[idx]}' markiert -> ueberschrieben")
            events[idx] = name
            print(f"{name} @ t = {fmt_time(idx / fps)} s (Frame {idx})")
            save_events(video_path, events, fps, total_frames, quiet=True)  # Autosave
        elif key == ord('u'):
            idx = cur_index()
            if idx in events:
                print(f"Geloescht: {events.pop(idx)} @ Frame {idx}")
                save_events(video_path, events, fps, total_frames, quiet=True)  # Autosave
            else:
                print("Aktueller Frame ist nicht markiert.")
        elif key == ord('s'):
            save_events(video_path, events, fps, total_frames, quiet=True)
            print("Manuell gespeichert.")

        if not paused:
            ret, frame = cap.read()
            if not ret:  # Videoende -> pausieren, zurueck zum Start
                paused = True
                ret, frame = do_seek(cap, 0)

    cap.release()
    cv2.destroyAllWindows()
    # Fix 4: bedingungslos final speichern (auch bei leerer Markierungsliste)
    save_events(video_path, events, fps, total_frames, quiet=True)
    print(f"Beendet. {len(events)} Ereignisse in "
          f"{os.path.splitext(video_path)[0]}_events.csv")


if __name__ == "__main__":
    main()
