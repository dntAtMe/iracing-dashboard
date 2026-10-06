"""Session history: records every lap's telemetry, session events and the field's lap
times to SQLite, and reads them back for the analysis page.

Writes happen on the telemetry thread through one Recorder connection; the web
server opens its own short-lived read connections (WAL mode makes that safe).
"""
import json
import os
import shutil
import sqlite3
import string
import sys
import time
import zlib
from pathlib import Path

# output name -> (iRacing variable, decimals). 't' (seconds into the lap) is added by the recorder.
CHANNELS = {
    "pct": ("LapDistPct", 5),
    "speed": ("Speed", 2),
    "rpm": ("RPM", 0),
    "gear": ("Gear", 0),
    "thr": ("Throttle", 3),
    "brk": ("Brake", 3),
    "clu": ("Clutch", 3),
    "steer": ("SteeringWheelAngle", 3),
    "latG": ("LatAccel", 2),
    "lonG": ("LongAccel", 2),
    "yawRate": ("YawRate", 3),
    "fuel": ("FuelLevel", 3),
    "abs": ("BrakeABSactive", 0),
    "pit": ("OnPitRoad", 0),
}

# same priority order as the dashboard
FLAGS = [
    (0x10000 | 0x20000, "black"), (0x100000, "meatball"), (0x10, "red"), (0x1, "checkered"),
    (0x4000 | 0x8000 | 0x8 | 0x100, "yellow"), (0x20, "blue"), (0x2, "white"), (0x40, "debris"),
    (0x4 | 0x400, "green"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, started REAL, ended REAL, source TEXT, sim_key TEXT,
    track_id INTEGER, track TEXT, config TEXT, map_key TEXT, length_m REAL,
    car TEXT, type TEXT, name TEXT, driver TEXT, car_idx INTEGER
);
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY, session_id INTEGER, lap INTEGER, time REAL, official INTEGER,
    partial INTEGER, fuel_used REAL, pit INTEGER, incidents INTEGER, position INTEGER,
    class_position INTEGER, max_speed REAL, air REAL, track_temp REAL, started_at REAL,
    samples INTEGER, tyres TEXT, data BLOB
);
CREATE INDEX IF NOT EXISTS laps_session ON laps(session_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, session_id INTEGER, t REAL, lap INTEGER, pct REAL, kind TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS drivers (
    session_id INTEGER, car_idx INTEGER, name TEXT, num TEXT, cls TEXT, color TEXT, ir INTEGER,
    PRIMARY KEY (session_id, car_idx)
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY, session_id INTEGER, kind TEXT, t0 REAL, t1 REAL, n INTEGER, data BLOB
);
CREATE INDEX IF NOT EXISTS frames_session ON frames(session_id, kind, t0);
CREATE TABLE IF NOT EXISTS car_laps (
    session_id INTEGER, car_idx INTEGER, lap INTEGER, time REAL, position INTEGER,
    PRIMARY KEY (session_id, car_idx, lap)
);
"""


def is_network_path(path):
    """UNC paths and mapped network drives. SQLite's WAL mode isn't safe on those."""
    s = str(path)
    if s.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32" and len(s) >= 2 and s[1] == ":":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(f"{s[0]}:\\") == 4  # DRIVE_REMOTE
        except Exception:
            return False
    return False


def connect(path):
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA journal_mode={'DELETE' if is_network_path(path) else 'WAL'}")
    db.executescript(SCHEMA)
    return db


class Storage:
    """The data folder: history.db (or history-mock.db) plus the track maps its sessions use.
    The chosen folder is remembered in config.json next to server.py."""

    def __init__(self, root, cli_dir=None, mock=False):
        self.root = Path(root)
        self.config_path = self.root / "config.json"
        self.mock = mock
        self.cli_override = cli_dir is not None
        configured = None
        try:
            configured = json.loads(self.config_path.read_text()).get("data_dir")
        except (OSError, ValueError):
            pass
        self.dir = Path(cli_dir or configured or self.root).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self):
        return self.dir / ("history-mock.db" if self.mock else "history.db")

    @property
    def maps_dir(self):
        return self.dir / "maps"

    def info(self):
        return {"dir": str(self.dir), "file": self.db_path.name, "network": is_network_path(self.dir),
                "exists": self.db_path.exists()}

    def set_dir(self, new_dir, create=False):
        p = Path(new_dir).expanduser()
        if not p.is_absolute():
            raise ValueError("Use a full path, for example D:\\iRacing\\history")
        if not p.exists():
            if not create:
                raise ValueError("That folder doesn't exist")
            p.mkdir(parents=True)
        if not p.is_dir():
            raise ValueError("That path is a file, not a folder")
        probe = p / ".write-test"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError:
            raise ValueError("Can't write to that folder") from None
        self.dir = p
        try:
            cfg = json.loads(self.config_path.read_text()) if self.config_path.exists() else {}
        except (OSError, ValueError):
            cfg = {}
        cfg["data_dir"] = str(p)
        self.config_path.write_text(json.dumps(cfg, indent=2))


def browse(path, filename="history.db"):
    """Subfolders of `path` for the folder picker."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError("Not a folder")
    dirs = []
    try:
        for c in p.iterdir():
            try:
                if c.is_dir() and not c.name.startswith((".", "$")):
                    dirs.append(c.name)
            except OSError:
                pass
    except OSError:
        raise ValueError("Can't open that folder") from None
    drives = []
    if sys.platform == "win32":
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": sorted(dirs, key=str.lower), "drives": drives,
            "hasHistory": (p / filename).exists(), "network": is_network_path(p)}


