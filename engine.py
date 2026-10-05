"""Turns raw telemetry into the frames the dashboard renders.

fast frame  (~20 Hz): driving inputs, lap timing, car positions for the map
slow frame  (~4 Hz):  fuel strategy, standings, relative, tyres, systems, lap history
map frame   (on change): track outline, built from one clean lap and cached in maps/
"""
import bisect
import json
import math
import re
import time
from collections import deque
from pathlib import Path

UNLIMITED_LAPS = 32767
UNLIMITED_TIME = 3 * 86400
CORNERS = ("LF", "RF", "LR", "RR")
SURFACE_OFF_TRACK, SURFACE_ON_TRACK = 0, 3


def rnd(v, n=2):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, n)
    return v


def at(seq, i, default=None):
    return seq[i] if 0 <= i < len(seq) else default


def lap_or_none(v):
    return round(v, 3) if isinstance(v, (int, float)) and v > 0 else None


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def class_color(v):
    if isinstance(v, str):
        try:
            v = int(v, 16)
        except ValueError:
            return "#8a919c"
    if isinstance(v, int):
        return f"#{v & 0xFFFFFF:06x}"
    return "#8a919c"


class TrackMap:
    """Dead-reckons the track outline from Speed + Yaw over one clean lap.

    iRacing doesn't expose world coordinates, so we integrate the car's heading
    and speed, close the loop by spreading the drift error over the lap, then
    resample to evenly spaced LapDistPct points. Saved per track/config.
    """

    SAMPLES = 500

    def __init__(self, maps_dir):
        self.dir = Path(maps_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = None
        self.name = ""
        self.points = None
        self.version = 0
        self.progress = 0.0
        self._rec = None
        self._prev = None

    def _path(self):
        return self.dir / (re.sub(r"[^A-Za-z0-9_-]+", "_", self.key) + ".json")

    def set_track(self, key, name):
        if key == self.key:
            return
        self.key, self.name = key, name
        self.points, self._rec, self._prev, self.progress = None, None, None, 0.0
        try:
            self.points = json.loads(self._path().read_text())["points"]
        except (OSError, ValueError, KeyError):
            pass
        self.version += 1

    def frame(self):
        return {"version": self.version, "name": self.name, "points": self.points}

    def feed(self, t, pct, speed, yaw, valid):
        if self.points is not None or self.key is None:
            return
        if None in (t, pct, speed, yaw) or pct < 0 or not valid:
            self._prev, self._rec, self.progress = None, None, 0.0
            return
        prev, self._prev = self._prev, (t, pct)
        if prev is None:
            return
        dt = t - prev[0]
        if dt <= 0:
            return  # paused
        if dt > 0.5:
            self._rec, self.progress = None, 0.0
            return
        crossed = prev[1] > 0.9 and pct < 0.1
        rec = self._rec
        if rec is None:
            if crossed:
                self._rec = {"x": 0.0, "y": 0.0, "pts": [(pct, 0.0, 0.0)]}
            return
        d = pct - prev[1]
        if not crossed and (d < -0.01 or d > 0.05):  # reset / tow / teleport
            self._rec, self.progress = None, 0.0
            return
        rec["x"] += speed * math.cos(yaw) * dt
        rec["y"] += speed * math.sin(yaw) * dt
        if crossed:
            self._finish(rec["pts"], (1.0 + pct, rec["x"], rec["y"]))
            return
        if pct - rec["pts"][-1][0] >= 0.0005:
            rec["pts"].append((pct, rec["x"], rec["y"]))
        self.progress = pct

    def _finish(self, pts, end):
        self._rec = None
        if len(pts) < 100:
            return
        pts = pts + [end]
        p0, span = pts[0][0], end[0] - pts[0][0]
        ex, ey = end[1] - pts[0][1], end[2] - pts[0][2]
        length = sum(math.dist(a[1:], b[1:]) for a, b in zip(pts, pts[1:]))
        if length <= 0 or math.hypot(ex, ey) > 0.15 * length:
            self.progress = 0.0
            return  # too much drift, try again next lap
        fixed = [(p, x - ex * (p - p0) / span, y - ey * (p - p0) / span) for p, x, y in pts]
        keys = [p for p, _, _ in fixed]
        out = []
        for k in range(self.SAMPLES):
            u = k / self.SAMPLES
            u = min(max(u if u >= p0 else u + 1.0, p0), end[0])
            j = min(max(bisect.bisect_right(keys, u) - 1, 0), len(fixed) - 2)
            (pa, xa, ya), (pb, xb, yb) = fixed[j], fixed[j + 1]
            f = (u - pa) / (pb - pa) if pb > pa else 0.0
            out.append((xa + (xb - xa) * f, ya + (yb - ya) * f))
        xs, ys = [p[0] for p in out], [p[1] for p in out]
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        s = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 or 1.0
        self.points = [[round((x - cx) / s, 4), round((y - cy) / s, 4)] for x, y in out]
        self.progress = 1.0
        self.version += 1
        try:
            self._path().write_text(json.dumps({"key": self.key, "name": self.name, "points": self.points}))
        except OSError:
            pass


class Engine:
    def __init__(self, source, maps_dir):
        self.src = source
        self.map = TrackMap(maps_dir)
        self.connected = False
        self._next_connect = 0.0
        self._next_info = 0.0
        self.fuel_samples = deque(maxlen=5)
        self.player_idx = 0
        self.drivers = {}
        self.car, self.track, self.session = {}, {}, {}
        self.incident_limit = None
        self._track_key = self._session_key = None
        self._reset_session()

    # -- bookkeeping -----------------------------------------------------------
    def _reset_session(self):
        self.laps = deque(maxlen=100)
        self.lap_times = deque(maxlen=5)
        self._pending = []
        self._reset_lap_tracker()

    def _reset_lap_tracker(self):
        self._last_lc = None
        self._lap_start_fuel = None
        self._pit_this_lap = False
        self._partial_lap = True
        self._was_on_pit = None
        self.stint = None

    def g(self, name):
        return self.src.get(name)

    def arr(self, name):
        v = self.src.get(name)
        return v if isinstance(v, (list, tuple)) else []

    def step(self):
        """Poll the source once. Returns True while connected."""
        now = time.monotonic()
        if not self.connected:
            if now < self._next_connect:
                return False
            self._next_connect = now + 2.0
            if not self.src.connect():
                return False
            self.connected = True
            self._next_info = 0.0
            self._reset_lap_tracker()
        elif not self.src.alive():
            self.src.disconnect()
            self.connected = False
            return False
        self.src.freeze()
        if now >= self._next_info:
            self._next_info = now + 1.0
            self._refresh_info()
        self._track_laps(now)
        surface = self.g("PlayerTrackSurface")
        valid = (bool(self.g("IsOnTrack")) and not self.g("IsReplayPlaying") and not self.g("OnPitRoad")
                 and surface in (None, SURFACE_OFF_TRACK, SURFACE_ON_TRACK))
        self.map.feed(self.g("SessionTime"), self.g("LapDistPct"), self.g("Speed"), self.g("Yaw"), valid)
        return True

    def _refresh_info(self):
        di = self.g("DriverInfo") or {}
        wi = self.g("WeekendInfo") or {}
        si = self.g("SessionInfo") or {}
        self.player_idx = di.get("DriverCarIdx", self.player_idx)
        drivers = {}
        for d in di.get("Drivers") or []:
            idx = d.get("CarIdx")
            if idx is None:
                continue
            drivers[idx] = {
                "name": d.get("UserName") or "", "team": d.get("TeamName") or "",
                "num": str(d.get("CarNumber") or ""),
                "car": d.get("CarScreenNameShort") or d.get("CarScreenName") or "",
                "cls": d.get("CarClassShortName") or "", "clsId": d.get("CarClassID") or 0,
                "clsColor": class_color(d.get("CarClassColor")),
                "ir": d.get("IRating") or 0, "lic": d.get("LicString") or "",
                "est": d.get("CarClassEstLapTime") or 0,
                "pace": bool(d.get("CarIsPaceCar")), "spec": bool(d.get("IsSpectator")),
            }
        self.drivers = drivers
        me = drivers.get(self.player_idx, {})
        self.car = {
            "name": me.get("car", ""),
            "slFirst": di.get("DriverCarSLFirstRPM"), "slShift": di.get("DriverCarSLShiftRPM"),
            "slBlink": di.get("DriverCarSLBlinkRPM"), "redline": di.get("DriverCarRedLine"),
            "tank": rnd((di.get("DriverCarFuelMaxLtr") or 0) * (di.get("DriverCarMaxFuelPct") or 1), 1),
        }
        self.track = {
            "name": wi.get("TrackDisplayName") or wi.get("TrackName") or "",
            "config": wi.get("TrackConfigName") or "", "length": wi.get("TrackLength") or "",
        }
        track_key = f"{wi.get('TrackID', 'x')}_{wi.get('TrackConfigName') or ''}"
        if track_key != self._track_key:
            self._track_key = track_key
            self.fuel_samples.clear()
        self.map.set_track(track_key, " - ".join(filter(None, [self.track["name"], self.track["config"]])))

        limit = (wi.get("WeekendOptions") or {}).get("IncidentLimit")
        try:
            self.incident_limit = int(limit)
        except (TypeError, ValueError):
            self.incident_limit = None

        snum = self.g("SessionNum")
        sess = next((s for s in si.get("Sessions") or [] if s.get("SessionNum") == snum), {})
        self.session = {"type": sess.get("SessionType") or "", "name": sess.get("SessionName") or ""}
        key = (wi.get("SubSessionID"), wi.get("SessionID"), snum)
        if key != self._session_key:
            if self._session_key is not None:
                self._reset_session()
            self._session_key = key

    def _track_laps(self, now):
        lc, fuel, t = self.g("LapCompleted"), self.g("FuelLevel"), self.g("SessionTime")
        on_pit = bool(self.g("OnPitRoad"))
        if on_pit:
            self._pit_this_lap = True
        if self._was_on_pit is None or (self._was_on_pit and not on_pit):
            self.stint = (lc, t)
        self._was_on_pit = on_pit

        if lc is not None:
            if self._last_lc is None or lc < self._last_lc:
                self._last_lc, self._lap_start_fuel, self._pit_this_lap = lc, fuel, on_pit
                self._partial_lap = True
            elif lc > self._last_lc:
                if lc == self._last_lc + 1:
                    used = None
                    if fuel is not None and self._lap_start_fuel is not None:
                        used = self._lap_start_fuel - fuel
                    clean = not self._pit_this_lap
                    entry = {"lap": lc, "time": None, "pit": self._pit_this_lap,
                             "fuel": round(used, 3) if used is not None and used >= 0 and not self._partial_lap else None}
                    self.laps.append(entry)
                    self._pending.append((now + 2.0, entry, clean))
                    if clean and not self._partial_lap and used is not None and used > 0:
                        self.fuel_samples.append(used)
                self._last_lc, self._lap_start_fuel, self._pit_this_lap = lc, fuel, on_pit
                self._partial_lap = False

        # iRacing updates LapLastLapTime shortly after the line, so resolve a bit later
        while self._pending and now >= self._pending[0][0]:
            _, entry, clean = self._pending.pop(0)
            llt = self.g("LapLastLapTime")
            if isinstance(llt, (int, float)) and llt > 0:
                entry["time"] = round(llt, 3)
                if clean:
                    self.lap_times.append(llt)

    # -- frames -----------------------------------------------------------------
    def fast_frame(self):
        if not self.connected:
            return {"connected": False}
        g = self.g
        pct, surf, pit = self.arr("CarIdxLapDistPct"), self.arr("CarIdxTrackSurface"), self.arr("CarIdxOnPitRoad")
        cars = []
        for idx, d in self.drivers.items():
            p = at(pct, idx, -1)
            if d["spec"] or p is None or p < 0 or at(surf, idx, -1) == -1:
                continue
            cars.append([idx, round(p, 4), 1 if at(pit, idx) else 0, 1 if d["pace"] else 0])
        clutch = g("Clutch")
        return {
            "connected": True, "me": self.player_idx, "cars": cars,
            "onTrack": g("IsOnTrack"), "replay": g("IsReplayPlaying"),
            "speed": rnd(g("Speed"), 2), "rpm": rnd(g("RPM"), 0), "gear": g("Gear"),
            "thr": rnd(g("Throttle"), 3), "brk": rnd(g("Brake"), 3),
            "clu": rnd(1 - clutch, 3) if clutch is not None else None,
            "steer": rnd(g("SteeringWheelAngle"), 3),
            "lap": g("Lap"), "pct": rnd(g("LapDistPct"), 4),
            "cur": rnd(g("LapCurrentLapTime"), 3), "last": lap_or_none(g("LapLastLapTime")),
            "best": lap_or_none(g("LapBestLapTime")),
            "delta": rnd(g("LapDeltaToBestLap"), 3), "deltaOk": g("LapDeltaToBestLap_OK"),
            "sdelta": rnd(g("LapDeltaToSessionBestLap"), 3), "sdeltaOk": g("LapDeltaToSessionBestLap_OK"),
            "pos": g("PlayerCarPosition"), "cpos": g("PlayerCarClassPosition"),
            "fuel": rnd(g("FuelLevel"), 2), "fuelPct": rnd(g("FuelLevelPct"), 4),
            "flags": g("SessionFlags"), "warn": g("EngineWarnings"), "pit": g("OnPitRoad"),
            "tRem": rnd(g("SessionTimeRemain"), 1), "lRem": g("SessionLapsRemainEx"),
            "sTime": rnd(g("SessionTime"), 1),
        }

    def slow_frame(self):
        if not self.connected:
            return {"connected": False, "source": self.src.name}
        g = self.g
        return {
            "connected": True, "source": self.src.name, "me": self.player_idx,
            "track": self.track, "session": {**self.session, "state": g("SessionState")}, "car": self.car,
            "fuel": self._fuel(),
            "env": {"air": rnd(g("AirTemp"), 1), "track": rnd(g("TrackTempCrew"), 1), "wet": g("TrackWetness"),
                    "wind": rnd(g("WindVel"), 1), "windDir": rnd(g("WindDir"), 2),
                    "precip": rnd(g("Precipitation"), 2)},
            "inc": {"me": g("PlayerCarMyIncidentCount"), "team": g("PlayerCarTeamIncidentCount"),
                    "limit": self.incident_limit},
            "stint": self._stint(),
            "tyres": self._tyres(),
            "sys": {"water": rnd(g("WaterTemp"), 1), "oil": rnd(g("OilTemp"), 1), "oilP": rnd(g("OilPress"), 2),
                    "fuelP": rnd(g("FuelPress"), 2), "volt": rnd(g("Voltage"), 1),
                    "bias": rnd(g("dcBrakeBias"), 2), "abs": rnd(g("dcABS"), 0),
                    "tc": rnd(g("dcTractionControl"), 0), "tc2": rnd(g("dcTractionControl2"), 0),
                    "warn": g("EngineWarnings")},
            "pitSv": {"flags": g("PitSvFlags"), "fuel": rnd(g("PitSvFuel"), 1),
                      "repair": rnd(g("PitRepairLeft"), 1), "optRepair": rnd(g("PitOptRepairLeft"), 1),
                      "inStall": g("PlayerCarInPitStall")},
            "standings": self._standings(),
            "relative": self._relative(),
            "laps": list(self.laps)[-30:],
            "mapRec": None if self.map.points is not None else round(self.map.progress, 3),
        }

    def _avg_lap(self):
        return (mean(self.lap_times) or lap_or_none(self.g("LapBestLapTime"))
                or (self.drivers.get(self.player_idx) or {}).get("est") or None)

    def _fuel(self):
        g = self.g
        fuel = g("FuelLevel")
        pct = g("LapDistPct")
        pct = pct if isinstance(pct, (int, float)) and pct >= 0 else 0.0
        avg = mean(self.fuel_samples)
        avg_lap = self._avg_lap()
        laps_rem, t_rem = g("SessionLapsRemainEx"), g("SessionTimeRemain")

        laps_to_go = None
        if isinstance(laps_rem, int) and 0 <= laps_rem < UNLIMITED_LAPS:
            laps_to_go = max(0.0, laps_rem - pct)
        elif isinstance(t_rem, (int, float)) and 0 <= t_rem < UNLIMITED_TIME and avg_lap:
            rest = t_rem - (1 - pct) * avg_lap
            laps_to_go = (1 - pct) + (math.ceil(rest / avg_lap) if rest > 0 else 0)

        out = {"avg": rnd(avg, 3), "last": self.fuel_samples[-1] if self.fuel_samples else None,
               "samples": len(self.fuel_samples), "lapsToGo": rnd(laps_to_go, 2), "avgLap": rnd(avg_lap, 3),
               "lapsInTank": None, "toFinish": None, "toAdd": None, "stops": None}
        if out["last"] is not None:
            out["last"] = round(out["last"], 3)
        if avg and fuel is not None:
            out["lapsInTank"] = round(fuel / avg, 2)
            if laps_to_go is not None:
                need = laps_to_go * avg
                add = max(0.0, need - fuel)
                tank = self.car.get("tank") or 0
                out["toFinish"] = round(need, 2)
                out["toAdd"] = round(add, 2)
                out["stops"] = math.ceil(add / tank) if tank > 0 and add > 0 else 0
        return out

    def _stint(self):
        if not self.stint:
            return None
        lap0, t0 = self.stint
        lc, t = self.g("LapCompleted"), self.g("SessionTime")
        return {"laps": lc - lap0 if lc is not None and lap0 is not None else None,
                "time": round(t - t0) if t is not None and t0 is not None else None}

    def _tyres(self):
        out = {}
        for c in CORNERS:
            temps = [self.g(f"{c}tempC{s}") for s in "LMR"]
            if all(v is None for v in temps):
                temps = [self.g(f"{c}temp{s}") for s in "LMR"]
            out[c] = {"t": [rnd(v, 1) for v in temps],
                      "w": [rnd(self.g(f"{c}wear{s}"), 3) for s in "LMR"],
                      "p": rnd(self.g(f"{c}pressure"), 1), "cp": rnd(self.g(f"{c}coldPressure"), 1)}
        return out

    def _driver_fields(self, idx):
        d = self.drivers.get(idx, {})
        return {"num": d.get("num", ""), "name": d.get("name", ""), "cls": d.get("cls", ""),
                "clsColor": d.get("clsColor", "#8a919c"), "ir": d.get("ir", 0), "lic": d.get("lic", "")}

    def _standings(self):
        A = self.arr
        pos, cpos, lapc, pct = A("CarIdxPosition"), A("CarIdxClassPosition"), A("CarIdxLapCompleted"), A("CarIdxLapDistPct")
        last, best, f2, pit, surf = A("CarIdxLastLapTime"), A("CarIdxBestLapTime"), A("CarIdxF2Time"), A("CarIdxOnPitRoad"), A("CarIdxTrackSurface")
        race = self.session.get("type", "").lower().startswith("race")
        rows = []
        for idx, d in self.drivers.items():
            if d["pace"] or d["spec"]:
                continue
            p = at(pct, idx, -1)
            rows.append({
                "idx": idx, **self._driver_fields(idx),
                "pos": at(pos, idx, 0) or 0, "cpos": at(cpos, idx, 0) or 0,
                "last": lap_or_none(at(last, idx)), "best": lap_or_none(at(best, idx)),
                "pit": bool(at(pit, idx)), "out": at(surf, idx, -1) == -1,
                "_tot": (at(lapc, idx, 0) or 0) + (p if p and p > 0 else 0), "_f2": at(f2, idx),
                "gap": None, "down": 0, "int": None,
            })
        rows.sort(key=lambda r: (r["pos"] <= 0, r["pos"] if r["pos"] > 0 else 0, r["best"] or 1e9))
        if rows:
            if race:
                leader = next((r for r in rows if r["pos"] == 1), rows[0])
                for r in rows:
                    down = int(leader["_tot"] - r["_tot"])
                    r["down"] = down if down >= 1 else 0
                    f = r["_f2"]
                    r["gap"] = round(f, 2) if isinstance(f, (int, float)) and f > 0 and r is not leader else (0.0 if r is leader else None)
            else:
                fastest = min((r["best"] for r in rows if r["best"]), default=None)
                for r in rows:
                    r["gap"] = round(r["best"] - fastest, 3) if r["best"] and fastest else None
            prev = None
            for r in rows:
                if prev is not None and r["gap"] is not None and prev["gap"] is not None and not r["down"]:
                    r["int"] = round(r["gap"] - prev["gap"], 3)
                prev = r
        for r in rows:
            del r["_tot"], r["_f2"]
        return rows

    def _relative(self, n=4):
        me = self.player_idx
        pct, est, surf = self.arr("CarIdxLapDistPct"), self.arr("CarIdxEstTime"), self.arr("CarIdxTrackSurface")
        lapc, pos, pit = self.arr("CarIdxLapCompleted"), self.arr("CarIdxPosition"), self.arr("CarIdxOnPitRoad")
        my_pct = at(pct, me, -1)
        if my_pct is None or my_pct < 0:
            return []
        my_est = at(est, me, 0) or 0
        my_tot = (at(lapc, me, 0) or 0) + my_pct
        lap_len = (self.drivers.get(me) or {}).get("est") or self._avg_lap() or 90.0
        rows = []
        for idx, d in self.drivers.items():
            p = at(pct, idx, -1)
            if d["spec"] or d["pace"] or p is None or p < 0 or at(surf, idx, -1) == -1:
                continue
            dp = p - my_pct
            if dp > 0.5:
                dp -= 1
            elif dp < -0.5:
                dp += 1
            gap = (at(est, idx, 0) or 0) - my_est
            if dp > 0 and gap < 0:
                gap += lap_len
            elif dp < 0 and gap > 0:
                gap -= lap_len
            if idx == me:
                dp = gap = 0.0
            lap_diff = (at(lapc, idx, 0) or 0) + p - my_tot
            rows.append({"idx": idx, **self._driver_fields(idx), "pos": at(pos, idx, 0) or 0,
                         "dp": dp, "gap": round(gap, 2), "pit": bool(at(pit, idx)),
                         "lap": 1 if lap_diff > 0.5 else (-1 if lap_diff < -0.5 else 0)})
        me_row = [r for r in rows if r["idx"] == me]
        ahead = sorted((r for r in rows if r["idx"] != me and r["dp"] > 0), key=lambda r: r["dp"])[:n]
        behind = sorted((r for r in rows if r["idx"] != me and r["dp"] <= 0), key=lambda r: -r["dp"])[:n]
        out = ahead[::-1] + me_row + behind
        for r in out:
            del r["dp"]
        return out
