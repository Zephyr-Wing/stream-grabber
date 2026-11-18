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
import atexit
import datetime
import os
import re
import subprocess
import sys
import tempfile

from sgrab import config as C
from sgrab import referer as R
from sgrab import route as Route
from sgrab import sniffer as S

# 优先 H.264：AV1 / HEVC 在部分播放器和剪辑软件里不认
FMT_PREF = "bv*[vcodec^=avc1]+ba/bv*+ba/b"


def safe_name(s, fallback="video"):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (s or "").strip())
    s = s.strip("._")
    if not s:
        s = fallback + "_" + datetime.datetime.now().strftime("%Y%m%d_%H%M")
    return s[:80]


def _drop(path):
    """cookie 文件里是登录凭据，用完立刻删掉，别留在硬盘上。"""
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


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


def probe_extractor(yt, url, proxy, cfg, jar=None):
    """问 yt-dlp：这个站你认识吗？认得就返回 (True, 标题)。"""
    cmd = [yt, url,
           "--simulate", "--no-playlist", "--no-warnings",
           "--socket-timeout", str(cfg["socket_timeout"]),
           "--print", "%(title)s"]
    if proxy:
        cmd += ["--proxy", proxy]
    if jar:
        cmd += ["--cookies", jar]
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


def _expand_argv(argv):
    """把粘在同一行里的「地址 --参数」拆开。

    grab.bat 的交互输入整行是一个参数，用户很自然会在地址后面直接跟
    --cdp-port 9222。不拆的话它会被当成地址的一部分，打开一个坏链接。
    """
    out = []
    for a in argv:
        a = (a or "").strip()
        if not a:
            continue
        if len(a) > 1 and a[0] in "\"'" and a[-1] == a[0]:
            a = a[1:-1].strip()
        if "://" in a and " " in a:
            out.extend(x for x in a.split() if x)
        elif a:
            out.append(a)
    return out


def build_parser():
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
    ap.add_argument("--no-proxy", action="store_true", help="解析页面和下载都不走代理")
    ap.add_argument("--via-proxy", action="store_true", help="下载阶段强制走代理（默认 auto：探测到就用）")
    ap.add_argument("--no-fallback", action="store_true",
                    help="嗅探不到 m3u8 时不要回落到 yt-dlp 站点解析器")
    ap.add_argument("--cookies", default="", metavar="BROWSER",
                    help="让 yt-dlp 读浏览器的 cookie 库，如 chrome / edge（要求该浏览器已关闭）")
    ap.add_argument("--guest", action="store_true",
                    help="用一次性配置，不带任何登录态")
    ap.add_argument("--cdp-port", type=int, default=0, metavar="PORT",
                    help="连一个已开着调试端口的浏览器，不用关它，例如 9222")
    ap.add_argument("--list", action="store_true", help="只解析播放地址，不下载")
    return ap


