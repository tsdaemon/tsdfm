"""Real browser/DSP smoke test; no radio services, credentials, or npm needed."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from urllib.request import urlopen

import websockets


async def check():
    app = Path(__file__).resolve().parents[2]
    browser = os.environ.get("BROWSER_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
    if not browser:
        raise RuntimeError("Install Chrome/Chromium or set BROWSER_BIN")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="tsdfm-spectrum-") as temp:
        root = Path(temp)
        (root / "static").symlink_to(app / "src/tsdfm/static", target_is_directory=True)
        (root / "index.html").write_text((app / "tests/browser/spectrum.html").read_text())
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=temp))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        process = subprocess.Popen([
            browser, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
            "--no-first-run", "--remote-debugging-port=0",
            "--autoplay-policy=no-user-gesture-required", f"--user-data-dir={root / 'profile'}",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = root / "profile/DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                if process.poll() is not None:
                    raise RuntimeError("Browser exited before starting")
                await asyncio.sleep(0.1)
            port = port_file.read_text().splitlines()[0]
            with urlopen(f"http://127.0.0.1:{port}/json") as response:
                target = next(t for t in json.load(response) if t["type"] == "page")
            async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2**20) as ws:
                await ws.send(json.dumps({"id": 10, "method": "Page.enable"}))
                while json.loads(await ws.recv()).get("id") != 10:
                    pass
                await ws.send(json.dumps({"id": 11, "method": "Page.navigate", "params": {
                    "url": f"http://127.0.0.1:{server.server_port}/",
                }}))
                while json.loads(await asyncio.wait_for(ws.recv(), 10)).get("method") != "Page.loadEventFired":
                    pass
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "new Promise(resolve => { const timer = setInterval(() => {"
                                  "if (typeof runChecks === 'function') {clearInterval(timer);resolve();}"
                                  "}, 50); }).then(() => runChecks())",
                    "awaitPromise": True, "returnByValue": True, "userGesture": True,
                }}))
                while True:
                    result = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                    if result.get("id") == 1:
                        break
                details = result.get("result", {})
                if "exceptionDetails" in details or "error" in result:
                    raise AssertionError(json.dumps(result))
                print(details["result"]["value"])
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            server.shutdown()
            server.server_close()


asyncio.run(check())
