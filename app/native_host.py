# -*- coding: utf-8 -*-
"""Native Messaging 宿主：让扩展能够启动与停止本地服务。

浏览器插件没有启动进程的权限，唯一通道是本机已登记的宿主程序。
这里实现四件事：

  python -m app.native_host --install [扩展ID]   写宿主清单 + 注册表
  python -m app.native_host --uninstall          撤销登记
  python -m app.native_host                      以宿主身份运行（由浏览器拉起）
  python -m app.native_host --status             命令行查看服务状态

协议：stdin/stdout 上 4 字节小端长度 + UTF-8 JSON。stdout 只允许出现协议数据，
所有日志走 stderr。
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from . import config  # noqa: E402

HOST_NAME = "com.streamgrabber.host"
MANIFEST = os.path.join(ROOT, "native-host", HOST_NAME + ".json")
LAUNCHER = os.path.join(ROOT, "launch_host.bat")
PID_FILE = os.path.join(ROOT, ".tmp", "server.pid")

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
# 尝试脱离父进程所在的 Job 对象：否则浏览器退出时可能把服务一起带走
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _log(msg):
    sys.stderr.write("[native] %s\n" % msg)
    sys.stderr.flush()


# ----------------------------------------------------------------- 协议

def read_message(stream):
    raw = stream.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    if not n or n > 64 * 1024 * 1024:
        return None
    body = stream.read(n)
    if len(body) < n:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(stream, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


# ----------------------------------------------------------------- 服务状态

def ping(timeout=1.5):
    port = int(config.load().get("server_port") or 8765)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open("http://127.0.0.1:%d/v2/ping" % port, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _pids():
    """所有在跑的服务进程（此处只能确认端口是否被监听，pid 由服务自己写）。"""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def start_server(wait=20):
    info = ping()
    if info:
        return {"ok": True, "already": True, "version": info.get("version")}

    cfg = config.load()
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 隐藏启动没有控制台，输出落到 .tmp/server.log，出问题时有据可查
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    try:
        logf = open(os.path.join(ROOT, ".tmp", "server.log"), "ab", buffering=0)
    except OSError:
        logf = subprocess.DEVNULL
    base = dict(cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT)
    try:
        subprocess.Popen([exe, "-m", "app"],
                         creationflags=(DETACHED_PROCESS | CREATE_NO_WINDOW
                                        | CREATE_BREAKAWAY_FROM_JOB), **base)
    except OSError:
        # 宿主所在的 Job 不允许 breakaway 时退回普通分离方式
        try:
            subprocess.Popen([exe, "-m", "app"],
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, **base)
        except Exception as e:
            _log("启动失败：%s" % e)
            return {"ok": False, "error": "无法启动服务：%s" % e}
    except Exception as e:
        _log("启动失败：%s" % e)
        return {"ok": False, "error": "无法启动服务：%s" % e}

    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.4)
        info = ping(timeout=1.0)
        if info:
            return {"ok": True, "started": True, "version": info.get("version"),
                    "port": cfg.get("server_port")}
    return {"ok": False, "error": "服务已拉起但 %d 秒内没有响应，查看 .tmp 下的日志" % wait}


def stop_server():
    # 端口已经没人应答，说明服务本来就没开；顺手清掉可能残留的 pid 文件
    if not ping():
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return {"ok": True, "stopped": True, "note": "服务本来就没在运行"}

    pid = _pids()
    if not pid:
        return {"ok": False, "error": "服务在运行但找不到进程号，请手动关闭"}

    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception as e:
        return {"ok": False, "error": "结束进程失败：%s" % e}

    for _ in range(20):
        time.sleep(0.3)
        if not ping():
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return {"ok": True, "stopped": True}
    return {"ok": False, "error": "进程已结束但端口仍在响应"}


def handle(payload):
    cmd = (payload.get("cmd") or "status").lower()
    if cmd == "start":
        return start_server()
    if cmd == "stop":
        return stop_server()
    if cmd == "status":
        info = ping()
        return {"ok": True, "running": bool(info),
                "version": (info or {}).get("version")}
    return {"ok": False, "error": "未知命令：%s" % cmd}


# ----------------------------------------------------------------- 宿主登记

def _chrome_profiles():
    bases = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"),
    ]
    for base in bases:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if name in ("Default", "System Profile") or name.startswith("Profile"):
                yield d


def detect_extension_id():
    """从未打包扩展的路径反查它的 ID。

    Chrome 的 ID 由路径推导，规则随版本变化，直接读配置更稳：
    每个 profile 的 Secure Preferences 里记着扩展的 path 与 id。
    """
    target = os.path.join(ROOT, "ext").lower()
    for prof in _chrome_profiles():
        for fname in ("Secure Preferences", "Preferences"):
            path = os.path.join(prof, fname)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            settings = (data.get("extensions") or {}).get("settings") or {}
            for eid, val in settings.items():
                p = ((val or {}).get("path") or "").lower().replace("/", "\\")
                if p.rstrip("\\") == target.rstrip("\\"):
                    return eid
    return ""


def _write_manifest(ext_ids):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    manifest = {
        "name": HOST_NAME,
        "description": "stream-grabber 本地服务启动器",
        "path": LAUNCHER,
        "type": "stdio",
        "allowed_origins": ["chrome-extension://%s/" % i for i in ext_ids],
    }
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return MANIFEST


def _browsers():
    return [
        ("Chrome", r"Software\Google\Chrome\NativeMessagingHosts"),
        ("Edge", r"Software\Microsoft\Edge\NativeMessagingHosts"),
        ("Chromium", r"Software\Chromium\NativeMessagingHosts"),
    ]


def install(ext_ids, dry_run=False):
    if not ext_ids:
        return {"ok": False, "error": "没有拿到扩展 ID，先加载扩展再运行本命令"}
    if not dry_run:
        _write_manifest(ext_ids)
    regs = []
    try:
        import winreg
    except ImportError:
        return {"ok": False, "error": "只支持 Windows"}
    for label, sub in _browsers():
        key = sub + "\\" + HOST_NAME
        regs.append(label)
        if dry_run:
            continue
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, MANIFEST)
        except Exception as e:
            return {"ok": False, "error": "写入注册表失败（%s）：%s" % (label, e)}
    return {"ok": True, "manifest": MANIFEST, "extension_ids": ext_ids,
            "browsers": regs}


def uninstall():
    try:
        import winreg
    except ImportError:
        return {"ok": False, "error": "只支持 Windows"}
    for _label, sub in _browsers():
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub + "\\" + HOST_NAME)
        except FileNotFoundError:
            pass
        except Exception as e:
            _log("删除失败 %s: %s" % (sub, e))
    try:
        os.remove(MANIFEST)
    except OSError:
        pass
    return {"ok": True}


# ----------------------------------------------------------------- 入口

def serve():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            msg = read_message(stdin)
        except Exception as e:
            _log("读取失败：%s" % e)
            return 1
        if msg is None:
            return 0
        try:
            reply = handle(msg)
        except Exception as e:
            reply = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        write_message(stdout, reply)


def main(argv):
    args = argv[1:]
    if "--install" in args:
        explicit = [a for a in args if not a.startswith("-")]
        if not explicit:
            found = detect_extension_id()
            explicit = [found] if found else []
        r = install(explicit)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if "--uninstall" in args:
        print(json.dumps(uninstall(), ensure_ascii=False))
        return 0
    if "--status" in args:
        print(json.dumps(handle({"cmd": "status"}), ensure_ascii=False))
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
