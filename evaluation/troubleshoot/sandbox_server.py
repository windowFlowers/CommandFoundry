"""Sandbox API on a unix socket: POST /start, /exec, /stop with JSON bodies.

Each run gets its own PID, network, mount and IPC namespaces through bwrap. The
socket lives in a 0700 directory and reaches the harness through ssh -L.
"""
import http.server
import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
API = ROOT / "api.sock"
MAX_AGE = 3600
runs = {}
lock = threading.Lock()


def start(body):
    run_id = uuid.uuid4().hex[:12]
    base = RUNS / run_id
    for name in ("opt", "var", "run", "home", "ctl"):
        (base / name).mkdir(parents=True)
    size = str(int(body.get("data_bytes", 33554432)))
    argv = ["bwrap", "--unshare-all", "--hostname", "sbx", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", "/etc/alternatives", "/etc/alternatives", "--ro-bind", "/etc/hosts", "/etc/hosts",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(base / "opt"), "/opt", "--bind", str(base / "var"), "/var",
            "--bind", str(base / "run"), "/run", "--bind", str(base / "home"), "/home/dev",
            "--size", size, "--tmpfs", "/srv/data",
            "--ro-bind", str(ROOT / "lib"), "/sbx", "--bind", str(base / "ctl"), "/ctl",
            "--chdir", "/home/dev", "python3", "/sbx/supervisor.py"]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    with lock:
        runs[run_id] = {"process": process, "started": time.time(), "base": base}
    for _ in range(100):
        if (base / "ctl" / "s.sock").exists():
            return {"run_id": run_id}
        time.sleep(0.05)
    stop({"run_id": run_id})
    raise RuntimeError("sandbox did not start")


def execute(body):
    with lock:
        run = runs[body["run_id"]]
    timeout = body.get("timeout", 30)
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(timeout + 5)
    with client:
        client.connect(str(run["base"] / "ctl" / "s.sock"))
        client.sendall(json.dumps({"argv": body["argv"], "stdin": body.get("stdin", ""), "timeout": timeout}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def stop(body):
    with lock:
        run = runs.pop(body["run_id"], None)
    if run:
        pid = run["process"].pid
        # --new-session puts the inner bwrap in its own session, so killpg alone leaves it and every
        # service running. It is PID 1 of the sandbox: killing it takes the whole namespace down.
        # (--die-with-parent is no use here: it fires when the request thread that started bwrap ends.)
        try:
            for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split():
                os.kill(int(child), signal.SIGKILL)
            os.killpg(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass
        run["process"].wait(timeout=5)
        shutil.rmtree(run["base"], ignore_errors=True)
    return {"stopped": bool(run)}


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {"/start": start, "/exec": execute, "/stop": stop}

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            status, result = 200, self.routes[self.path](body)
        except Exception as exc:
            status, result = 500, {"error": f"{type(exc).__name__}: {exc}"}
        payload = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 128

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def reap():
    while True:
        time.sleep(60)
        with lock:
            old = [run_id for run_id, run in runs.items() if time.time() - run["started"] > MAX_AGE]
        for run_id in old:
            stop({"run_id": run_id})


def main():
    os.chmod(ROOT, 0o700)
    # A previous daemon may have died with sandboxes still running.
    subprocess.run(["pkill", "-9", "-f", f"^bwrap .*{RUNS}/"], check=False)
    shutil.rmtree(RUNS, ignore_errors=True)
    RUNS.mkdir()
    API.unlink(missing_ok=True)
    threading.Thread(target=reap, daemon=True).start()
    Server(str(API), Handler).serve_forever()


if __name__ == "__main__":
    main()
