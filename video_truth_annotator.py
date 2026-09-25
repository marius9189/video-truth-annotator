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
  m                : Wiedergabemodus umschalten (ECHTZEIT <-> SKIP)
  1 bis 9, 0       : jeden n-ten Frame anzeigen (1-10), wechsel auf SKIP
  q / ESC          : Beenden

Wiedergabemodus (auch per Schieberegler unter dem Video waehlbar):
  ECHTZEIT : Zielzeit jedes Frames aus absolutem Zeitzaehler; kommt das
             Dekodieren nicht hinterher, werden Frames uebersprungen
             (Sprung zum Soll-Frame), Wiedergabe nie langsamer als Echtzeit.
  SKIP n   : jeder n-te Frame wird angezeigt, jeder angezeigte Frame bleibt
             eine normale Frame-Dauer stehen -> n-fache Abspielgeschwindigkeit.

Timeline: Klick in die Leiste unten springt an die Stelle, Klick + Ziehen
scrubbt durchs Video (pausiert automatisch). Gelbe Striche = markierte
Frames, ROTE Striche = unlogische Sequenz (zwei Takeoffs oder zwei
Landungen direkt hintereinander, ohne das jeweilige Gegenstueck dazwischen).

Fenster frei resizierbar (WINDOW_NORMAL); bei 4K-Videos wird beim Start auf
maximal 1280x720 verkleinert, Seitenverhaeltnis bleibt erhalten.

Fuer die frameweise Annotation ist der Wiedergabemodus irrelevant: alle
Frames bleiben einzeln ansteuerbar (Pfeiltasten).

