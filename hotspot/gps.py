"""Read NMEA fixes without blocking camera capture."""
import collections
import datetime
import logging
import math
import os
import select
import termios
import threading
import time

LOG = logging.getLogger("hotspot.gps")


def parse_gga(sentence):
    if not sentence.startswith(("$GNGGA,", "$GPGGA,")) or "*" not in sentence:
        return None
    body, checksum = sentence[1:].split("*", 1)
    value = 0
    for char in body:
        value ^= ord(char)
    try:
        if value != int(checksum[:2], 16):
            return None
        fields = body.split(",")
        quality = int(fields[6] or "0")
        if quality <= 0:
            return {"valid": False, "reason": "no_fix"}
        def coordinate(text, hemisphere, digits):
            degree, minute = int(text[:digits]), float(text[digits:])
            if not math.isfinite(minute) or minute < 0 or minute >= 60:
                raise ValueError("invalid minutes")
            value = degree + minute / 60
            return -value if hemisphere in ("S", "W") else value
        if fields[3] not in ("N", "S") or fields[5] not in ("E", "W"):
            return None
        lat = coordinate(fields[2], fields[3], 2)
        lon = coordinate(fields[4], fields[5], 3)
        altitude = float(fields[9]) if fields[9] and fields[10] == "M" else None
        hdop = float(fields[8]) if fields[8] else None
        if abs(lat) > 90 or abs(lon) > 180 or any(
            v is not None and not math.isfinite(v) for v in (altitude, hdop)
        ):
            return None
        return {"valid": True, "latitude": lat, "longitude": lon,
                "altitude_msl_m": altitude, "fix_quality": quality,
                "satellites": int(fields[7]) if fields[7] else None,
                "hdop": hdop, "gps_utc_time": fields[1] or None,
                "position_reference": "gps_antenna"}
    except (ValueError, IndexError):
        return None


class GPSReader:
    def __init__(self, device="/dev/serial0", baud=115200, max_age=3.0):
        self.device, self.baud, self.max_age = device, baud, max_age
        self.samples = collections.deque(maxlen=256)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            fd = None
            try:
                fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
                settings = termios.tcgetattr(fd)
                settings[0] = 0
                settings[1] = 0
                settings[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                settings[3] = 0
                speed = getattr(termios, "B" + str(self.baud))
                settings[4] = speed
                settings[5] = speed
                termios.tcsetattr(fd, termios.TCSANOW, settings)
                buffer = b""
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([fd], [], [], 0.5)
                    if not ready:
                        continue
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    buffer += chunk
                    lines = buffer.split(b"\n")
                    buffer = lines.pop()[-8192:]
                    for line in lines:
                        fix = parse_gga(line.decode("ascii", errors="ignore").strip())
                        if fix is not None:
                            with self.lock:
                                self.samples.append((time.monotonic(), fix))
            except (OSError, termios.error, AttributeError) as error:
                LOG.warning("GPS unavailable: %s", error)
            finally:
                if fd is not None:
                    os.close(fd)
            self.stop_event.wait(5)

    def snapshot(self, frame_clock):
        with self.lock:
            candidates = [(clock, fix) for clock, fix in self.samples if clock <= frame_clock]
        if not candidates:
            return {"valid": False, "reason": "no_fix"}
        clock, fix = candidates[-1]
        age = frame_clock - clock
        if age > self.max_age:
            return {"valid": False, "reason": "stale_fix", "age_seconds": round(age, 3)}
        return dict(fix, age_seconds=round(age, 3))

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
