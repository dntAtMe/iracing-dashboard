# iRacing Race Engineer Dashboard

A live telemetry dashboard for iRacing that runs in any browser. Start it on the PC running the sim, then open it
on your phone as a second screen or hand the link to the person acting as your race engineer.

![Desktop console view](docs/desktop.png)

<p align="center"><img src="docs/mobile.png" width="320" alt="Phone view, Drive tab"></p>

## Features

| Area | What you get |
|---|---|
| **Drive** | Gear, speed, shift lights driven by the car's own RPM settings, delta bar (against your best lap or the session best), current, last and best lap, pedal inputs, pit limiter and engine warnings |
| **Fuel** | Average use per lap from clean laps, laps left in the tank, laps to go (timed or lap-limited races), fuel to finish, fuel to add, stops needed, fuel set for the next stop |
| **Session** | Flag shown in the status bar, time and laps remaining, incidents against the limit, stint length, air and track temperature, track wetness, wind |
| **Timing** | Relative with lapping and lapped cars coloured, full standings with gap, interval, last and best lap (purple = fastest overall), your lap history with fuel used per lap |
| **Car** | Tyre temperatures, tread and pressures laid out like the car, water and oil temperatures, oil and fuel pressure, voltage, brake bias, ABS, traction control, repair time left, input trace |
| **Track** | A track outline drawn automatically from your first clean lap and saved for next time, with every car placed on it |
| **History** | Every session is recorded: each lap's telemetry at 60 Hz, lap times, fuel, incidents, pit stops, flags and the whole field's lap times |
| **Analysis** | Overlay up to 3 laps: speed, throttle, brake, gear, RPM, steering, G-forces and delta, linked to a speed-coloured track map |
| **Replay** | Play back any recorded session in the live view with play, pause, seek, lap jumps and up to 16× speed |

On a wide screen everything shows at once as a three-column console. On a phone the same panels are split
across Drive, Timing, Car and Track tabs.

## Requirements