Nach jeder Aenderung an den Markierungen wird automatisch und atomar
in <video>_events.csv zwischengespeichert (kein Datenverlust bei Crash).
"""

import os
import sys
import time
import cv2
from datetime import datetime

WIN_NAME = ("Video Truth Player - Leertaste: Play/Pause, Pfeile: Frame, "
            "o/l: Takeoff/Landing, t/f: LED on/off, u: Loeschen, "
            "s: Speichern, q: Ende")

TRACKBAR_NAME = "Wiedergabemodus (0=Echtzeit, 1-10=Skip)"

BAR_H = 28         # Hoehe der Fortschrittsleiste unten
OVERLAY_H = 90     # Hoehe des Info-Overlays oben
MAX_START_W = 1280  # Startfenster: max. Breite
MAX_START_H = 720   # Startfenster: max. Hoehe

MODE_REALTIME = "realtime"  # Echtzeit-Wiedergabe (Auto-Skip bei Rueckstand)
MODE_SKIP = "skip"          # jeder n-te Frame, n-fache Geschwindigkeit

FLIGHT_EVENTS = ("Takeoff", "Landing")  # muessen streng alternieren

YELLOW = (0, 255, 255)   # Timeline: gueltige Markierung
RED = (0, 0, 255)        # Timeline: unlogische Sequenz


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


def invalid_flight_frames(events):
    """Frame-Indizes zurueckgeben, deren Takeoff/Landing-Sequenz unlogisch ist.

    Regel: Takeoff und Landing muessen streng alternieren. Folgt auf einen
    Takeoff ein weiterer Takeoff (oder Landing auf Landing), sind BEIDE
    Markierungen ungueltig (rot). LED-Ereignisse stoeren die Sequenz nicht
    und werden nicht geprueft.
    """
    invalid = set()
    flight_seq = [(idx, name) for idx, name in sorted(events.items())
                  if name in FLIGHT_EVENTS]
    for (idx_a, name_a), (idx_b, name_b) in zip(flight_seq, flight_seq[1:]):
        if name_a == name_b:  # zwei Takeoffs oder zwei Landungen hintereinander
            invalid.add(idx_a)
            invalid.add(idx_b)
    return invalid


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
    frames_known = total_frames > 0  # Merker fuer Overlay-Anzeige
    if not frames_known:
        print("Warnung: Frame-Anzahl unbekannt, Timeline-Skalierung ggf. ungenau.")
        total_frames = 1

    paused = True
    events = {}  # frame_index -> event_name
    play_mode = MODE_SKIP  # aktueller Wiedergabemodus
    skip_step = 2              # SKIP: jeder n-te Frame (2 = jeder zweite)
    ret, frame = cap.read()
    if not ret:
        print("Fehler: Video enthaelt keine Frames.")
        sys.exit(1)

    # WINDOW_NORMAL statt AUTOSIZE: Fenster frei resizierbar, Bild skaliert mit
    cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL)
    # Startgroesse: maximal MAX_START_W x MAX_START_H, Seitenverhaeltnis erhalten
    frame_h, frame_w = frame.shape[:2]
    start_scale = min(MAX_START_W / frame_w, MAX_START_H / frame_h, 1.0)
    cv2.resizeWindow(WIN_NAME, int(frame_w * start_scale), int(frame_h * start_scale))

    # Echtzeit-Taktung: Basiszeit und Frame-Index bei Play-Start
    play_start = None      # Performance-Counter-Wert bei Play-Start
    play_start_idx = None  # Frame-Index bei Play-Start

    seek_state = {"seek": -1, "scrubbing": False, "total": total_frames}

    def on_mouse(event, x, y, flags, param):
        """Klick/Ziehen in die Fortschrittsleiste -> Seek-Ziel setzen."""
        if frame is None:
            return
        h, w = frame.shape[:2]
        bar_y0 = h - BAR_H
        if y < bar_y0:
            return  # Klick lag im Bild, nicht in der Leiste
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
        """Kurztext: naechstes markiertes Ereignis samt Zeitabstand."""
        upcoming = [(i, n) for i, n in sorted(events.items()) if i > idx]
        if upcoming:
            i, n = upcoming[0]
            return f"naechstes: {n} in {(i - idx) * frame_time_ms:.0f} ms"
        return "keine weiteren Ereignisse"

    def mode_text():
        if play_mode == MODE_REALTIME:
            return "ECHTZEIT"
        return f"SKIP x{skip_step} (jeder {skip_step}.)"

    def set_playback_mode(pos):
        """Trackbar-Callback: 0 = Echtzeit, 1-10 = jeder n-te Frame (SKIP)."""
        nonlocal play_mode, skip_step, play_start, play_start_idx
        new_mode = MODE_REALTIME if pos <= 0 else MODE_SKIP
        if new_mode == play_mode and (new_mode == MODE_REALTIME or pos == skip_step):
            return  # keine Aenderung -> nichts tun (kein Konsolen-Spam beim Ziehen)
        if new_mode == MODE_SKIP:
            skip_step = pos
        play_mode = new_mode
        if not paused and play_mode == MODE_REALTIME:
            # Taktbasis neu setzen, damit die Uhr beim Umschalten nicht springt
            play_start = time.perf_counter()
            play_start_idx = cur_index()
        print(f"Wiedergabemodus: {mode_text()}")

    def set_mode_from_keys(new_mode, new_step):
        """Modus per Tastatur setzen und Trackbar sichtbar mitsynchronisieren."""
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
        print(f"Wiedergabemodus: {mode_text()}")

    # Schieberegler unter dem Videofenster: Position 0 = Echtzeit,
    # Position 1-10 = jeder n-te Frame (SKIP). OpenCV-HighGUI kennt keine
    # Dropdown-Menues, Trackbar ist das naechstliegende Bedienelement.
    cv2.createTrackbar(TRACKBAR_NAME, WIN_NAME, 0, 10, set_playback_mode)

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
        invalid = invalid_flight_frames(events)

        # Fortschrittsleiste unten: grauer Grund, gruener Verlauf,
        # gelbe Striche fuer markierte Frames, rote fuer unlogische Sequenz
        bar_y0 = h - BAR_H
        scale = max(total_frames - 1, 1)
        cv2.rectangle(display, (0, bar_y0), (w, h), (60, 60, 60), -1)
        pos_x = int(min(idx / scale, 1.0) * w)
        cv2.rectangle(display, (0, bar_y0), (pos_x, h), (0, 200, 0), -1)
        for mi in events:
            mx = int(min(mi / scale, 1.0) * w)
            color = RED if mi in invalid else YELLOW
            cv2.rectangle(display, (mx - 2, bar_y0), (mx + 2, h), color, -1)
        # Positionsmarke (weiss)
        cv2.rectangle(display, (pos_x - 2, bar_y0), (pos_x + 2, h),
                     (255, 255, 255), -1)

        # Markierungs-Balken (orange) ueber der Leiste
        if idx in events:
            cv2.rectangle(display, (0, h - BAR_H - 80),
                          (w, h - BAR_H), (0, 120, 255), -1)
            cv2.putText(display, f"MARKIERT: {events[idx]}",
                        (10, h - BAR_H - 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 4, cv2.LINE_AA)

        # Overlay oben: Frame, Zeit, Status, Wiedergabemodus
        total_txt = f"/{total_frames}" if frames_known else ""
        if idx in events:
            marked = "  [MARKIERT - SEQUENZ UNLOGISCH]" if idx in invalid \
                else "  [MARKIERT]"
        else:
            marked = ""
        info = (f"Frame {idx}{total_txt}   "
                f"t = {fmt_time(idx / fps)} s   "
                f"[{'PAUSE' if paused else 'PLAY'}] {mode_text()}{marked}")
        cv2.rectangle(display, (0, 0), (w, OVERLAY_H), (0, 0, 0), -1)
        cv2.putText(display, info, (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 255, 0), 4, cv2.LINE_AA)
        cv2.putText(display, next_event_text(idx), (10, 84),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 0), 3, cv2.LINE_AA)
        cv2.imshow(WIN_NAME, display)

        # --- Taktung ---
        # ECHTZEIT: Zielzeit des NAECHSTEN Frames absolut berechnen und nur die
        #   Restzeit warten -> kein kumulativer Drift.
        # SKIP n:   jeder angezeigte Frame steht eine normale Frame-Dauer
        #   -> n-fache Geschwindigkeit.
        # Pause: kurz pollen, damit Maus-Scrubbing funktioniert.
        if paused:
            key = cv2.waitKeyEx(30) & 0xFFFFFFFF
        elif play_mode == MODE_SKIP:
            key = cv2.waitKeyEx(max(1, int(round(frame_time_ms)))) & 0xFFFFFFFF
        else:  # MODE_REALTIME
            elapsed = time.perf_counter() - play_start
            due = (cur_index() + 1 - play_start_idx) / fps  # Soll-Zeit naechster Frame
            key = cv2.waitKeyEx(max(1, int(round((due - elapsed) * 1000)))) & 0xFFFFFFFF

        # Timeline-Seek verarbeiten (pausiert automatisch)
        if seek_state["seek"] >= 0:
            paused = True
            ret, frame = do_seek(cap, seek_state["seek"])
            seek_state["seek"] = -1

        if key == ord('q') or key == 27:
            break
        elif key == 32:  # Leertaste
            paused = not paused
            if not paused:
                play_start = time.perf_counter()
                play_start_idx = cur_index()
        elif key == 2555904 or key == ord('d'):  # Pfeil rechts / d
            # am bekannten Ende direkt stehen bleiben, kein Fehl-Seek
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
        elif key == ord('m'):  # Modus umschalten
            set_mode_from_keys(MODE_SKIP if play_mode == MODE_REALTIME
                               else MODE_REALTIME, skip_step)
        elif ord('1') <= key <= ord('9'):  # Skip-Schritt 1-9
            set_mode_from_keys(MODE_SKIP, key - ord('0'))
        elif key == ord('0'):  # Skip-Schritt 10
            set_mode_from_keys(MODE_SKIP, 10)
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
            if idx in invalid_flight_frames(events):
                print("WARNUNG: Unlogische Sequenz - zwei Takeoffs oder zwei "
                      "Landungen hintereinander! (rot in der Timeline)")
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
            if play_mode == MODE_SKIP:
                # jeden n-ten Frame anzeigen: n Frames sequenziell lesen,
                # den letzten zeigen. Sequentielles Lesen ist meist schneller
                # als ein Seek; uebersprungen wird nur die Anzeige.
                for _ in range(skip_step):
                    ret, frame = cap.read()
                    if not ret:
                        break
                if not ret:  # Videoende -> pausieren, zurueck zum Start
                    paused = True
                    ret, frame = do_seek(cap, 0)
            else:
                # ECHTZEIT: Ziel-Frame nach Uhr; kommt das Dekodieren nicht
                # hinterher, direkt zum Soll-Frame springen (Frames werden
                # uebersprungen), damit die Wiedergabe in Echtzeit bleibt.
                target_idx = play_start_idx + int(
                    (time.perf_counter() - play_start) * fps)
                if target_idx > cur_index() + 1:
                    ok, new_frame = do_seek(cap, target_idx)
                    if ok:
                        frame = new_frame
                    else:
                        # Sprung fehlgeschlagen (i. d. R. Videoende)
                        paused = True
                        ret, frame = do_seek(cap, 0)
                else:
                    # im Takt: sequenziell weiterlesen (schneller als Seek)
                    ret, frame = cap.read()
                    if not ret:  # Videoende -> pausieren, zurueck zum Start
                        paused = True
                        ret, frame = do_seek(cap, 0)

    cap.release()
    cv2.destroyAllWindows()
    # Bedingungslos final speichern (auch bei leerer Markierungsliste)
    save_events(video_path, events, fps, total_frames, quiet=True)
    print(f"Beendet. {len(events)} Ereignisse in "
          f"{os.path.splitext(video_path)[0]}_events.csv")


if __name__ == "__main__":
    main()
