# -*- coding: utf-8 -*-
"""一键把 yt-dlp 和 ffmpeg 装到 tools/ 目录。

默认走 GitHub 直连，失败自动换镜像。
    python setup_tools.py
    python setup_tools.py --mirror https://gh-proxy.com/
"""
import argparse
import os
import platform
import shutil
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(ROOT, "tools")

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")

MIRRORS = ["", "https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/"]


def fetch(url, dest, mirrors=None):
    """下载到 dest，返回实际使用的地址。"""
    last = None
    for m in (mirrors if mirrors is not None else MIRRORS):
        real = (m + url) if m else url
        try:
            print("  下载 %s" % (real[:100]))
            req = urllib.request.Request(real, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                total = 0
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total % (5 * 1048576) < 262144:
                        sys.stdout.write("\r    %.0f MB" % (total / 1048576))
                        sys.stdout.flush()
            print("\r    完成 %.1f MB" % (os.path.getsize(dest) / 1048576))
            return real
        except Exception as e:
            last = e
            print("\r    失败：%s" % str(e)[:70])
            if os.path.exists(dest):
                os.remove(dest)
    raise RuntimeError("全部地址都失败，最后一个错误：%s" % last)


def install_ytdlp():
    dest = os.path.join(TOOLS, "yt-dlp.exe")
    if os.path.isfile(dest):
        print("yt-dlp 已存在，跳过（删除 %s 可重装）" % dest)
        return
    print("安装 yt-dlp…")
    fetch(YTDLP_URL, dest)
    print("  已装到 %s" % dest)


def install_ffmpeg():
    d = os.path.join(TOOLS, "ffmpeg")
    if os.path.isfile(os.path.join(d, "ffmpeg.exe")):
        print("ffmpeg 已存在，跳过")
        return
    os.makedirs(d, exist_ok=True)
    zpath = os.path.join(TOOLS, "ffmpeg.zip")
    print("安装 ffmpeg（约 80MB）…")
    fetch(FFMPEG_URL, zpath)
    print("  解压…")
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            base = os.path.basename(n)
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(n) as src, open(os.path.join(d, base), "wb") as out:
                    shutil.copyfileobj(src, out)
    os.remove(zpath)
    for f in ("ffmpeg-master-latest-win64-gpl",):
        p = os.path.join(TOOLS, f)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    print("  已装到 %s" % d)


def main():
    ap = argparse.ArgumentParser(description="安装 yt-dlp 与 ffmpeg 到 tools/")
    ap.add_argument("--mirror", default=None, help="强制使用指定镜像前缀")
    a = ap.parse_args()

    if a.mirror is not None:
        globals()["MIRRORS"] = [a.mirror]

    if platform.system() != "Windows":
        print("这个脚本只做 Windows 版二进制安装。")
        print("macOS: brew install yt-dlp ffmpeg")
        print("Linux: sudo apt install ffmpeg && pip install yt-dlp")
        return 1

    os.makedirs(TOOLS, exist_ok=True)
    try:
        install_ytdlp()
        install_ffmpeg()
    except Exception as e:
        print()
        print("安装失败：%s" % e)
        print("可以手动下载后放到 tools/ 目录：")
        print("  yt-dlp.exe  ->  " + YTDLP_URL)
        print("  ffmpeg      ->  " + FFMPEG_URL)
        return 1

    print()
    print("完成。现在可以运行：python grab.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
