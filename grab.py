# -*- coding: utf-8 -*-
"""stream-grabber 命令行入口。

两条路线：
  1) 页面用 HLS（m3u8）—— 真实浏览器嗅探播放地址，再交给 yt-dlp 多线程搬
  2) 页面不用 HLS（DASH / mpd，如 B 站）—— 嗅探不到时自动回落到 yt-dlp 的站点解析器

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

# 优先 H.264：AV1 / HEVC 在部分播放器和剪辑软件里不认
FMT_PREF = "bv*[vcodec^=avc1]+ba/bv*+ba/b"


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


def _decode(b):
    """子进程输出在 Windows 上可能是 GBK 也可能是 UTF-8，两种都试。"""
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def probe_extractor(yt, url, proxy, cfg):
    """问 yt-dlp：这个站你认识吗？认得就返回 (True, 标题)。"""
    cmd = [yt, url,
           "--simulate", "--no-playlist", "--no-warnings",
           "--socket-timeout", str(cfg["socket_timeout"]),
           "--print", "%(title)s"]
    if proxy:
        cmd += ["--proxy", proxy]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env)
    except Exception:
        return False, ""
    out = _decode(p.stdout).strip()
    if p.returncode != 0 or not out:
        return False, ""
    return True, out.splitlines()[0].strip()


def main():
    C.setup_console()
    ap = argparse.ArgumentParser(
        prog="grab.py",
        description="抓取网页视频：HLS 页面用浏览器嗅探，非 HLS 页面回落到 yt-dlp 解析器。",
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
    ap.add_argument("--no-fallback", action="store_true",
                    help="嗅探不到 m3u8 时不要回落到 yt-dlp 站点解析器")
    ap.add_argument("--cookies", default="", metavar="BROWSER",
                    help="从浏览器读登录态，如 chrome / edge（下 4K、会员内容用）")
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
    page_mode = False       # True = 把整个页面交给 yt-dlp 的解析器
    title = ""

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
            print("页面上没有 m3u8 流 —— 这个站多半不用 HLS（B 站、爱奇艺这类走 DASH/mpd），")
            print("嗅探器按 m3u8 过滤，所以看不见。")
            if args.no_fallback:
                return 1
            print()
            print("改用 yt-dlp 的站点解析器直接试…")
            ok, title = probe_extractor(yt, url, proxy, cfg)
            if not ok:
                print()
                print("yt-dlp 也不认识这个站。可能原因：")
                print("  1) 站点需要登录（试试 --cookies chrome）")
                print("  2) 页面真的没有视频，或视频已被删除")
                print("  3) 站点太新或太小，两边都还没支持")
                return 1
            print("认出来了：《%s》" % title[:60])
            target, ref = url, None
            page_mode = True
        else:
            print()
            print("播放地址：%s" % target[:100])

    name = safe_name(args.name
                     or name_from_url(url)
                     or (title if page_mode else name_from_url(target)))

    if args.list:
        print()
        print("名称  : %s" % name)
        print("路线  : %s" % ("yt-dlp 站点解析器（非 HLS）" if page_mode else "浏览器嗅探 m3u8"))
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
    if args.cookies:
        extra += ["--cookies-from-browser", args.cookies]

    if page_mode:
        # 整页交给解析器时不要硬塞 Referer / UA，某些站的鉴权会因此坏掉
        cmd = [yt, target,
               "-f", FMT_PREF,
               "--merge-output-format", "mp4",
               "--no-playlist",
               "--retries", str(cfg["retries"]),
               "--socket-timeout", str(cfg["socket_timeout"]),
               "--no-warnings",
               "-o", out_tmpl] + extra
    else:
        # Referer 只在拿得到主机名时传，避免拼出非法的 https:///
        h = R.host_of(target)
        ref_url = ref or ("https://" + h + "/" if h else "")
        cmd = [yt, target]
        if ref_url:
            cmd += ["--referer", ref_url]
        cmd += ["--user-agent", S.UA,
                "-N", str(cfg["threads"]),
                "--retries", str(cfg["retries"]),
                "--fragment-retries", str(cfg["fragment_retries"]),
                "--socket-timeout", str(cfg["socket_timeout"]),
                "--no-warnings",
                "-o", out_tmpl] + extra

    print("=" * 60)
    print("保存到：%s" % os.path.normpath(os.path.join(out_dir, name + ".mp4")))
    if page_mode:
        print("使用 yt-dlp 站点解析器；音视频分离时会自动合并为 mp4")
    else:
        print("%d 线程下载；中断后重跑同一链接会自动续传" % cfg["threads"])
    print("=" * 60)
    print()

    try:
        rc = subprocess.call(cmd)
    except KeyboardInterrupt:
        print()
        print("已中断。重跑同一条命令即可续传。")
        return 130

    print()
    done = []
    for f in os.listdir(out_dir):
        stem = os.path.splitext(f)[0]
        if stem != name and not f.startswith(name + "."):
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