def main():
    C.setup_console()
    ap = build_parser()

    argv = _expand_argv(sys.argv[1:])
    args = ap.parse_args(argv)

    url = (args.url or "").strip().strip('"').strip("'")
    if not url:
        print("粘贴视频页面地址或 m3u8 链接，回车开始：")
        print("（一般直接回车就行；要带参数就写在地址后面，比如 ... --list）")
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 2
        if not line:
            return 2
        args = ap.parse_args(_expand_argv(argv + [line]))
        url = (args.url or "").strip().strip('"').strip("'")

    if not re.match(r"https?://", url, re.I):
        print("链接需要以 http:// 或 https:// 开头")
        return 2

    cfg = C.load(args.config or None)
    if args.out:
        cfg["out_dir"] = os.path.abspath(args.out)
    if args.threads:
        cfg["threads"] = args.threads
    if args.via_proxy:
        cfg["download_via_proxy"] = True
    if args.no_proxy:
        # --no-proxy 是「完全不碰代理」，下载阶段也跟着直连
        cfg["download_via_proxy"] = False

    yt = C.find_ytdlp(cfg)
    if not yt:
        print("找不到 yt-dlp。")
        print("  方式一：运行 python setup_tools.py 自动安装到 tools/")
        print("  方式二：自行安装并加入 PATH")
        return 2
    ff = C.find_ffmpeg(cfg)

    # 登录态：默认就用专用配置，不问、也不要求你关浏览器。
    #   一、已经开着调试端口（专用浏览器没关）—— 附着过去，省一次启动
    #   二、否则用专用配置启动一份，登录过的站，登录态还在里面
    #   三、--guest 才用一次性配置，什么登录态都不带
    is_page = ".m3u8" not in url.lower()
    profile_dir = None

    if is_page and not args.cdp_port:
        p = C.find_debug_port(cfg.get("cdp_port") or 9222)
        if p:
            args.cdp_port = p
            print("[登录态] 连上已开的调试浏览器（端口 %d）" % p)
        elif args.guest:
            print("[登录态] 访客模式，不带登录信息")
        else:
            profile_dir = C.profile_dir(cfg)
            print("[登录态] 专用浏览器配置：%s" % profile_dir)
            print("        （第一次用、或者画质偏低：先双击 chrome-debug.bat 登录一次）")

    proxy = None if args.no_proxy else (
        args.proxy or C.resolve_proxy(cfg))

    print("=" * 60)
    print("输入：%s" % url[:110])

    target, ref = url, None
    page_mode = False       # True = 把整个页面交给 yt-dlp 的解析器
    title = ""
    jar = None              # 从已登录浏览器导出的 cookie，下载阶段要用

    if args.cookies and S.browser_running():
        print("[!] --cookies 要读浏览器的 cookie 库，而它现在开着，多半会失败。")
        print("    想带登录态，改读已登录的专用配置（默认行为），或用 --cdp-port 附着。")

    if ".m3u8" in url.lower():
        ref = R.find(url, cfg.get("referer_map"), proxy=proxy)
        if ref is None:
            print()
            print("所有候选来源页都被拒绝。常见原因：")
            print("  1) 链接过期 —— 这类地址通常几小时失效，重新抓一条")
            print("  2) 需要登录状态")
            print("  3) CDN 校验了客户端指纹（换用页面地址模式，由浏览器代取）")
            print("  4) 该 CDN 需要走代理 —— 加 --proxy 指定，或检查 config.json 的 proxy")
            return 1
    else:
        print("这是网页地址，启动浏览器解析播放地址…")
        # 有登录态才值得导出 cookie；访客会话导出来对下载没帮助
        jar_path = None
        if args.cdp_port or profile_dir:
            fd, jar_path = tempfile.mkstemp(prefix="sgrab_cookies_", suffix=".txt")
            os.close(fd)
            os.remove(jar_path)   # 交给 sniffer 自己创建
            # 里面是登录凭据，正常路径会删掉；中途异常或被 Ctrl+C 断掉时
            # 也靠这个兜底，别把它留在临时目录里
            atexit.register(_drop, jar_path)
        try:
            target, ref, jar = S.resolve(
                url, proxy=proxy,
                user_data_dir=profile_dir,
                cdp_port=(args.cdp_port or None),
                cookie_jar=jar_path)
        except S.SnifferError as e:
            # 这类错误重试无用，也不该伪装成「页面没有视频」
            _drop(jar_path)
            print()
            print("[X] 无法解析页面：%s" % e)
            print()
            print("检查顺序：")
            print("  1) 依赖：pip install -r requirements.txt（用运行本脚本的同一个 Python）")
            print("  2) 浏览器：先双击 chrome-debug.bat 登录一次，再重跑")
            print("  3) 调试端口：已被占用时用 --cdp-port 指定，或先关掉那个浏览器")
            return 2

        if not target:
            print()
            print("页面上没有 m3u8 流 —— 这个站多半不用 HLS（B 站、爱奇艺这类走 DASH/mpd），")
            print("嗅探器按 m3u8 过滤，所以看不见。")
            if args.no_fallback:
                return 1
            print()
            print("改用 yt-dlp 的站点解析器直接试…")
            ok, title = probe_extractor(yt, url, proxy, cfg, jar)
            if not ok:
                print()
                print("yt-dlp 也不认识这个站。可能原因：")
                print("  1) 站点需要登录（先在 chrome-debug.bat 打开的窗口里登录一次，再重跑）")
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
        route_desc = ("yt-dlp 站点解析器（非 HLS）" if page_mode
                      else ("m3u8 直链" if not is_page else "浏览器嗅探 m3u8"))
        mode = C.proxy_mode(cfg)
        if mode == "off" or not proxy:
            path = "直连"
        elif mode == "on":
            path = "代理 %s" % proxy
        elif page_mode:
            path = "代理 %s（失败改直连）" % proxy
        elif mode == "smart":
            path = "测速择优（直连 vs 代理 %s）" % proxy
        else:
            path = "直连（不通时改代理 %s）" % proxy
        print()
        print("名称  : %s" % name)
        print("路线  : %s" % route_desc)
        print("地址  : %s" % target)
        print("Referer: %s" % (ref or "(不需要)"))
        print("下载路径: %s" % path)
        if Route.is_signed(target):
            print("备注  : 该地址带签名参数，只能从取地址时的同一条网络路径访问")
        print("登录态: %s" % ("已导出" if jar else "无（访客身份，画质可能被限制）"))
        _drop(jar)
        return 0

    out_dir = C.ensure_out(cfg)
    out_tmpl = os.path.join(out_dir, name + ".%(ext)s")

    # Referer 只在拿得到主机名时传，避免拼出非法的 https:///
    _h = R.host_of(target)
    ref_url = ref or ("https://" + _h + "/" if _h else "")

    common = []
    if ff:
        common += ["--ffmpeg-location", ff]
    if jar:
        common += ["--cookies", jar]
    elif args.cookies:
        common += ["--cookies-from-browser", args.cookies]

    def build_cmd(dl_proxy):
        extra = list(common)
        if dl_proxy:
            extra += ["--proxy", dl_proxy]
        if page_mode:
            # 整页交给解析器时不要硬塞 Referer / UA，某些站的鉴权会因此坏掉
            return [yt, target,
                    "-f", FMT_PREF,
                    "--merge-output-format", "mp4",
                    "--no-playlist",
                    "--retries", str(cfg["retries"]),
                    "--socket-timeout", str(cfg["socket_timeout"]),
                    "--no-warnings",
                    "-o", out_tmpl] + extra
        c = [yt, target]
        if ref_url:
            c += ["--referer", ref_url]
        return c + ["--user-agent", S.UA,
                    "-N", str(cfg["threads"]),
                    "--retries", str(cfg["retries"]),
                    "--fragment-retries", str(cfg["fragment_retries"]),
                    "--socket-timeout", str(cfg["socket_timeout"]),
                    "--no-warnings",
                    "-o", out_tmpl] + extra

    def collect():
        out = []
        try:
            names = os.listdir(out_dir)
        except OSError:
            return out
        for f in names:
            stem = os.path.splitext(f)[0]
            if stem != name and not f.startswith(name + "."):
                continue
            if ".part" in f or ".ytdl" in f:
                continue
            p = os.path.join(out_dir, f)
            if os.path.isfile(p) and os.path.getsize(p) > 10240:
                out.append((f, os.path.getsize(p)))
        return out

    # 下载走哪条网络路径
    mode = C.proxy_mode(cfg)
    if not proxy:
        attempts = [None]
    elif mode == "off":
        attempts = [None]
    elif mode == "on":
        attempts = [proxy, None]
    elif page_mode:
        # 整页交给解析器时无法预取分片地址，按「页面多半需要代理」先走代理
        attempts = [proxy, None]
    elif Route.is_signed(target):
        # 签名常把取址时的出口 IP 一并签进去，先走同一条路
        attempts = [proxy, None]
    else:
        use_proxy, _, _ = Route.pick(target, ref_url, S.UA, proxy,
                                     smart=(mode == "smart"))
        attempts = [use_proxy, proxy if use_proxy is None else None]

    def route_name(p):
        return p or "直连"

    print("=" * 60)
    print("保存到：%s" % os.path.normpath(os.path.join(out_dir, name + ".mp4")))
    print("代理    ：%s" % route_name(attempts[0]))
    if page_mode:
        print("使用 yt-dlp 站点解析器；音视频分离时会自动合并为 mp4")
    else:
        print("%d 线程下载；中断后重跑同一链接会自动续传" % cfg["threads"])
    print("=" * 60)
    print()

    rc = 0
    try:
        for i, use_proxy in enumerate(attempts):
            if i:
                print()
                print("%s 未能完成下载，改用%s重试…"
                      % (route_name(attempts[0]), route_name(use_proxy)))
                print()
            rc = subprocess.call(build_cmd(use_proxy))
            done = collect()
            if done:
                print()
                print("完成：")
                for f, size in done:
                    print("  %s  %.1f MB" % (f, size / 1048576))
                return 0
            if rc in (130, 143, -2, -15):
                print()
                print("已中断。重跑同一条命令即可从断点续传。")
                return 130
    except KeyboardInterrupt:
        print()
        print("已中断。重跑同一条命令即可从断点续传。")
        return 130
    finally:
        _drop(jar)

    print()
    print("没有生成文件（yt-dlp 退出码 %s）" % rc)
    # 复探一次地址，把 403 这类含糊的错误翻译成具体原因
    code, lines = Route.explain_failure(target, ref_url, proxy)
    if lines:
        print()
        for line in lines:
            print(line)
    if code != 403:
        print()
        print("海外站点可试 --via-proxy，国内站点可试 --no-proxy。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
