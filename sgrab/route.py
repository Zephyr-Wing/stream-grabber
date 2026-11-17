# -*- coding: utf-8 -*-
"""选择下载路径：对目标 CDN 分别测一次直连与代理的实际吞吐，取快的那条。

为什么需要实测：
  页面所在域名和视频所在 CDN 常常不是一台机器。前者往往被墙、必须走代理，
  后者有时直连很快（运营商线路好），有时只有代理能跑满。固定用其中一种，
  总有一半场景是慢的。这里各拉一小段分片比一比，按结果决定。

代价是每次下载多花 1-2 秒，换来的是不会拿错路径跑完一整个文件。
"""
import ssl
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 单个样本：先预热读一段（吃掉 TLS 握手和慢启动），再计时读一段
WARMUP_BYTES = 65536
TIMED_BYTES = 262144
SAMPLE_TIMEOUT = 8
# 单次采样最多占用这么久，避免慢线路把测速拖成十几秒
SAMPLE_BUDGET = 1.6
# 每条路径采两次取最大值：CDN 常在单个连接上抖动，单次采样会误判
ROUNDS = 2
# 代理要比直连快这么多倍才值得切换，避免在误差范围内来回跳
SWITCH_MARGIN = 1.3


def _opener(proxy):
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _fetch(url, proxy, referer, ua, timeout):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua or UA)
    req.add_header("Accept", "*/*")
    if referer:
        req.add_header("Referer", referer)
    return _opener(proxy).open(req, timeout=timeout)


def first_segment(playlist_url, proxy, referer="", ua=UA, depth=0):
    """从播放列表里取出第一个分片地址；master 列表就往下钻一层。"""
    if depth > 2:
        return None
    with _fetch(playlist_url, proxy, referer, ua, SAMPLE_TIMEOUT) as r:
        txt = r.read(300000).decode("utf-8", "ignore")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    uris = [l for l in lines if not l.startswith("#")]
    if not uris:
        return None
    first = urllib.parse.urljoin(playlist_url, uris[0])
    if "#EXT-X-STREAM-INF" in txt:
        return first_segment(first, proxy, referer, ua, depth + 1)
    return first


def throughput(url, proxy, referer="", ua=UA):
    """实际吞吐（字节/秒），另返回读到的字节数。

    握手与慢启动会吃掉开头几百毫秒，先预热读一段再开始计时，
    否则小样本会把每条路都测成几百 KB/s。整体受 SAMPLE_BUDGET 约束。
    """
    warm = 0
    got = 0
    hard_deadline = time.time() + SAMPLE_BUDGET
    try:
        with _fetch(url, proxy, referer, ua, SAMPLE_TIMEOUT) as r:
            while warm < WARMUP_BYTES and time.time() < hard_deadline:
                b = r.read(16384)
                if not b:
                    break
                warm += len(b)
            if not warm:
                return 0.0, 0
            start = time.time()
            while got < TIMED_BYTES and time.time() < hard_deadline:
                b = r.read(16384)
                if not b:
                    break
                got += len(b)
    except Exception:
        return 0.0, warm
    if not got:
        return 0.0, warm
    return got / max(time.time() - start, 0.05), warm + got


def best_rate(url, proxy, referer="", ua=UA):
    """采 ROUNDS 次取最快的一次，压掉单连接的抖动。"""
    best, total = 0.0, 0
    for _ in range(ROUNDS):
        rate, got = throughput(url, proxy, referer, ua)
        best = max(best, rate)
        total = max(total, got)
        if rate and rate > 4 * 1048576:   # 已经够快，没必要再采
            break
    return best, total


def pick(playlist_url, referer="", ua=UA, proxy=None, smart=False, log=print):
    """决定下载走哪条路。

    返回 (proxy_to_use, 直连速率, 代理速率)，proxy_to_use 为 None 表示直连。

    默认策略是直连优先：分片所在的 CDN 与页面往往不是一台机器，多数情况下
    直连更快（省掉代理这一跳），只有直连完全取不到数据时才改用代理。
    单连接短采样的速率受 CDN 抖动影响很大，不适合作为普遍依据；
    需要「谁快用谁」时把 smart 打开，两路各测两次再比。
    """
    if not proxy:
        return None, 0.0, 0.0

    seg = None
    for p in (None, proxy):          # 直连先试，取不到再借代理拿播放列表
        try:
            seg = first_segment(playlist_url, p, referer, ua)
        except Exception:
            seg = None
        if seg:
            break
    if not seg:
        log("  [路径] 拿不到分片地址，按直连处理")
        return None, 0.0, 0.0

    d_rate, d_got = best_rate(seg, None, referer, ua)

    if not smart:
        if d_got:
            log("  [路径] 直连可用（%.1f MB/s）" % (d_rate / 1048576))
            return None, d_rate, 0.0
        p_rate, p_got = best_rate(seg, proxy, referer, ua)
        if p_got:
            log("  [路径] 直连不通，改用代理（%.1f MB/s）" % (p_rate / 1048576))
            return proxy, 0.0, p_rate
        log("  [路径] 两条路都取不到分片，按直连处理")
        return None, 0.0, 0.0

    p_rate, p_got = best_rate(seg, proxy, referer, ua)

    if not d_got and not p_got:
        log("  [路径] 两条路都取不到样本，按直连处理")
        return None, 0.0, 0.0
    if not d_got:
        log("  [路径] 直连不通，改用代理（%.1f MB/s）" % (p_rate / 1048576))
        return proxy, 0.0, p_rate
    if not p_got:
        log("  [路径] 代理不通，使用直连（%.1f MB/s）" % (d_rate / 1048576))
        return None, d_rate, 0.0

    use_proxy = p_rate > d_rate * SWITCH_MARGIN
    log("  [路径] 直连 %.1f MB/s，代理 %.1f MB/s → 使用%s"
        % (d_rate / 1048576, p_rate / 1048576, "代理" if use_proxy else "直连"))
    return (proxy if use_proxy else None), d_rate, p_rate