def flag_name(bits):
    if not isinstance(bits, int):
        return None
    return next((name for mask, name in FLAGS if bits & mask), None)


def parse_length(s):
    """'3.85 km' / '2.39 mi' -> metres."""
    try:
        value, unit = str(s).split()[:2]
        return float(value) * (1609.344 if unit.startswith("mi") else 1000.0)
    except (ValueError, TypeError):
        return None


class Recorder:
    MIN_SAMPLES = 120  # don't keep fragments shorter than ~2 s
    FRAME_CHUNK = 10.0  # seconds of dashboard frames per stored block

    def __init__(self, path, maps_dir=None):
        self.db = connect(path)
        self.maps_dir = Path(maps_dir) if maps_dir else None
        self._close_orphans()
        self._switch_to = None
        self._map_src = None
        self.session_id = None
        self._sim_key = None
        self._frames = {"fast": [], "slow": []}
        self._frames_t0 = {}
        self._reset_state()

    def _reset_state(self):
        self.buf = None
        self._last_lc = None
        self._prev_pct = None
        self._pending = []         # (due, lap_id, measured_time)
        self._car_last_lc = {}
        self._car_pending = []     # (due, car_idx, lap)
        self._ev = {"flag": None, "inc": None, "pit": None, "pos": None, "state": None, "pit_fuel": None}

    def _close_orphans(self):
        """Sessions left open by a crash or a killed server: close them, drop the empty ones."""
        for (sid,) in self.db.execute("SELECT id FROM sessions WHERE ended IS NULL").fetchall():
            laps = self.db.execute("SELECT COUNT(*) FROM laps WHERE session_id=?", (sid,)).fetchone()[0]
            if laps:
                self.db.execute("UPDATE sessions SET ended=started WHERE id=?", (sid,))
            else:
                delete_session(self.db, sid)
        self.db.commit()

    # -- session lifecycle -------------------------------------------------------------
    def on_info(self, eng):
        """Called after the engine refreshes session info (~1 Hz)."""
        key = f"{eng.src.name}:{eng.session_key}"
        if key != self._sim_key:
            self.end_session()
            self._start_session(eng, key)
        rows = [(self.session_id, idx, d["name"], d["num"], d["cls"], d["clsColor"], d["ir"])
                for idx, d in eng.drivers.items() if not d["pace"] and not d["spec"]]
        self.db.executemany("INSERT OR REPLACE INTO drivers VALUES (?,?,?,?,?,?,?)", rows)
        self.db.commit()

    # -- data folder switching (requested by the web server, applied on the telemetry thread) --
    def request_switch(self, db_path, maps_dir):
        self._switch_to = (db_path, maps_dir)

    def apply_pending(self):
        target, self._switch_to = self._switch_to, None
        if target is None:
            return
        self.end_session()
        self.db.close()
        self.db = connect(target[0])
        self.maps_dir = Path(target[1])
        self._close_orphans()

    def _export_map(self):
        """Copy the session's track outline into the data folder so the folder is self-contained."""
        if not self._map_src or not self.maps_dir:
            return
        src = Path(self._map_src)
        dst = self.maps_dir / src.name
        try:
            if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size):
                self.maps_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        except OSError:
            pass

    def _start_session(self, eng, key):
        me = eng.drivers.get(eng.player_idx, {})
        cur = self.db.execute(
            "INSERT INTO sessions (started, source, sim_key, track_id, track, config, map_key, length_m,"
            " car, type, name, driver, car_idx) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), eng.src.name, key, eng.track.get("id"), eng.track.get("name"), eng.track.get("config"),
             eng.map.key, parse_length(eng.track.get("length")), eng.car.get("name"),
             eng.session.get("type"), eng.session.get("name"), me.get("name"), eng.player_idx))
        self.db.commit()
        self.session_id = cur.lastrowid
        self._sim_key = key
        self._map_src = eng.map._path() if eng.map.key else None
        self._export_map()
        self._reset_state()

    def end_session(self):
        if self.session_id is None:
            return
        self._flush_partial()
        for kind in self._frames:
            self._flush_frames(kind)
        self._export_map()
        laps = self.db.execute("SELECT COUNT(*) FROM laps WHERE session_id=?", (self.session_id,)).fetchone()[0]
        if laps:
            self.db.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), self.session_id))
            self.db.commit()
        else:
            delete_session(self.db, self.session_id)  # nothing driven: don't clutter the list
        self.session_id = self._sim_key = None
        self._reset_state()

    # -- dashboard frames, for replaying the Live view -----------------------------------
    def frame(self, kind, t, msg):
        """Store a broadcast frame (already JSON) stamped with SessionTime."""
        if self.session_id is None or not isinstance(t, (int, float)):
            return
        buf = self._frames[kind]
        if buf and (t < self._frames_t0[kind] or t - self._frames_t0[kind] >= self.FRAME_CHUNK):
            self._flush_frames(kind)
        if not buf:
            self._frames_t0[kind] = t
        buf.append((t, f"[{t:.3f},{msg}]"))

    def _flush_frames(self, kind):
        buf = self._frames[kind]
        if not buf or self.session_id is None:
            buf.clear()
            return
        blob = zlib.compress(",".join(item for _, item in buf).encode(), 6)
        self.db.execute("INSERT INTO frames (session_id, kind, t0, t1, n, data) VALUES (?,?,?,?,?,?)",
                        (self.session_id, kind, buf[0][0], buf[-1][0], len(buf), blob))
        self.db.commit()
        buf.clear()

    # -- per tick ------------------------------------------------------------------------
    def step(self, eng, now):
        if self.session_id is None:
            return
        g = eng.g
        t, pct, lc = g("SessionTime"), g("LapDistPct"), g("LapCompleted")
        self._events(eng, t, pct, lc)
        self._field_laps(eng, now)
        self._resolve(eng, now)

        live = bool(g("IsOnTrack")) and not g("IsReplayPlaying")
        if not live or t is None or pct is None or pct < 0 or lc is None:
            self._flush_partial()
            self._last_lc, self._prev_pct = lc, None
            return

        prev = self._prev_pct
        self._prev_pct = pct
        if prev is not None and self.buf is not None:
            d = pct - prev
            wrapped = prev > 0.9 and pct < 0.1
            if not wrapped and (d < -0.02 or d > 0.1):  # reset / tow / teleport
                self._flush_partial()

        if self._last_lc is not None and lc != self._last_lc:
            if lc == self._last_lc + 1 and self.buf is not None:
                self._finish_lap(eng, t, partial=self.buf["partial"])
            else:
                self._flush_partial()
            self.buf = self._new_buf(eng, t, lc, partial=False)
        elif self.buf is None:
            # joined mid-lap (or after a reset): keep it, flagged as partial
            self.buf = self._new_buf(eng, t, lc, partial=True)
        self._last_lc = lc

        cols = self.buf["cols"]
        cols["t"].append(round(t - self.buf["t0"], 3))
        for name, (var, dp) in CHANNELS.items():
            v = g(var)
            if name == "clu" and v is not None:
                v = 1 - v
            if isinstance(v, bool):
                v = int(v)
            cols[name].append(round(v, dp) if isinstance(v, float) else v)

    def _new_buf(self, eng, t, lc, partial):
        g = eng.g
        return {"t0": t, "lap": (lc or 0) + 1, "partial": partial, "fuel0": g("FuelLevel"),
                "inc0": g("PlayerCarMyIncidentCount"), "pit": False,
                "cols": {name: [] for name in ["t", *CHANNELS]}}

    def _flush_partial(self):
        if self.buf is not None and len(self.buf["cols"]["t"]) >= self.MIN_SAMPLES:
            self._store(self.buf, None, partial=True, extra={})
        self.buf = None

    def _finish_lap(self, eng, t, partial):
        g = eng.g
        buf = self.buf
        measured = t - buf["t0"]
        fuel1, inc1 = g("FuelLevel"), g("PlayerCarMyIncidentCount")
        pit = any(buf["cols"]["pit"])
        used = buf["fuel0"] - fuel1 if None not in (buf["fuel0"], fuel1) else None
        extra = {
            "fuel_used": round(used, 3) if used is not None and used >= 0 and not pit else None,
            "pit": int(pit),
            "incidents": inc1 - buf["inc0"] if None not in (inc1, buf["inc0"]) else None,
            "position": g("PlayerCarPosition"), "class_position": g("PlayerCarClassPosition"),
            "air": g("AirTemp"), "track_temp": g("TrackTempCrew"),
            "tyres": json.dumps(eng._tyres()),
        }
        lap_id = self._store(buf, None if partial else round(measured, 3), partial, extra)
        if not partial:
            self._pending.append((time.monotonic() + 2.0, lap_id))

    def _store(self, buf, lap_time, partial, extra):
        cols = buf["cols"]
        # samples recorded either side of the line belong to this lap, not the far end of it
        pcts = cols["pct"]
        n = len(pcts)
        for i in range(min(n, 30)):
            if pcts[i] is not None and pcts[i] > 0.9:
                pcts[i] = round(pcts[i] - 1, 5)
        for i in range(max(0, n - 30), n):
            if pcts[i] is not None and pcts[i] < 0.1:
                pcts[i] = round(pcts[i] + 1, 5)
        cols = {k: v for k, v in cols.items() if any(x is not None for x in v)}
        speeds = [v for v in cols.get("speed", []) if v is not None]
        blob = zlib.compress(json.dumps(cols, separators=(",", ":")).encode(), 6)
        cur = self.db.execute(
            "INSERT INTO laps (session_id, lap, time, official, partial, fuel_used, pit, incidents, position,"
            " class_position, max_speed, air, track_temp, started_at, samples, tyres, data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, buf["lap"], lap_time, 0, int(partial), extra.get("fuel_used"),
             extra.get("pit", int(any(cols.get("pit", [])))), extra.get("incidents"), extra.get("position"),
             extra.get("class_position"), max(speeds) if speeds else None, extra.get("air"),
             extra.get("track_temp"), buf["t0"], n, extra.get("tyres"), blob))
        self.db.commit()
        return cur.lastrowid

    def _resolve(self, eng, now):
        """Swap measured lap times for iRacing's official ones once they're published."""
        while self._pending and time.monotonic() >= self._pending[0][0]:
            _, lap_id = self._pending.pop(0)
            llt = eng.g("LapLastLapTime")
            if isinstance(llt, (int, float)) and llt > 0:
                self.db.execute("UPDATE laps SET time=?, official=1 WHERE id=?", (round(llt, 3), lap_id))
                self.db.commit()

    def _event(self, t, lap, pct, kind, data=None):
        self.db.execute("INSERT INTO events (session_id, t, lap, pct, kind, data) VALUES (?,?,?,?,?,?)",
                        (self.session_id, t, lap, round(pct, 4) if isinstance(pct, float) else pct, kind,
                         json.dumps(data) if data is not None else None))
        self.db.commit()

    def _events(self, eng, t, pct, lc):
        g, ev = eng.g, self._ev
        lap = (lc or 0) + 1 if lc is not None else None

        flag = flag_name(g("SessionFlags"))
        if flag != ev["flag"]:
            if ev["flag"] is not None or flag is not None:
                self._event(t, lap, pct, "flag", {"flag": flag})
            ev["flag"] = flag

        inc = g("PlayerCarMyIncidentCount")
        if isinstance(inc, int):
            if ev["inc"] is not None and inc > ev["inc"]:
                self._event(t, lap, pct, "incident", {"added": inc - ev["inc"], "total": inc})
            ev["inc"] = inc

        on_pit = bool(g("OnPitRoad"))
        if ev["pit"] is not None and on_pit != ev["pit"]:
            fuel = g("FuelLevel")
            if on_pit:
                ev["pit_fuel"] = fuel
                self._event(t, lap, pct, "pit_in", {"fuel": fuel})
            else:
                added = (fuel - ev["pit_fuel"]) if None not in (fuel, ev["pit_fuel"]) else None
                self._event(t, lap, pct, "pit_out", {"fuel": fuel, "added": round(added, 2) if added else 0})
        ev["pit"] = on_pit

        if (eng.session.get("type") or "").lower().startswith("race"):
            pos = g("PlayerCarPosition")
            if isinstance(pos, int) and pos > 0 and pos != ev["pos"]:
                if ev["pos"] is not None:
                    self._event(t, lap, pct, "position", {"from": ev["pos"], "to": pos})
                ev["pos"] = pos

        state = g("SessionState")
        if state != ev["state"]:
            self._event(t, lap, pct, "state", {"state": state})
            ev["state"] = state

    def _field_laps(self, eng, now):
        lapc = eng.arr("CarIdxLapCompleted")
        mono = time.monotonic()
        for idx in eng.drivers:
            lc = lapc[idx] if idx < len(lapc) else None
            if not isinstance(lc, int) or lc < 0:
                continue
            last = self._car_last_lc.get(idx)
            if last is not None and lc == last + 1:
                self._car_pending.append((mono + 2.0, idx, lc))
            self._car_last_lc[idx] = lc
        if self._car_pending and mono >= self._car_pending[0][0]:
            last_t, pos = eng.arr("CarIdxLastLapTime"), eng.arr("CarIdxPosition")
            rows = []
            while self._car_pending and mono >= self._car_pending[0][0]:
                _, idx, lap = self._car_pending.pop(0)
                lt = last_t[idx] if idx < len(last_t) else None
                rows.append((self.session_id, idx, lap, round(lt, 3) if isinstance(lt, float) and lt > 0 else None,
                             pos[idx] if idx < len(pos) else None))
            self.db.executemany("INSERT OR REPLACE INTO car_laps VALUES (?,?,?,?,?)", rows)
            self.db.commit()