- Windows (iRacing's telemetry is only available on the PC running the sim)
- Python 3.10 or newer
- Any modern browser on the viewing device

## Quick start

```bash
git clone https://github.com/dntAtMe/iracing-dashboard.git
cd iracing-dashboard
run.bat
```

`run.bat` creates a virtual environment and installs dependencies the first time, then starts the server. When it
starts, it prints the addresses to open:

```
  iRacing dashboard  (live iRacing)
  This PC:      http://localhost:8765/
  Phone / LAN:  http://192.168.1.23:8765/
```

Open the LAN address on any device connected to the same Wi-Fi. The first time, Windows Firewall asks whether
Python may accept connections. Allow it on **Private** networks.

You can start the server before or after iRacing. It connects as soon as you are in the car and reconnects
automatically between sessions.

### Manual setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python server.py
```

### Try it without iRacing

A built-in simulator runs a 40-minute multiclass race (GT3 and GT4) with pit stops, flags and incidents:

```bash
.venv\Scripts\python server.py --mock
.venv\Scripts\python server.py --mock --mock-speed 4   # 4x faster
```

## Using it on a phone

- Add the page to your home screen to open it full screen without the browser bar.
- Turn off auto-lock while you drive. Browsers only let a page keep the screen awake over HTTPS.
- Turn the phone sideways for a two-column layout.
- Unit choices (km/h or mph, °C or °F, litres or gallons, kPa, psi or bar) and the delta reference are under the
  menu button in the top right. Each device remembers its own choices.

## History, analysis and replay

Sessions are recorded automatically while the server runs. Open **History** with the chart button in the top bar,
or go to `/history.html`.

![Lap analysis](docs/history.png)

- **Sessions and laps.** Pick a session, then click up to three laps to compare. The first one you pick is the
  reference. Your best lap is selected when a session opens.
- **Linked cursor.** Hover any chart and every other chart, the readouts and the track map follow. Hover the track
  map to move the chart cursor to that corner. Other laps appear on the map where they were at the same moment of
  the reference lap, so you can see the gap on track.
- **Zoom.** Scroll over a chart to zoom, drag to pan, double-click or **Full lap** to reset. Clicking the map zooms
  into that part of the lap. The zoomed stretch is highlighted on the map.
- **Session pace.** Your lap times against the whole field. Click one of your laps to load it.
- **Replay.** **Replay** in the header, or **Replay** next to a selected lap, opens the live view and plays the
  session back. Every panel works as it did live. Use the bar at the bottom, or Space to play and pause, and the
  arrow keys to skip 5 seconds (30 with Shift).

### Data folder

Everything is saved in one folder: `history.db` plus the track maps its sessions use, so the folder can be moved,
backed up, or opened from another PC. By default that's the dashboard's own folder.

To change it, open **History** on the sim PC and use the folder button in the top bar. Pick a folder that already
has a `history.db` to load the races in it. The choice is remembered in `config.json`. For safety the folder can
only be changed from the PC running the dashboard; other devices can see which folder is in use.

Network folders such as a NAS share work too. Only have one dashboard writing to the same folder at a time.

## Remote race engineer

If your engineer is not on your network, the simplest option is [Tailscale](https://tailscale.com/). Install it on
both machines, then they open `http://<your-tailscale-ip>:8765` (or `http://<pc-name>:8765` with MagicDNS). The
server prints your Tailscale address when it starts.

Windows treats Tailscale as a separate network, so the firewall usually blocks it even when your Wi-Fi works.
Allow the dashboard port on the Tailscale connection only, from an **Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "iRacing dashboard (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8765 -InterfaceAlias Tailscale -RemoteAddress 100.64.0.0/10 -Action Allow
```

Tailscale also works for your own phone away from home Wi-Fi, or when the Wi-Fi blocks devices from seeing each
other.

If you expose the server any other way (port forwarding, a tunnel), require a token:

```bash
.venv\Scripts\python server.py --token SOME-LONG-SECRET
```

and share `http://<host>:8765/?token=SOME-LONG-SECRET`. Without a token, anyone who can reach the port can see your
telemetry. The dashboard is read-only and can't control the sim.

## Command-line options

| Option | Default | Description |
|---|---|---|
| `--port` | `8765` | HTTP port |
| `--host` | `0.0.0.0` | Address to listen on. Use `127.0.0.1` to allow only this PC |
| `--token` | none | Require `?token=` on every connection |
| `--mock` | off | Use the built-in race simulator instead of iRacing |
| `--mock-speed` | `1.0` | Time multiplier for the simulator |
| `--data-dir` | as chosen in History | Folder for recorded sessions. Overrides the folder picked in History |
| `--no-history` | off | Don't record sessions |

## Good to know

- **Tyre data** is refreshed by iRacing only while the car is in the pit box, not live on track. This is a
  limitation of iRacing's live telemetry.
- **Fuel estimates** appear after one clean lap. Laps that include a pit visit, and the partial lap in progress when
  the dashboard connects, are not counted.
- **The track map** is drawn from your speed and heading over one lap without pitting or resetting. It is saved in
  `maps/`. Delete the file for a track to redraw its outline.
- **Gaps** in the relative and standings come from iRacing's own estimates, so they can differ slightly from the
  in-sim black boxes.
- Values your car doesn't have (for example a second traction control setting) are shown as `–`.
- **Recording size** is roughly 50 KB per lap of telemetry plus 5–15 MB per hour for replay, depending on the field
  size. Sessions where you never completed part of a lap aren't kept. Simulated sessions (`--mock`) go to
  `history-mock.db` so they never mix with real ones.

## Troubleshooting

| Status bar shows | Meaning and fix |
|---|---|
| **Offline** | The browser can't reach the server. Check that `server.py` is running, that you used the LAN address and not `localhost` on the phone, and that the firewall allows Python on private networks. |
| **Standby** | The server is running but iRacing isn't sending data yet. Load into a session and get in the car. |
| A flag colour or **Live** | Receiving telemetry. |

If the page looks out of date after updating, reload it. The server tells browsers to check for a newer version on
every load.

## How it works

```
iRacing shared memory ──► sources.py ──► engine.py ──► server.py ──WebSocket──► browser (static/)
     (60 Hz)              read vars       fuel, laps,     20 Hz driving data
                                          standings,       4 Hz strategy/timing
                                          track map        map on change
```

A background thread polls iRacing at 60 Hz. The engine turns raw telemetry into three kinds of message, and the
server pushes them to every connected browser. Each browser receives only the newest message of each kind, so a
phone on weak Wi-Fi skips stale frames instead of falling behind.

On the same thread, `history.py` records each lap's telemetry, session events and the messages themselves into
SQLite. The History page reads them back through a small JSON API, and replay feeds the stored messages through the
same rendering code the live view uses.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the message format, the database layout, the fuel and track-map
algorithms, and how to add a panel.

## Project layout

```
server.py            FastAPI app, WebSocket fan-out, telemetry thread, history API, CLI
engine.py            Telemetry → dashboard frames: fuel strategy, lap tracking, relative, standings, TrackMap
history.py           Recorder (SQLite), data folder, read queries for History and replay
sources.py           IRacingSource (pyirsdk) and MockSource (race simulator)
static/index.html    Live dashboard markup, including the replay bar
static/app.js        WebSocket client, rendering, canvas map, input trace, replay player
static/history.html  History and lap analysis page
static/history.js    Session and lap lists, linked charts, speed map, pace chart, data folder picker
static/style.css     Console styling and responsive layout for both pages
docs/                Architecture notes and screenshots
maps/                Track outlines drawn by the live view (created at runtime, not committed)
history.db           Recorded sessions, in the data folder (created at runtime, not committed)
```

## License

[MIT](LICENSE)

This project is not affiliated with or endorsed by iRacing.com Motorsport Simulations.
