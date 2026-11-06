"""Sandbox PID-2 helper: runs one request at a time from the control socket."""
import json
import os
import socket
import subprocess

PATH = "/ctl/s.sock"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/dev", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def run(request):
    try:
        done = subprocess.run(request["argv"], input=request.get("stdin", "").encode("utf-8"), capture_output=True,
                              timeout=request.get("timeout", 30), env=ENV, cwd="/home/dev")
        return {"code": done.returncode, "stdout": done.stdout.decode("utf-8", "replace"),
                "stderr": done.stderr.decode("utf-8", "replace")}
    except subprocess.TimeoutExpired:
        return {"code": 124, "stdout": "", "stderr": "timeout"}


def main():
    server = socket.socket(socket.AF_UNIX)
    server.bind(PATH)
    server.listen(8)
    while True:
        connection, _ = server.accept()
        with connection:
            data = b""
            while not data.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            if data:
                connection.sendall(json.dumps(run(json.loads(data))).encode("utf-8") + b"\n")


if __name__ == "__main__":
    os.setsid() if os.getpid() != os.getsid(0) else None
    main()
