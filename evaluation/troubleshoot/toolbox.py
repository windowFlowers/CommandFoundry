"""Typed troubleshooting tools, standard library only.

Runs inside the sandbox as ``python3 toolbox.py <tool> <json-arguments>``. The
harness imports TOOLS for the model-facing schemas. Keep string fields free of
minLength/maxLength: vLLM's xgrammar then forbids JSON escapes in that string.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

LIMIT = 6000
START_MARK = "=== service start "


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def _text(description):
    return {"type": "string", "description": description}


def _number(description):
    return {"type": "integer", "description": description}


def _choice(description, values):
    return {"type": "string", "description": description, "enum": list(values)}


GIT_VIEWS = ("status", "log", "reflog", "branches", "diff", "stash")
GIT_ACTIONS = ("reset_hard", "checkout", "create_branch", "merge", "merge_abort", "cherry_pick", "stash_pop")

TOOLS = {
    "list_dir": {"writes": False, "description": "列出目录内容，含权限、大小和修改时间。",
                 "parameters": _schema({"path": _text("目录路径")}, ["path"])},
    "read_file": {"writes": False, "description": "读取文本文件。日志可用 tail_lines 只看末尾。",
                  "parameters": _schema({"path": _text("文件路径"),
                                         "tail_lines": _number("只读取最后多少行，可省略")}, ["path"])},
    "search_text": {"writes": False, "description": "在目录或文件中按固定字符串搜索。",
                    "parameters": _schema({"text": _text("要搜索的字符串"), "path": _text("搜索路径")},
                                          ["text", "path"])},
    "check_port": {"writes": False, "description": "查看某个 TCP 端口是否被监听，以及监听进程的 PID 和命令行。",
                   "parameters": _schema({"port": _number("端口号")}, ["port"])},
    "list_processes": {"writes": False, "description": "列出当前进程的 PID、运行时长和命令行。",
                       "parameters": _schema({})},
    "service_status": {"writes": False, "description": "查看服务是否运行，并返回最近的服务日志。",
                       "parameters": _schema({"name": _text("服务名")}, ["name"])},
    "http_check": {"writes": False, "description": "向本机 URL 发 GET 请求，返回状态码和响应内容。",
                   "parameters": _schema({"url": _text("完整 URL")}, ["url"])},
    "disk_usage": {"writes": False, "description": "查看路径所在磁盘的容量，以及路径下占用最大的文件。",
                   "parameters": _schema({"path": _text("路径")}, ["path"])},
    "git_inspect": {"writes": False, "description": "只读查看 Git 仓库。",
                    "parameters": _schema({"repo": _text("仓库路径"), "view": _choice("查看内容", GIT_VIEWS)},
                                          ["repo", "view"])},
    "edit_file": {"writes": True, "description": "把文件中唯一出现的 old_text 替换为 new_text。",
                  "parameters": _schema({"path": _text("文件路径"), "old_text": _text("要替换的原文"),
                                         "new_text": _text("替换后的内容")}, ["path", "old_text", "new_text"])},
    "write_file": {"writes": True, "description": "创建文件或覆盖文件全部内容。",
                   "parameters": _schema({"path": _text("文件路径"), "content": _text("完整内容")},
                                         ["path", "content"])},
    "delete_path": {"writes": True, "description": "删除文件或目录，不可恢复。",
                    "parameters": _schema({"path": _text("路径")}, ["path"])},
    "chmod_path": {"writes": True, "description": "修改文件或目录权限。",
                   "parameters": _schema({"path": _text("路径"), "mode": _text("八进制权限，例如 755")},
                                         ["path", "mode"])},
    "kill_process": {"writes": True, "description": "结束指定 PID 的进程。",
                     "parameters": _schema({"pid": _number("进程 PID"),
                                            "force": {"type": "boolean", "description": "是否强制结束，可省略"}},
                                           ["pid"])},
    "service_restart": {"writes": True, "description": "重启服务并报告启动结果。",
                        "parameters": _schema({"name": _text("服务名")}, ["name"])},
    "run_script": {"writes": True, "description": "运行 /opt 下已有的脚本，例如项目自带的管理脚本。",
                   "parameters": _schema({"path": _text("脚本路径"),
                                          "args": {"type": "array", "items": {"type": "string"},
                                                   "description": "脚本参数，可省略"}}, ["path"])},
    "git_action": {"writes": True, "description": "修改 Git 仓库状态。除 merge_abort、stash_pop 外都要提供 ref。",
                   "parameters": _schema({"repo": _text("仓库路径"), "action": _choice("操作", GIT_ACTIONS),
                                          "ref": _text("提交、分支或新分支名")}, ["repo", "action"])},
}


class ToolError(Exception):
    pass


def _clip(text):
    if len(text) <= LIMIT:
        return text
    return f"{text[:1500]}\n...[省略 {len(text) - LIMIT} 字符]...\n{text[-(LIMIT - 1500):]}"


def _path(value):
    return os.path.expanduser(value)


def _run(argv, timeout=15):
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令超时：{argv[0]}") from None
    output = (done.stdout + done.stderr).decode("utf-8", "replace")
    return done.returncode, output


def _tail(path, lines):
    """Return the newest log lines of the current service start only."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return ""
    current = content.rsplit(START_MARK, 1)[-1].split("\n", 1)[-1] if START_MARK in content else content
    return "".join(current.splitlines(True)[-lines:])


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _command_line(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _service(name):
    run = f"/opt/svc/{name}/run"
    if not os.path.exists(run):
        known = sorted(os.listdir("/opt/svc")) if os.path.isdir("/opt/svc") else []
        raise ToolError(f"未知服务 {name}，已有服务：{', '.join(known) or '无'}")
    return run, f"/run/svc/{name}.pid", f"/var/log/{name}/service.log"


def _service_pid(pid_file):
    try:
        with open(pid_file, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def list_dir(args):
    path = _path(args["path"])
    if not os.path.isdir(path):
        raise ToolError(f"目录不存在：{path}")
    return _run(["ls", "-la", "--time-style=long-iso", path])[1]


def read_file(args):
    path = _path(args["path"])
    try:
        with open(path, "rb") as handle:
            if b"\0" in handle.read(4096):
                return f"{path} 是二进制文件（{os.path.getsize(path)} 字节），不读取内容"
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        raise ToolError(f"无法读取 {path}：{exc.strerror}") from None
    tail = args.get("tail_lines")
    return "".join(lines[-tail:] if tail and tail > 0 else lines) or "（空文件）"


def search_text(args):
    code, output = _run(["grep", "-rnIF", "-m", "20", "--", args["text"], _path(args["path"])])
    return output if code in (0, 1) and output else "没有匹配结果"


def check_port(args):
    port = args["port"]
    _, output = _run(["ss", "-ltnpH"])
    lines = [line for line in output.splitlines() if f":{port} " in line]
    if not lines:
        return f"端口 {port} 没有进程监听"
    result = []
    for line in lines:
        pids = [part.split("=")[1].rstrip(",)") for part in line.replace(",", " ").split() if part.startswith("pid=")]
        owners = "; ".join(f"pid={pid} {_command_line(pid)}" for pid in pids)
        result.append(f"端口 {port} 正在监听：{owners or line}")
    return "\n".join(result)


def list_processes(_):
    _, output = _run(["ps", "-eo", "pid,etimes,args"])
    hidden = ("/sbx/", "ps -eo", "bwrap")
    return "\n".join(line for line in output.splitlines() if not any(item in line for item in hidden))


def service_status(args):
    _, pid_file, log = _service(args["name"])
    pid = _service_pid(pid_file)
    state = f"{args['name']}：运行中，pid={pid}" if pid else f"{args['name']}：未运行"
    return f"{state}\n--- 最近日志 ---\n{_tail(log, 20) or '（无日志）'}"


def http_check(args):
    try:
        with urllib.request.urlopen(args["url"], timeout=5) as response:
            return f"HTTP {response.status}\n{response.read(2000).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}\n{exc.read(2000).decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"连接失败：{getattr(exc, 'reason', exc)}"


def disk_usage(args):
    path = _path(args["path"])
    if not os.path.exists(path):
        raise ToolError(f"路径不存在：{path}")
    usage = _run(["df", "-h", path])[1]
    largest = subprocess.run(f"du -ah -d 1 -- '{path}' 2>/dev/null | sort -rh | head -15", shell=True,
                             capture_output=True, timeout=15).stdout.decode("utf-8", "replace")
    return f"{usage}\n--- 占用最大 ---\n{largest}"


def git_inspect(args):
    views = {"status": ["status"], "log": ["log", "--oneline", "--decorate", "-n", "20"],
             "reflog": ["reflog", "-n", "20"], "branches": ["branch", "-avv"], "diff": ["diff", "--stat"],
             "stash": ["stash", "list", "--stat"]}
    code, output = _run(["git", "-C", _path(args["repo"]), *views[args["view"]]])
    if code:
        raise ToolError(output.strip())
    return output or "（无输出）"


def edit_file(args):
    path = _path(args["path"])
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            content = handle.read()
    except OSError as exc:
        raise ToolError(f"无法读取 {path}：{exc.strerror}") from None
    count = content.count(args["old_text"])
    if count != 1:
        raise ToolError(f"old_text 在文件中出现 {count} 次，必须正好 1 次")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content.replace(args["old_text"], args["new_text"]))
    return f"已修改 {path}"


def write_file(args):
    path = _path(args["path"])
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(args["content"])
    except OSError as exc:
        raise ToolError(f"无法写入 {path}：{exc.strerror}") from None
    return f"已写入 {path}"


def delete_path(args):
    path = _path(args["path"])
    if os.path.abspath(path) in ("/", "/opt", "/srv", "/srv/data", "/var", "/home/dev"):
        raise ToolError(f"拒绝删除：{path}")
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as exc:
        raise ToolError(f"无法删除 {path}：{exc.strerror}") from None
    return f"已删除 {path}"


def chmod_path(args):
    try:
        os.chmod(_path(args["path"]), int(args["mode"], 8))
    except (OSError, ValueError) as exc:
        raise ToolError(f"无法修改权限：{exc}") from None
    return f"已将 {args['path']} 权限改为 {args['mode']}"


def kill_process(args):
    pid = args["pid"]
    if pid <= 2 or not _alive(pid):
        raise ToolError(f"进程不存在或不可结束：{pid}")
    command = _command_line(pid)
    os.kill(pid, signal.SIGKILL if args.get("force") else signal.SIGTERM)
    for _ in range(20):
        if not _alive(pid):
            return f"已结束 pid={pid}：{command}"
        time.sleep(0.1)
    return f"已发送信号，但 pid={pid} 仍在运行"


def service_restart(args):
    name = args["name"]
    run, pid_file, log = _service(name)
    pid = _service_pid(pid_file)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    with open(log, "ab") as output:
        output.write(f"{START_MARK}{time.strftime('%H:%M:%S')} ===\n".encode())
        output.flush()
        try:
            child = subprocess.Popen([run], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        except OSError as exc:
            output.write(f"failed to exec {run}: {exc.strerror}\n".encode())
            output.flush()
            return f"{name} 启动失败\n{_tail(log, 5)}"
    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
    time.sleep(1.0)
    code = child.poll()
    if code is None:
        return f"{name} 已启动，pid={child.pid}"
    return f"{name} 启动后退出，返回码 {code}\n--- 最近日志 ---\n{_tail(log, 12)}"


def run_script(args):
    path = _path(args["path"])
    if not os.path.abspath(path).startswith("/opt/") or not os.path.isfile(path):
        raise ToolError(f"只能运行 /opt 下已有的脚本：{path}")
    argv = ["python3", path] if path.endswith(".py") else ["sh", path]
    code, output = _run([*argv, *args.get("args", [])], timeout=20)
    return f"退出码 {code}\n{output}"


def git_action(args):
    action, ref = args["action"], args.get("ref")
    commands = {"reset_hard": ["reset", "--hard"], "checkout": ["checkout"], "create_branch": ["branch"],
                "merge": ["merge", "--no-edit"], "merge_abort": ["merge", "--abort"],
                "cherry_pick": ["cherry-pick"], "stash_pop": ["stash", "pop"]}
    command = commands[action]
    if action not in ("merge_abort", "stash_pop"):
        if not ref:
            raise ToolError(f"{action} 需要 ref")
        command = [*command, ref]
    code, output = _run(["git", "-C", _path(args["repo"]), *command])
    if code:
        raise ToolError(output.strip() or f"git 退出码 {code}")
    return output or "完成"


def main():
    name, arguments = sys.argv[1], json.loads(sys.argv[2])
    try:
        print(_clip(globals()[name](arguments)))
    except ToolError as exc:
        print(f"错误：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
