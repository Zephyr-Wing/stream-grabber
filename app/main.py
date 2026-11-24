# -*- coding: utf-8 -*-
"""启动入口：起本地服务与任务线程。

    python -m app
    python -m app --port 8765
"""
import argparse
import atexit
import json
import os
import sys
import urllib.request

from . import api, config, jobs

PID_FILE = os.path.join(config.ROOT, ".tmp", "server.pid")


def _write_pid():
    """记录进程号，供扩展的「停止服务」与宿主脚本使用。"""
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(_drop_pid)
    except OSError:
        pass


def _drop_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            if f.read().strip() == str(os.getpid()):
                os.remove(PID_FILE)
    except Exception:
        pass


def _already_running(port):
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:%d/v2/ping" % port, timeout=1) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if d.get("app") == "stream-grabber" else None
    except Exception:
        return None


def main():
    config.setup_console()
    ap = argparse.ArgumentParser(prog="stream-grabber")
    ap.add_argument("-p", "--port", type=int, default=0)
    ap.add_argument("--open", action="store_true", help="启动后打开下载目录")
    args = ap.parse_args()

    cfg = config.load()
    port = args.port or int(cfg.get("server_port") or 8765)

    running = _already_running(port)
    if running:
        print("服务已在运行（端口 %d，版本 %s）。" % (port, running.get("version")))
        print("去浏览器点扩展图标即可。")
        return 0

    yt, ff = jobs.start(cfg)
    httpd, port = api.serve(cfg, yt, ff, port)
    _write_pid()

    print("=" * 58)
    print(" stream-grabber 本地服务")
    print("=" * 58)
    print(" 监听    127.0.0.1:%d（仅本机）" % port)
    print(" 保存到  %s" % cfg["out_dir"])
    print(" 并发    %d" % cfg["threads"])
    print(" 路径    %s" % cfg["route_mode"])
    print(" yt-dlp  %s" % ("OK" if yt else "未找到（放进 tools/ 或 PATH）"))
    print(" ffmpeg  %s" % ("OK" if ff else "未找到（音视频分离的站会合并不了）"))
    print()
    print(" 现在去浏览器点扩展图标。窗口保持运行，关掉就用不了了。")
    print("=" * 58)
    print()

    if args.open and os.name == "nt":
        os.startfile(cfg["out_dir"])

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
