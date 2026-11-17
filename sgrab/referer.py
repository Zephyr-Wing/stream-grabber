# -*- coding: utf-8 -*-
"""防盗链探测：找出能让播放列表返回 200 的 Referer。

很多 CDN 会校验 Referer，请求头缺失或来源不对就返回 403。
这里先试裸请求，再按「配置映射 → 站点根域 → 带 www → CDN 自身」的顺序逐个试。
"""
import re
import ssl
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def host_of(url):
    m = re.match(r"https?://([^/:]+)", url, re.I)
    return m.group(1).lower() if m else ""


# 二级后缀：这些情况下根域要取三段，否则 com.cn / co.uk 会被当成站点主体
TWO_LEVEL_TLD = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "co.jp", "or.jp", "ne.jp", "ac.jp",
    "com.tw", "com.hk", "com.mo", "com.sg", "com.au", "com.br",
    "co.kr", "co.nz", "co.za", "co.in", "com.my", "com.tr",
}


def root_of(host):
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_TLD:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def candidates(url, extra_map=None):
    """生成候选 Referer 列表。"""
    h = host_of(url)
    out = []
    for key, refs in (extra_map or {}).items():
        k = key.lower()
        if h == k or h.endswith("." + k):
            out.extend(refs)
    root = root_of(h)
    out += [
        "https://" + root + "/",
        "https://www." + root + "/",
        "https://" + h + "/",
    ]
    seen, uniq = set(), []
    for r in out:
        if r and r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def _ctx():
    ctx = ssl.create_default_context()
    # 这类站点的证书链经常不完整，只为探测，不校验证书
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe(url, referer=None, timeout=12):
    """返回 (是否为 m3u8, 状态)"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            head = r.read(2048)
            return (b"#EXTM3U" in head), r.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception as e:
        return False, str(e)[:60]


def find(url, extra_map=None, log=print):
    """找到可用 Referer。返回字符串（可能为空串表示不需要）。失败返回 None。"""
    ok, code = probe(url)
    if ok:
        log("  来源校验：不需要额外 Referer")
        return ""
    log("  直接请求返回 %s，开始尝试来源页…" % code)
    for ref in candidates(url, extra_map):
        ok, c = probe(url, ref)
        log("    %-38s -> %s" % (ref, "通过" if ok else c))
        if ok:
            return ref
    return None
