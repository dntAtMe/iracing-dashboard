"""Telemetry sources.

Both sources expose the same tiny interface so the Engine doesn't care where data
comes from:

    connect() -> bool      try to attach; True when data is available
    alive() -> bool        still attached?
    disconnect()
    freeze()               snapshot the latest telemetry sample
    get(name)              telemetry variable or session-info section (None if missing)
"""
import bisect
import math
import random
import time


class IRacingSource:
    """Live data from iRacing's shared memory via pyirsdk (Windows only)."""

    name = "iracing"

    def __init__(self):
        try:
            import irsdk  # provided by the `pyirsdk` package
        except ImportError as e:
            raise SystemExit("pyirsdk not installed - run: pip install -r requirements.txt") from e
        self._ir = irsdk.IRSDK()
        self._started = False
        self._vars = frozenset()

    @staticmethod
    def _sim_running():
        # Same check pyirsdk does, minus its console spam while iRacing is closed.
        from urllib import request
        try:
            with request.urlopen("http://127.0.0.1:32034/get_sim_status?object=simStatus", timeout=0.5) as r:
                return "running:1" in r.read().decode("utf-8", "replace")
        except Exception:
            return False

    def connect(self):
        if not self._sim_running():
            return False
        try:
            if self._started:
                self._ir.shutdown()
            self._started = bool(self._ir.startup())
            self._vars = frozenset(self._ir.var_headers_names or []) if self._started else frozenset()
        except Exception:
            self._started = False
        return self.alive()

    def alive(self):
        try:
            return self._started and self._ir.is_initialized and self._ir.is_connected
        except Exception:
            return False

    def disconnect(self):
        if self._started:
            try:
                self._ir.shutdown()
            except Exception:
                pass
        self._started = False

    def freeze(self):
        # Waits (briefly) for iRacing's next 60 Hz sample, then snapshots it.
        self._ir.freeze_var_buffer_latest()

    def get(self, name):
        # Telemetry vars this car doesn't have would otherwise fall through to a
        # session-info YAML search on every call; session sections are CamelCase "...Info".
        if name not in self._vars and not name.endswith("Info"):
            return None
        try:
            return self._ir[name]
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Mock source: a small race simulation for testing the dashboard without iRacing
# ---------------------------------------------------------------------------

VMAX = 88.0     # m/s top speed
LAT_G = 24.0    # m/s^2 cornering grip
BRAKE = 30.0    # m/s^2
ACCEL = 7.5     # m/s^2
GEAR_TOPS = [24, 36, 48, 60, 72, 92]  # m/s at 8000 rpm per gear


class _MockTrack:
    def __init__(self, length=5200.0, n=2400):
        raw = []
        for i in range(n):
            th = 2 * math.pi * i / n
            r = (1 + 0.25 * math.cos(2 * th) + 0.12 * math.sin(3 * th + 0.5)
                 + 0.07 * math.cos(5 * th + 1.0) + 0.035 * math.sin(7 * th))
            raw.append((r * math.cos(th), r * math.sin(th)))
        seg = [math.dist(raw[i], raw[(i + 1) % n]) for i in range(n)]
        scale = length / sum(seg)
        self.n, self.length = n, length
        self.ds = [d * scale for d in seg]
        self.s = [0.0] * n
        for i in range(1, n):
            self.s[i] = self.s[i - 1] + self.ds[i - 1]
        self.heading = [math.atan2(raw[(i + 1) % n][1] - raw[i][1], raw[(i + 1) % n][0] - raw[i][0])
                        for i in range(n)]

        curv = []
        for i in range(n):
            dh = self.heading[(i + 1) % n] - self.heading[i]
            curv.append(math.atan2(math.sin(dh), math.cos(dh)) / self.ds[i])
        w = 9
        curv = [sum(curv[(i + k) % n] for k in range(-w, w + 1)) / (2 * w + 1) for i in range(n)]
        self.curv = curv

        v = [min(VMAX, math.sqrt(LAT_G / max(abs(k), 1e-6))) for k in curv]
        for _ in range(3):
            for i in reversed(range(n)):
                j = (i + 1) % n
                v[i] = min(v[i], math.sqrt(v[j] ** 2 + 2 * BRAKE * self.ds[i]))
            for i in range(n):
                j = (i + 1) % n
                v[j] = min(v[j], math.sqrt(v[i] ** 2 + 2 * ACCEL * self.ds[i]))
        self.v = v

        self.thr, self.brk = [], []
        for i in range(n):
            j = (i + 1) % n
            acc = (v[j] ** 2 - v[i] ** 2) / (2 * self.ds[i])
            if acc > 1.0 or v[i] >= VMAX - 0.5:
                self.thr.append(1.0); self.brk.append(0.0)
            elif acc < -4.0:
                self.thr.append(0.0); self.brk.append(min(1.0, -acc / BRAKE))
            else:
                self.thr.append(0.45 + 0.4 * v[i] / VMAX); self.brk.append(0.0)

        self.t = [0.0] * (n + 1)
        for i in range(n):
            self.t[i + 1] = self.t[i] + self.ds[i] / ((v[i] + v[(i + 1) % n]) / 2)
        self.lap_time = self.t[n]

    def index(self, dist):
        d = dist % self.length
        i = max(0, bisect.bisect_right(self.s, d) - 1)
        return i, (d - self.s[i]) / self.ds[i]

    def speed(self, dist):
        i, f = self.index(dist)
        return self.v[i] + (self.v[(i + 1) % self.n] - self.v[i]) * f

    def time_at(self, pct):
        i, f = self.index(pct * self.length)
        return self.t[i] + (self.t[i + 1] - self.t[i]) * f