# ---------------------------------------------------------------------------
# Read side (used by the web server)
# ---------------------------------------------------------------------------

def list_sessions(db):
    rows = db.execute(
        "SELECT s.*, COUNT(l.id) AS laps,"
        " MIN(CASE WHEN l.partial=0 AND l.pit=0 AND l.time > 0 THEN l.time END) AS best,"
        " (SELECT COUNT(*) FROM frames f WHERE f.session_id = s.id) AS frames"
        " FROM sessions s LEFT JOIN laps l ON l.session_id = s.id"
        " GROUP BY s.id ORDER BY s.started DESC").fetchall()
    return [dict(r) for r in rows]


def session_detail(db, sid, maps_dirs):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if s is None:
        return None
    s = dict(s)
    laps = [dict(r) for r in db.execute(
        "SELECT id, lap, time, official, partial, fuel_used, pit, incidents, position, class_position,"
        " max_speed, air, track_temp, started_at, samples, tyres FROM laps WHERE session_id=? ORDER BY id",
        (sid,))]
    for lap in laps:
        lap["tyres"] = json.loads(lap["tyres"]) if lap["tyres"] else None
    events = [dict(r) for r in db.execute(
        "SELECT t, lap, pct, kind, data FROM events WHERE session_id=? ORDER BY id", (sid,))]
    for e in events:
        e["data"] = json.loads(e["data"]) if e["data"] else None
    drivers = {r["car_idx"]: dict(r) for r in db.execute("SELECT * FROM drivers WHERE session_id=?", (sid,))}
    field = [dict(r) for r in db.execute(
        "SELECT car_idx, lap, time, position FROM car_laps WHERE session_id=? AND time IS NOT NULL", (sid,))]
    points = None
    if s.get("map_key"):
        import re
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", s["map_key"]) + ".json"
        for d in maps_dirs:
            try:
                points = json.loads((Path(d) / name).read_text())["points"]
                break
            except (OSError, ValueError, KeyError):
                continue
    return {"session": s, "laps": laps, "events": events, "drivers": drivers, "field": field, "map": points}


