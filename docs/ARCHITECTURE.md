# Architecture

## Overview

```
┌──────────────┐  freeze()/get()  ┌──────────┐  fast/slow/map dicts  ┌───────────┐  JSON over WS  ┌──────────┐
│ sources.py   │ ───────────────► │ engine.py│ ────────────────────► │ server.py │ ─────────────► │ app.js   │
│ IRacingSource│                  │ Engine   │                       │ Hub       │                │ (browser)│
│ MockSource   │                  │ TrackMap │                       │ Client ×N │                │          │
└──────────────┘                  └──────────┘                       └───────────┘                └──────────┘
        ▲                                ▲                                  ▲
        └───────── telemetry thread (60 Hz) ────────────┘                   └── asyncio event loop (uvicorn)
```

Two execution contexts:

- **Telemetry thread** (`telemetry_loop` in `server.py`). Calls `engine.step()` about 60 times a second, builds
  frames on a schedule, serialises them to JSON and hands them to the event loop with `loop.call_soon_threadsafe`.
  All reads from iRacing happen here, so pyirsdk's blocking wait for new data never stalls the web server.
- **asyncio loop** (uvicorn). Serves static files and WebSockets. `Hub.publish` stores the latest message of each kind
  and pushes it to every `Client`.

Each `Client` keeps one pending slot per message kind. A slow connection therefore always receives the newest data and
never builds up a backlog. A newly connected browser immediately gets the last `map`, `slow` and `fast` messages.

## Sources

Both sources implement the same small interface:

| Method | Purpose |
|---|---|
| `connect() -> bool` | Attach. For iRacing, first checks `http://127.0.0.1:32034/get_sim_status` |
| `alive() -> bool` | Still attached and receiving data |
| `disconnect()` | Detach |
| `freeze()` | Snapshot the latest sample. For iRacing this waits up to 32 ms for the next 60 Hz tick |
| `get(name)` | A telemetry variable, or a session-info section such as `DriverInfo`. Returns `None` when missing |

`IRacingSource.get` only forwards names that exist in the current car's variable list, or that end in `Info`
(session-info sections). Without this, pyirsdk falls back to searching the session YAML for every missing variable.

`MockSource` simulates a race on a procedurally generated closed track. It pre-computes a speed profile from
curvature, braking and acceleration limits, drives 18 cars with small pace differences, and produces every
variable the engine reads, including the `CarIdx*` arrays and session-info dictionaries.

## Messages

All messages are JSON objects with a `t` field.

### `fast` (20 Hz)

| Field | Source variable | Notes |
|---|---|---|
| `connected` | | `false` means the other fields are absent |
| `speed`, `rpm`, `gear` | `Speed` (m/s), `RPM`, `Gear` | gear −1 = R, 0 = N |
| `thr`, `brk`, `clu` | `Throttle`, `Brake`, `1 - Clutch` | 0–1; clutch inverted so 1 = pedal pressed |
| `lap`, `pct` | `Lap`, `LapDistPct` | |
| `cur`, `last`, `best` | `LapCurrentLapTime`, `LapLastLapTime`, `LapBestLapTime` | seconds; `null` if no valid time |
| `delta`, `deltaOk` | `LapDeltaToBestLap`, `_OK` | |
| `sdelta`, `sdeltaOk` | `LapDeltaToSessionBestLap`, `_OK` | |
| `pos`, `cpos` | `PlayerCarPosition`, `PlayerCarClassPosition` | |
| `fuel`, `fuelPct` | `FuelLevel` (L), `FuelLevelPct` | |
| `flags` | `SessionFlags` | bitfield, decoded client-side |
| `warn` | `EngineWarnings` | bitfield; `0x10` = pit limiter |
| `pit` | `OnPitRoad` | |
| `tRem`, `lRem` | `SessionTimeRemain`, `SessionLapsRemainEx` | huge values mean unlimited |
| `me` | `DriverInfo.DriverCarIdx` | |
| `cars` | `CarIdxLapDistPct` and others | `[[carIdx, pct, onPit, isPaceCar], …]` for the map |

### `slow` (4 Hz)

