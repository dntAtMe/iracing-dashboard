"""iRacing race-engineer dashboard server.

Reads telemetry in a background thread and streams it to every connected browser
over a WebSocket. Open the printed URL on your phone / engineer's laptop.

    python server.py                 # live iRacing
    python server.py --mock          # simulated race, no iRacing needed
"""
import argparse
import asyncio
import json
import socket
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from engine import Engine
from sources import IRacingSource, MockSource

ROOT = Path(__file__).resolve().parent
TICK_HZ = 60   # telemetry polling (iRacing updates at 60 Hz)
FAST_HZ = 20   # driving data to clients
SLOW_HZ = 4    # strategy / standings to clients


class Client:
    """Per-browser sender that only ever sends the newest frame of each kind,
    so a slow phone on bad wifi drops stale frames instead of lagging behind."""

    ORDER = ("map", "slow", "fast")

    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.wake = asyncio.Event()

    def push(self, kind, msg):
        self.pending[kind] = msg
        self.wake.set()

    async def pump(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            batch, self.pending = self.pending, {}
            for kind in self.ORDER:
                if kind in batch:
                    await self.ws.send_text(batch[kind])


class Hub:
    def __init__(self):
        self.clients = set()
        self.latest = {}

    def publish(self, kind, msg):
        self.latest[kind] = msg
        for c in self.clients:
            c.push(kind, msg)


def telemetry_loop(engine, hub, loop, stop):
    def emit(kind, payload):
        msg = json.dumps({"t": kind, **payload}, separators=(",", ":"))
        loop.call_soon_threadsafe(hub.publish, kind, msg)

    next_fast = next_slow = 0.0
    map_version = None
    while not stop.is_set():
        t0 = time.perf_counter()
        live = False
        try:
            live = engine.step()
            now = time.perf_counter()
            if now >= next_fast:
                next_fast = max(next_fast + 1 / FAST_HZ, now) if live else now + 0.5
                emit("fast", engine.fast_frame())
            if now >= next_slow:
                next_slow = max(next_slow + 1 / SLOW_HZ, now) if live else now + 1.0
                emit("slow", engine.slow_frame())
            if engine.map.version != map_version:
                map_version = engine.map.version
                emit("map", engine.map.frame())
        except Exception:
            traceback.print_exc()
            stop.wait(1.0)
        if not live:
            stop.wait(0.25)
        else:
            rest = 1 / TICK_HZ - (time.perf_counter() - t0)
            if rest > 0:
                time.sleep(rest)


def create_app(engine, token=None):
    hub = Hub()

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        th = threading.Thread(target=telemetry_loop, args=(engine, hub, asyncio.get_running_loop(), stop),
                              daemon=True, name="telemetry")
        th.start()
        yield
        stop.set()
        th.join(timeout=2)

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if token and ws.query_params.get("token") != token:
            await ws.close(code=4401)
            return
        await ws.accept()
        client = Client(ws)
        for kind in Client.ORDER:
            if kind in hub.latest:
                client.push(kind, hub.latest[kind])
        hub.clients.add(client)
        pump = asyncio.create_task(client.pump())
        try:
            while True:
                await ws.receive_text()  # nothing expected; this detects disconnects
        except Exception:
            pass
        finally:
            hub.clients.discard(client)
            pump.cancel()

    @app.middleware("http")
    async def no_stale_assets(request, call_next):
        # Phones cache aggressively; always revalidate so dashboard updates show up.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app


def lan_ips():
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    ap = argparse.ArgumentParser(description="iRacing race-engineer web dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mock", action="store_true", help="run a simulated race instead of reading iRacing")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="time multiplier for --mock")
    ap.add_argument("--token", help="require ?token=... in the URL (use when exposing beyond your LAN)")
    args = ap.parse_args()

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)  # 1 ms timer resolution for the 60 Hz loop
        except Exception:
            pass

    source = MockSource(speed=args.mock_speed) if args.mock else IRacingSource()
    engine = Engine(source, ROOT / "maps")
    app = create_app(engine, args.token)

    q = f"/?token={args.token}" if args.token else "/"
    print(f"\n  iRacing dashboard  ({'SIMULATED' if args.mock else 'live iRacing'})")
    print(f"  This PC:      http://localhost:{args.port}{q}")
    for ip in lan_ips():
        print(f"  Phone / LAN:  http://{ip}:{args.port}{q}")
    print("  Ctrl+C to stop\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