_FIRST = ["Alex", "Sam", "Jordan", "Robin", "Kai", "Noa", "Mika", "Toni", "Charlie", "Remy",
          "Jules", "Sasha", "Eli", "Quinn", "Ari", "Lou", "Nico", "Dani"]
_LAST = ["Novak", "Berg", "Moreau", "Kowal", "Rossi", "Lind", "Okafor", "Silva", "Tanaka", "Weber",
         "Dumas", "Varga", "Holm", "Castro", "Ivanov", "Keller", "Brandt", "Duarte"]
CORNERS = ("LF", "RF", "LR", "RR")


class MockSource:
    """Simulated 40-minute multiclass race. `speed` scales simulated time."""

    name = "mock"
    RACE_LENGTH = 40 * 60.0
    PLAYER_IDX = 7

    def __init__(self, speed=1.0, seed=7):
        self.speed = speed
        self.rng = random.Random(seed)
        self.track = _MockTrack()
        self._vars, self._info = {}, {}
        self._connected = False

    # -- interface ---------------------------------------------------------
    def connect(self):
        if not self._connected:
            self._setup()
            self._connected = True
        return True

    def alive(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def freeze(self):
        now = time.monotonic()
        dt = min(0.25, now - self._wall) * self.speed
        self._wall = now
        if dt > 0:
            self._update(dt)

    def get(self, name):
        v = self._vars.get(name)
        return v if v is not None else self._info.get(name)

    # -- simulation ----------------------------------------------------------
    def _setup(self):
        rng = self.rng
        self.t = 0.0
        self._wall = time.monotonic()
        self.cars = []
        for k in range(18):
            idx = k + 1
            cls = "GT3" if k < 12 else "GT4"
            base = 1.0 if cls == "GT3" else 0.93
            self.cars.append({
                "idx": idx, "cls": cls,
                "name": f"{_FIRST[k]} {_LAST[(k * 7) % len(_LAST)]}",
                "num": str(rng.randint(2, 99)),
                "factor": 1.0 if idx == self.PLAYER_IDX else base * (1 + rng.uniform(-0.012, 0.010)),
                "phase": rng.uniform(0, 6.28),
                "dist": -20.0 - 16.0 * k,
                "lap_start": None, "last": -1.0, "best": -1.0, "v": 0.0, "pit": False,
                "ir": rng.randint(1200, 4200), "lic": rng.choice(["A 3.12", "B 2.40", "A 4.01", "C 3.55"]),
            })
        self.player = self.cars[self.PLAYER_IDX - 1]
        self.player["name"] = "You (Simulated)"
        self.fuel = 30.0
        self.pit_req = self.pitting = self.refuelled = False
        self.pit_lap = 0
        self.incidents = 0
        self.lap_factor = 1.0
        self.tyre = {c: [60.0, 60.0, 60.0] for c in CORNERS}
        self._info = self._session_info()
        self._update(1e-3)  # publish a first sample so the first read isn't empty

    def _session_info(self):
        drivers = [{"CarIdx": 0, "UserName": "Pace Car", "CarNumber": "0", "CarIsPaceCar": 1,
                    "IsSpectator": 0, "CarClassShortName": "", "CarClassColor": 0xFFFFFF}]
        for c in self.cars:
            gt3 = c["cls"] == "GT3"
            drivers.append({
                "CarIdx": c["idx"], "UserName": c["name"], "TeamName": "", "CarNumber": c["num"],
                "CarScreenNameShort": "GT3 Car" if gt3 else "GT4 Car",
                "CarClassShortName": c["cls"], "CarClassID": 1 if gt3 else 2,
                "CarClassColor": 0xFFDA59 if gt3 else 0x33CEFF,
                "CarClassEstLapTime": self.track.lap_time / (1.0 if gt3 else 0.93),
                "IRating": c["ir"], "LicString": c["lic"], "CarIsPaceCar": 0, "IsSpectator": 0,
            })
        return {
            "DriverInfo": {
                "DriverCarIdx": self.PLAYER_IDX, "DriverCarRedLine": 8000.0,
                "DriverCarSLFirstRPM": 6500.0, "DriverCarSLShiftRPM": 7500.0, "DriverCarSLBlinkRPM": 7700.0,
                "DriverCarFuelMaxLtr": 60.0, "DriverCarMaxFuelPct": 1.0, "Drivers": drivers,
            },
            "WeekendInfo": {
                "TrackID": 9999, "TrackDisplayName": "Mock Raceway", "TrackConfigName": "Grand Prix",
                "TrackLength": f"{self.track.length / 1000:.2f} km", "SubSessionID": 1, "SessionID": 1,
                "WeekendOptions": {"IncidentLimit": 17},
            },
            "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Race", "SessionName": "RACE",
                                          "SessionLaps": "unlimited", "SessionTime": "2400.0000 sec"}]},
        }

    def _advance(self, c, dt):
        v = self.track.speed(c["dist"]) * c["factor"] * (1 + 0.012 * math.sin(self.t * 0.05 + c["phase"]))
        if c is self.player:
            v *= self.lap_factor * (1 + 0.006 * math.sin(c["dist"] / 300))
        if c["pit"]:
            v = min(v, 22.0)
        c["v"] = v
        L = self.track.length
        old = math.floor(c["dist"] / L)
        c["dist"] += v * dt
        new = math.floor(c["dist"] / L)
        if new > old:
            if new >= 1 and c["lap_start"] is not None:
                c["last"] = self.t - c["lap_start"]
                c["best"] = c["last"] if c["best"] < 0 else min(c["best"], c["last"])
            c["lap_start"] = self.t
            return True
        return False

    def _update(self, dt):
        self.t += dt
        L = self.track.length
        racing = self.t < self.RACE_LENGTH + 120
        pl = self.player
        crossed_me = False
        for c in self.cars:
            if racing and self._advance(c, dt) and c is pl:
                crossed_me = True

        pct = (pl["dist"] % L) / L
        if crossed_me:
            self.lap_factor = 1 + self.rng.gauss(0, 0.004)
            if self.rng.random() < 0.08:
                self.incidents += self.rng.choice([1, 2])
            if self.fuel < 4.5:
                self.pit_req = True
            if self.pitting:
                self.fuel, self.pit_req, self.refuelled = 60.0, False, True
                self.pit_lap = math.floor(pl["dist"] / L)
        if self.pit_req and not self.pitting and pct > 0.95:
            self.pitting = True
        if self.pitting and self.refuelled and 0.04 < pct < 0.5:
            self.pitting = self.refuelled = False
        pl["pit"] = self.pitting

        i, _ = self.track.index(pl["dist"])
        thr, brk = self.track.thr[i], self.track.brk[i]
        if self.pitting:
            thr, brk = min(thr, 0.3), 0.0
        v = pl["v"] if racing else 0.0
        curv = self.track.curv[i]
        prev_v, self._prev_v = getattr(self, "_prev_v", v), v
        self.fuel = max(0.0, self.fuel - (0.035 * thr + 0.002) * dt)

        gear = next((g + 1 for g, top in enumerate(GEAR_TOPS) if v < top * 0.95), 6)
        rpm = max(1800.0, min(8100.0, 8000.0 * v / GEAR_TOPS[gear - 1]))

        # standings
        order = sorted(self.cars, key=lambda c: -c["dist"])
        pos, cpos, f2 = {}, {}, {}
        class_count = {}
        lead = order[0]["dist"]
        avg_speed = L / self.track.lap_time
        for p, c in enumerate(order, 1):
            pos[c["idx"]] = p
            class_count[c["cls"]] = class_count.get(c["cls"], 0) + 1
            cpos[c["idx"]] = class_count[c["cls"]]
            f2[c["idx"]] = (lead - c["dist"]) / avg_speed

        n = 64
        def arr(fill):
            return [fill] * n
        car_pct, car_lap, car_lapc, car_pos, car_cpos = arr(-1.0), arr(-1), arr(-1), arr(0), arr(0)
        car_last, car_best, car_f2, car_est, car_pit, car_surf = arr(-1.0), arr(-1.0), arr(0.0), arr(0.0), arr(False), arr(-1)
        for c in self.cars:
            k = c["idx"]
            cp = (c["dist"] % L) / L
            fl = math.floor(c["dist"] / L)
            car_pct[k], car_lap[k], car_lapc[k] = cp, fl + 1, max(0, fl)
            car_pos[k], car_cpos[k], car_f2[k] = pos[k], cpos[k], f2[k]
            car_last[k], car_best[k] = c["last"], c["best"]
            car_est[k] = self.track.time_at(cp) / (1.0 if c["cls"] == "GT3" else 0.93)
            car_pit[k] = c["pit"]
            car_surf[k] = 3 if not c["pit"] else 2

        cur = self.t - pl["lap_start"] if pl["lap_start"] is not None else 0.0
        frac = self.track.time_at(pct) / self.track.lap_time
        best = pl["best"]
        session_best = min((c["best"] for c in self.cars if c["best"] > 0 and c["cls"] == "GT3"), default=-1)

        flags = 0x4 | 0x40000
        if 180 <= self.t % 300 < 200:
            flags |= 0x8
        if self.t >= self.RACE_LENGTH:
            flags = 0x1

        laps_on_tyres = max(0, math.floor(pl["dist"] / L) - self.pit_lap)
        load = 0.6 * thr + 0.9 * brk + 0.4 * v / VMAX
        for ci, c in enumerate(CORNERS):
            bias = (6 if c[0] == "L" else 0) + (4 if c[1] == "F" else 0)
            for k, off in enumerate((3, 0, -2) if c[0] == "L" else (-2, 0, 3)):
                target = 62 + 32 * load + bias + off
                self.tyre[c][k] += (target - self.tyre[c][k]) * min(1.0, dt * 0.15)

        V = {
            "IsOnTrack": True, "IsReplayPlaying": False, "SessionTime": self.t,
            "SessionTimeRemain": max(0.0, self.RACE_LENGTH - self.t), "SessionLapsRemainEx": 32767,
            "SessionNum": 0, "SessionState": 4 if self.t < self.RACE_LENGTH else 5, "SessionFlags": flags,
            "Speed": v, "RPM": rpm, "Gear": gear, "Throttle": thr, "Brake": brk, "Clutch": 1.0,
            "SteeringWheelAngle": math.atan(2.6 * curv) * 14.0, "Yaw": self.track.heading[i],
            "LatAccel": v * v * curv, "LongAccel": (v - prev_v) / dt if dt > 0 else 0.0, "YawRate": v * curv,
            "BrakeABSactive": brk > 0.9,
            "Lap": math.floor(pl["dist"] / L) + 1, "LapCompleted": max(0, math.floor(pl["dist"] / L)),
            "LapDistPct": pct, "LapCurrentLapTime": cur, "LapLastLapTime": pl["last"], "LapBestLapTime": best,
            "LapDeltaToBestLap": cur - frac * best if best > 0 else 0.0, "LapDeltaToBestLap_OK": best > 0,
            "LapDeltaToSessionBestLap": cur - frac * session_best if session_best > 0 else 0.0,
            "LapDeltaToSessionBestLap_OK": session_best > 0,
            "PlayerCarPosition": pos[pl["idx"]], "PlayerCarClassPosition": cpos[pl["idx"]],
            "PlayerTrackSurface": 2 if self.pitting else 3,
            "FuelLevel": self.fuel, "FuelLevelPct": self.fuel / 60.0,
            "OnPitRoad": self.pitting, "PlayerCarInPitStall": False,
            "EngineWarnings": 0x10 if self.pitting else 0,
            "PitSvFlags": 0x10 | 0xF, "PitSvFuel": 60.0, "PitRepairLeft": 0.0, "PitOptRepairLeft": 0.0,
            "PlayerCarMyIncidentCount": self.incidents, "PlayerCarTeamIncidentCount": self.incidents,
            "WaterTemp": 86 + 6 * thr, "OilTemp": 98 + 4 * thr, "OilPress": 4.2 + rpm / 8000,
            "FuelPress": 3.9, "Voltage": 13.8, "dcBrakeBias": 54.5, "dcABS": 4.0, "dcTractionControl": 3.0,
            "AirTemp": 21.5, "TrackTempCrew": 31.2, "TrackWetness": 1, "WindVel": 3.4, "WindDir": 1.2,
            "Precipitation": 0.0,
            "CarIdxLapDistPct": car_pct, "CarIdxLap": car_lap, "CarIdxLapCompleted": car_lapc,
            "CarIdxPosition": car_pos, "CarIdxClassPosition": car_cpos, "CarIdxLastLapTime": car_last,
            "CarIdxBestLapTime": car_best, "CarIdxF2Time": car_f2, "CarIdxEstTime": car_est,
            "CarIdxOnPitRoad": car_pit, "CarIdxTrackSurface": car_surf,
        }
        for c in CORNERS:
            t = self.tyre[c]
            V[f"{c}tempCL"], V[f"{c}tempCM"], V[f"{c}tempCR"] = t
            wear = max(0.0, 1 - laps_on_tyres * (0.011 if c[0] == "L" else 0.009))
            V[f"{c}wearL"] = V[f"{c}wearM"] = V[f"{c}wearR"] = wear
            V[f"{c}coldPressure"] = 165.0
            V[f"{c}pressure"] = 165.0 + (sum(t) / 3 - 25) * 0.4
        self._vars = V
