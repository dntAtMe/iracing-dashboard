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
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import history
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
    def emit(kind, payload, record=False):
        msg = json.dumps({"t": kind, **payload}, separators=(",", ":"))
        loop.call_soon_threadsafe(hub.publish, kind, msg)
        if record and engine.recorder:
            engine.recorder.frame(kind, engine.g("SessionTime"), msg)

    next_fast = next_slow = 0.0
    map_version = None
    while not stop.is_set():
        t0 = time.perf_counter()
        live = False
        try:
            if engine.recorder:
                engine.recorder.apply_pending()
            live = engine.step()
            now = time.perf_counter()
            if now >= next_fast:
                next_fast = max(next_fast + 1 / FAST_HZ, now) if live else now + 0.5
                emit("fast", engine.fast_frame(), record=live)
            if now >= next_slow:
                next_slow = max(next_slow + 1 / SLOW_HZ, now) if live else now + 1.0
                emit("slow", engine.slow_frame(), record=live)
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


LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def create_app(engine, token=None, storage=None):
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
        if engine.recorder:
            engine.recorder.end_session()

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

    # ---- history API (read connections are per request; the recorder writes on its own) ----
    def check_token(request: Request):
        if token and request.query_params.get("token") != token:
            raise HTTPException(401, "Missing or wrong token")

    def history_db(request: Request):
        check_token(request)
        if not storage:
            raise HTTPException(404, "History is turned off (--no-history)")
        return history.connect(storage.db_path)

    def is_local(request: Request):
        return bool(request.client) and request.client.host in LOCAL_HOSTS

    def require_local(request: Request):
        check_token(request)
        if not storage:
            raise HTTPException(404, "History is turned off (--no-history)")
        if not is_local(request):
            raise HTTPException(403, "The data folder can only be changed on the PC running the dashboard")

    @app.get("/api/storage")
    def api_storage(request: Request):
        check_token(request)
        if not storage:
            raise HTTPException(404, "History is turned off (--no-history)")
        return {**storage.info(), "canChange": is_local(request), "cliOverride": storage.cli_override}

    @app.get("/api/storage/browse")
    def api_browse(request: Request, path: str = ""):
        require_local(request)
        try:
            return history.browse(path or str(storage.dir), storage.db_path.name)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/storage")
    async def api_set_storage(request: Request):
        require_local(request)
        body = await request.json()
        try:
            storage.set_dir(body.get("dir", ""), create=bool(body.get("create")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if engine.recorder:
            engine.recorder.request_switch(storage.db_path, storage.maps_dir)
        history.connect(storage.db_path).close()  # create the file so the list loads right away
        return {**storage.info(), "canChange": True, "cliOverride": storage.cli_override}

    @app.get("/api/sessions")
    def api_sessions(request: Request):
        db = history_db(request)
        try:
            return history.list_sessions(db)
        finally:
            db.close()

    @app.get("/api/sessions/{sid}")
    def api_session(sid: int, request: Request):
        db = history_db(request)
        try:
            detail = history.session_detail(db, sid, [storage.maps_dir, ROOT / "maps"])
        finally:
            db.close()
        if detail is None:
            raise HTTPException(404, "No such session")
        detail["live"] = bool(engine.recorder and engine.recorder.session_id == sid)
        return detail

    @app.delete("/api/sessions/{sid}")
    def api_delete_session(sid: int, request: Request):
        if engine.recorder and engine.recorder.session_id == sid:
            raise HTTPException(409, "This session is still being recorded")
        db = history_db(request)
        try:
            history.delete_session(db, sid)
        finally:
            db.close()
        return {"deleted": sid}

    @app.get("/api/sessions/{sid}/timeline")
    def api_timeline(sid: int, request: Request):
        db = history_db(request)
        try:
            tl = history.timeline(db, sid)
        finally:
            db.close()
        if tl is None:
            raise HTTPException(404, "This session has no replay data")
        return tl

    @app.get("/api/sessions/{sid}/frames")
    def api_frames(sid: int, a: float, b: float, request: Request):
        db = history_db(request)
        try:
            body = history.frames_json(db, sid, a, b)
        finally:
            db.close()
        if "deflate" in request.headers.get("accept-encoding", ""):
            return Response(zlib.compress(body, 6), media_type="application/json",
                            headers={"Content-Encoding": "deflate", "Vary": "Accept-Encoding"})
        return Response(body, media_type="application/json")

    @app.get("/api/laps/{lap_id}")
    def api_lap(lap_id: int, request: Request):
        deflate = "deflate" in request.headers.get("accept-encoding", "")
        db = history_db(request)
        try:
            blob = history.lap_blob(db, lap_id, decompress=not deflate)
        finally:
            db.close()
        if blob is None:
            raise HTTPException(404, "No such lap")
        headers = {"Content-Encoding": "deflate", "Vary": "Accept-Encoding"} if deflate else None
        return Response(blob, media_type="application/json", headers=headers)

    @app.middleware("http")
    async def no_stale_assets(request, call_next):
        # Phones cache aggressively; always revalidate so dashboard updates show up.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app


def lan_ips():
    """Returns (primary, tailscale, others). Primary is the adapter that carries the default route."""
    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            primary = s.getsockname()[0]
    except OSError:
        pass
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    if primary:
        ips.add(primary)
    ips = {ip for ip in ips if not ip.startswith(("127.", "169.254."))}
    # 100.64.0.0/10 is the carrier-grade NAT range Tailscale assigns from
    tailscale = sorted(ip for ip in ips if ip.startswith("100.") and 64 <= int(ip.split(".")[1]) <= 127)
    others = sorted(ips - set(tailscale) - {primary})
    return primary, tailscale, others


def main():
    ap = argparse.ArgumentParser(description="iRacing race-engineer web dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="run a simulated race instead of reading iRacing")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="time multiplier for --mock")
    ap.add_argument("--token", help="require ?token=... in the URL (use when exposing beyond your LAN)")
    ap.add_argument("--data-dir", help="folder for recorded sessions (default: the one chosen in History, else this folder)")
    ap.add_argument("--no-history", action="store_true", help="don't record sessions")
    args = ap.parse_args()

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)  # 1 ms timer resolution for the 60 Hz loop
        except Exception:
            pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((args.host, args.port))
    except OSError:
        raise SystemExit(f"Port {args.port} is already in use by another program. "
                         f"Start with a different one, e.g.: python server.py --port {args.port + 1}")

    source = MockSource(speed=args.mock_speed) if args.mock else IRacingSource()
    storage = None if args.no_history else history.Storage(ROOT, args.data_dir, mock=args.mock)
    recorder = history.Recorder(storage.db_path, storage.maps_dir) if storage else None
    engine = Engine(source, ROOT / "maps", recorder)
    app = create_app(engine, args.token, storage)

    q = f"/?token={args.token}" if args.token else "/"
    print(f"\n  iRacing dashboard  ({'SIMULATED' if args.mock else 'live iRacing'})")
    print(f"  This PC:      http://localhost:{args.port}{q}")
    primary, tailscale, others = lan_ips()
    if primary and primary not in tailscale:
        print(f"  Phone / LAN:  http://{primary}:{args.port}{q}")
    for ip in tailscale:
        print(f"  Tailscale:    http://{ip}:{args.port}{q}")
    for ip in others:
        print(f"  Other:        http://{ip}:{args.port}{q}   (VPN or virtual adapter)")
    if storage:
        print(f"  History:      http://localhost:{args.port}/history.html{q[1:]}  (saved to {storage.db_path})")
    print("  Ctrl+C to stop\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
