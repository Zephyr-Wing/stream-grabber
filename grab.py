# -*- coding: utf-8 -*-
"""stream-grabber 命令行入口。

用法示例：
    python grab.py https://example.com/videos/abc/
    python grab.py https://cdn.example.com/hls/xxx.m3u8 -n my_video
    python grab.py <页面地址> --list          # 只解析地址，不下载
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

from sgrab import config as C
from sgrab import referer as R
from sgrab import sniffer as S


def safe_name(s, fallback="video"):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (s or "").strip())
    s = s.strip("._")
    if not s:
        s = fallback + "_" + datetime.datetime.now().strftime("%Y%m%d_%H%M")
    return s[:80]


def name_from_url(u):
    m = re.search(r"/videos?/([A-Za-z0-9_-]+)/?", u)
    if m:
        return m.group(1)
    m = re.search(r"/([A-Za-z0-9_-]+)\.m3u8", u)
    if m and m.group(1) not in ("video", "index", "playlist", "master"):
        return m.group(1)
    return ""


def main():
    C.setup_console()
    ap = argparse.ArgumentParser(
        prog="grab.py",
        description="抓取网页里的 HLS 视频：自动过防盗链、自动嗅探播放地址。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("例子:\n"
                "  python grab.py https://example.com/videos/abc/\n"
                "  python grab.py https://cdn.example.com/hls/a.m3u8 -n demo\n"
                "  python grab.py <页面地址> --list\n"))
    ap.add_argument("url", nargs="?", help="网页地址，或直接的 m3u8 链接")
    ap.add_argument("-n", "--name", default="", help="保存文件名（不含扩展名）")
    ap.add_argument("-o", "--out", default="", help="保存目录，默认读配置")
    ap.add_argument("-c", "--config", default="", help="指定 config.json")
    ap.add_argument("-t", "--threads", type=int, default=0, help="并发分片数，默认 16")
    ap.add_argument("--proxy", default=None, help="页面解析用的代理，如 http://127.0.0.1:7897")
    ap.add_argument("--no-proxy", action="store_true", help="解析页面时不走代理")
    ap.add_argument("--via-proxy", action="store_true", help="下载也走代理（默认直连）")
    ap.add_argument("--list", action="store_true", help="只解析播放地址，不下载")
    args = ap.parse_args()

    cfg = C.load(args.config or None)
    if args.out:
        cfg["out_dir"] = os.path.abspath(args.out)
    if args.threads:
        cfg["threads"] = args.threads
    if args.via_proxy:
        cfg["download_via_proxy"] = True

    yt = C.find_ytdlp(cfg)
    if not yt:
        print("找不到 yt-dlp。")
        print("  方式一：运行 python setup_tools.py 自动安装到 tools/")
        print("  方式二：自行安装并加入 PATH")
        return 2
    ff = C.find_ffmpeg(cfg)

    url = (args.url or "").strip().strip('"').strip("'")
    if not url:
        print("粘贴视频页面地址或 m3u8 链接，回车开始：")
        try:
            url = input("> ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            return 2
        if not url:
            return 2
    if not re.match(r"https?://", url, re.I):
        print("链接需要以 http:// 或 https:// 开头")
        return 2

    proxy = None if args.no_proxy else (
        args.proxy or C.resolve_proxy(cfg))

    print("=" * 60)
    print("输入：%s" % url[:110])

    target, ref = url, None
    if ".m3u8" in url.lower():
        ref = R.find(url, cfg.get("referer_map"))
        if ref is None:
            print()
            print("所有候选来源页都被拒绝。常见原因：")
            print("  1) 链接过期 —— 这类地址通常几小时失效，重新抓一条")
            print("  2) 需要登录状态")
            print("  3) CDN 校验了客户端指纹（换用页面地址模式，由浏览器代取）")
            return 1
    else:
        print("这是网页地址，启动浏览器解析播放地址…")
        target, ref = S.resolve(url, proxy=proxy)
        if not target:
            print()
            print("没解析出播放地址。检查：")
            print("  1) 站点是否需要代理才能打开（试试 --proxy 或先开代理）")
            print("  2) 页面上是否真的有视频")
            return 1
        print()
        print("播放地址：%s" % target[:100])

    name = safe_name(args.name or name_from_url(url) or name_from_url(target))

    if args.list:
        print()
        print("名称  : %s" % name)
        print("地址  : %s" % target)
        print("Referer: %s" % (ref or "(不需要)"))
        return 0

    out_dir = C.ensure_out(cfg)
    out_tmpl = os.path.join(out_dir, name + ".%(ext)s")

    extra = []
    if ff:
        extra += ["--ffmpeg-location", ff]
    if cfg.get("download_via_proxy") and proxy:
        extra += ["--proxy", proxy]

    cmd = [yt, target,
           "--referer", ref or ("https://" + R.host_of(target) + "/"),
           "--user-agent", S.UA,
           "-N", str(cfg["threads"]),
           "--retries", str(cfg["retries"]),
           "--fragment-retries", str(cfg["fragment_retries"]),
           "--socket-timeout", str(cfg["socket_timeout"]),
           "--no-warnings",
           "-o", out_tmpl] + extra

    print("=" * 60)
    print("保存到：%s" % os.path.normpath(os.path.join(out_dir, name + ".mp4")))
    print("%d 线程下载；中断后重跑同一链接会自动续传" % cfg["threads"])
    print("=" * 60)
    print()
    sys.stdout.flush()

    try:
        rc = subprocess.call(cmd)
    except KeyboardInterrupt:
        print()
        print("已中断。重跑同一条命令即可续传。")
        return 130

    print()
    done = []
    for f in os.listdir(out_dir):
        if not f.startswith(name):
            continue
        if ".part" in f or ".ytdl" in f:
            continue
        p = os.path.join(out_dir, f)
        if os.path.isfile(p) and os.path.getsize(p) > 10240:
            done.append((f, os.path.getsize(p)))

    if done:
        print("完成：")
        for f, size in done:
            print("  %s  %.1f MB" % (f, size / 1048576))
        return 0

    if rc in (130, 143, -2, -15):
        print("已中断。重跑同一条命令即可从断点续传。")
        return 130

    print("没有生成文件（yt-dlp 退出码 %s）" % rc)
    print("把上面的报错发出来，或试试加上 --via-proxy。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
