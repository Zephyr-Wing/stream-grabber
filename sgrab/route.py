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
import urllib.error
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


def query_keys(url):
    try:
        q = urllib.parse.urlparse(url).query
        return {k.lower() for k, _ in urllib.parse.parse_qsl(q, keep_blank_values=True)}
    except Exception:
        return set()


def is_signed(url):
    """地址是否带时效或来源签名。

    这类链接常把「有效期」和「客户端出口 IP」一起签进去（例如 whip=、sign=），
    换一条网络路径请求就会被判为签名不符，因此下载路径必须与取址时一致。
    """
    keys = query_keys(url)
    strong = {"sign", "signature", "token", "whip", "auth_key", "key", "expires", "expire"}
    weak = {"t", "e", "st", "ts", "time", "tsign", "rand", "nonce"}
    return bool(keys & strong) or len(keys & weak) >= 2


def status_of(url, referer="", proxy=None, timeout=8):
    """探一次地址，返回 (状态码, 响应片段)。连不上时状态码为 None。"""
    try:
        with _fetch(url, proxy, referer, UA, timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200).decode("utf-8", "ignore").strip()
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, str(e)[:80]


def explain_failure(url, referer="", proxy=None):
    """下载失败后复探一次地址，把状态码翻译成能照着做的话。

    返回 (状态码, 提示行列表)。
    """
    code, body = status_of(url, referer, proxy)
    low = (body or "").lower()
    signed = is_signed(url)
    out = []

    if code is None:
        out.append("地址连不上：%s" % body)
        out.append("  检查网络，或换一条路：--via-proxy / --no-proxy")
        return code, out

    if code in (401, 403):
        if "sign" in low or signed:
            out.append("地址返回 %s，签名校验未通过。" % code)
            out.append("  这类链接把有效期和客户端出口 IP 一起签进了参数，只能从取址时")
            out.append("  的同一条网络路径访问。浏览器能播、这里下不了，说明两条路出口 IP 不同：")
            out.append("  常见原因是代理软件按规则分流，把该 CDN 域名走了直连。")
            out.append("  处理：在代理软件里让该 CDN 域名也走代理，或临时开全局模式，")
            out.append("        再用 --via-proxy 重跑；也可以把 --list 输出的地址交给浏览器下载。")
        else:
            out.append("地址返回 %s，多半是 Referer 不对。" % code)
            out.append("  用 --list 查看探测到的 Referer；CDN 与站点不同域时，")
            out.append("  在 config.json 的 referer_map 里登记来源页。")
        return code, out

    if code == 404:
        out.append("地址返回 404，链接可能已过期，重新解析一次。")
        return code, out

    if code == 429:
        out.append("地址返回 429，被限流了。降低 threads 后重试。")
        return code, out

    out.append("地址返回 %s。" % code)
    return code, out


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