| Field | Content |
|---|---|
| `track`, `session`, `car` | Names, session type and state, shift-light RPMs, tank capacity |
| `fuel` | `avg`, `last`, `samples`, `lapsInTank`, `lapsToGo`, `toFinish`, `toAdd`, `stops`, `avgLap` |
| `env` | Air and track temperature, wetness, wind, precipitation |
| `inc` | Your incidents, team incidents, limit |
| `stint` | Laps and seconds since leaving the pit lane |
| `tyres` | Per corner: `t` (temperatures L/M/R), `w` (tread L/M/R), `p` (pressure), `cp` (cold pressure) |
| `sys` | Water and oil temperatures, oil and fuel pressure, voltage, brake bias, ABS, TC |
| `pitSv` | Pit service flags, fuel to add, repair time remaining |
| `standings` | Sorted rows: `idx, pos, cpos, num, name, cls, clsColor, ir, lic, last, best, gap, int, down, pit, out` |
| `relative` | Up to 4 cars ahead, you, and up to 4 behind: `idx, pos, num, name, gap, lap (+1 lapping you / −1 lapped), pit` |
| `laps` | Last 30 laps: `lap, time, fuel, pit` |
| `mapRec` | Track-outline recording progress (0–1), or `null` once a map exists |

### `map` (when it changes)

`{ version, name, points }`, where `points` is 500 `[x, y]` pairs in the range −1…1 at evenly spaced lap
distance. `points` is `null` until an outline exists.

## Algorithms

### Lap and fuel tracking

`Engine._track_laps` watches `LapCompleted`. When it increases by one:

1. Fuel used = fuel at the start of the lap − fuel now.
2. The lap is **clean** if the car never touched pit road during it. The first lap after connecting is partial and
   is never used for fuel.
3. Clean laps add to a rolling window of the last 5 fuel samples.
4. iRacing updates `LapLastLapTime` shortly after the line, so the lap time is read 2 seconds later.

Fuel strategy (`Engine._fuel`):

- **Laps to go.** In a lap-limited race: `SessionLapsRemainEx − LapDistPct`. In a timed race: the rest of the
  current lap plus `ceil(remaining time after this lap / average lap time)`, because the lap in progress when the
  clock reaches zero is completed.
- **Fuel to finish** = laps to go × average use per lap.
- **Fuel to add** = max(0, fuel to finish − current fuel).
- **Stops needed** = ceil(fuel to add / usable tank capacity).

### Track map (`TrackMap`)

iRacing doesn't publish world coordinates, so the outline is reconstructed by dead reckoning:

1. Recording starts when the car crosses the start/finish line (`LapDistPct` wraps from above 0.9 to below 0.1).
2. Every tick, integrate `x += Speed·cos(Yaw)·dt` and `y += Speed·sin(Yaw)·dt`, with `dt` taken from `SessionTime`
   so pauses add nothing.
3. Recording is abandoned if the car enters pit road, leaves the world, is reset or towed (a jump in `LapDistPct`),
   or the replay is playing.
4. At the next crossing, the gap between end and start (drift) is spread linearly over the lap to close the loop.
   If the drift is more than 15% of the lap length, the lap is discarded.
5. The path is resampled to 500 points at evenly spaced `LapDistPct`, normalised to −1…1, and saved as
   `maps/<TrackID>_<config>.json`.

The browser places each car by interpolating between points at that car's `CarIdxLapDistPct`.

### Relative

For each car on track: `dp = their pct − my pct`, wrapped to −0.5…0.5. The time gap is the difference in
`CarIdxEstTime`, corrected by one lap time when its sign disagrees with `dp`. The nearest 4 cars ahead and 4 behind
are kept. A car more than half a lap ahead of you in total distance is marked as lapping you, and one more than half
a lap behind as being lapped.

### Standings

Rows are sorted by `CarIdxPosition`; unclassified cars go last, ordered by best lap. In races the gap is
`CarIdxF2Time` and `down` counts whole laps behind the leader. In practice and qualifying the gap is best lap minus
the fastest best lap. The interval is the gap difference to the car ahead.

## Front end

Plain HTML, CSS and JavaScript with no build step.

- `fast` messages mark the view dirty. A `requestAnimationFrame` loop redraws the driving panel, input trace and map
  at most once per display frame.
- `slow` messages re-render the tables and lists directly.
- Text is only written to the DOM when it changes (`txt()`), which keeps 20 Hz updates cheap on phones.
- Layout: three columns at 1280 px and wider, two columns from 820 px, otherwise tabs. Panels choose their tabs with
  `data-tabs="dash timing car map"`.
- Preferences live in `localStorage` per device.

## Adding a panel

1. Read the variable in `Engine.fast_frame` (if it changes quickly) or `Engine.slow_frame`. Use `self.g(name)`;
   it returns `None` when the car doesn't have it.
2. If it needs simulated data, add it to `MockSource._update`.
3. Add a `<section class="tile" data-tabs="…">` with a `tile-h` header to `static/index.html`.
4. Render it from `renderFast` or `renderSlow` in `static/app.js`.
5. Check it with `python server.py --mock --mock-speed 4`.