def lap_blob(db, lap_id, decompress=True):
    """Lap telemetry as JSON bytes; with decompress=False, the stored zlib stream
    (valid as HTTP `Content-Encoding: deflate`)."""
    row = db.execute("SELECT data FROM laps WHERE id=?", (lap_id,)).fetchone()
    if not row or not row["data"]:
        return None
    return zlib.decompress(row["data"]) if decompress else row["data"]


def delete_session(db, sid):
    for table in ("laps", "events", "drivers", "car_laps", "frames"):
        db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
    db.execute("DELETE FROM sessions WHERE id=?", (sid,))
    db.commit()


def timeline(db, sid):
    """Time span of recorded frames plus lap starts and events, for the replay scrubber."""
    span = db.execute("SELECT MIN(t0), MAX(t1), COUNT(*) FROM frames WHERE session_id=? AND kind='fast'",
                      (sid,)).fetchone()
    if not span or not span[2]:
        return None
    laps = [{"lap": r["lap"], "t": r["started_at"], "time": r["time"], "partial": r["partial"]}
            for r in db.execute("SELECT lap, started_at, time, partial FROM laps WHERE session_id=? ORDER BY started_at",
                                (sid,))]
    events = [{"t": r["t"], "kind": r["kind"], "data": json.loads(r["data"]) if r["data"] else None}
              for r in db.execute("SELECT t, kind, data FROM events WHERE session_id=? ORDER BY t", (sid,))]
    return {"t0": span[0], "t1": span[1], "laps": laps, "events": events}


def frames_json(db, sid, a, b):
    """Recorded fast and slow frames overlapping [a, b] as JSON bytes:
    {"fast": [[t, frame], ...], "slow": [...]}. Whole stored blocks are returned."""
    out = []
    for kind in ("fast", "slow"):
        rows = db.execute("SELECT data FROM frames WHERE session_id=? AND kind=? AND t1>=? AND t0<=? ORDER BY t0",
                          (sid, kind, a, b)).fetchall()
        out.append(f'"{kind}":[' + ",".join(zlib.decompress(r["data"]).decode() for r in rows) + "]")
    return ("{" + ",".join(out) + "}").encode()
