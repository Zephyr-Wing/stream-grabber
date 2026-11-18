# -*- coding: utf-8 -*-
"""HTTP 小工具：统一代理、Referer、UA，并保留状态码。

不读 HTTP_PROXY 环境变量，走哪条路由调用方显式指定，
避免“探测走代理、下载走直连”这种不一致。
"""
import ssl
import time
import urllib.error
import urllib.request

UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def opener(proxy=None):
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    handlers.append(urllib.request.HTTPSHandler(context=_ctx()))
    return urllib.request.build_opener(*handlers)


def headers_for(referer=None, ua=None, cookies=None):
    h = {}
    if ua or UA_DEFAULT:
        h["User-Agent"] = ua or UA_DEFAULT
    h["Accept"] = "*/*"
    if referer:
        h["Referer"] = referer
    if cookies:
        if isinstance(cookies, (list, tuple)):
            h["Cookie"] = "; ".join(
                "%s=%s" % (c.get("name"), c.get("value")) for c in cookies
                if isinstance(c, dict) and c.get("name"))
        else:
            h["Cookie"] = str(cookies)
    return h


def get(url, referer=None, ua=None, proxy=None, cookies=None,
        timeout=12, max_bytes=0):
    """返回 (状态码, 文本, 响应头)。失败时状态码为 None 或 HTTP 码。"""
    req = urllib.request.Request(url)
    for k, v in headers_for(referer, ua, cookies).items():
        req.add_header(k, v)
    try:
        with opener(proxy).open(req, timeout=timeout) as r:
            raw = r.read(max_bytes) if max_bytes else r.read()
            return r.status, raw.decode("utf-8", "ignore"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return e.code, body, {}
    except Exception as e:
        return None, str(e)[:120], {}


def sample(url, referer=None, ua=None, proxy=None, cookies=None,
           need=524288, max_seconds=2.5, timeout=6):
    """取一小段并估算稳定速率。

    返回 (可用, 字节数, 状态码, 原因, 速率 B/s)。
    速率为 0 表示样本太小（例如分片比首个 TCP 块还小），此时只能判定“通”，
    不能比较快慢。

    两个细节：
    - 计时从收到第一块数据才开始。连接与 TLS 握手的开销两条路都不同，
      算进去会让代理（本地已建隧道）显得虚高。
    - 用 8 KB 小块读。这类 CDN 常“发一阵停一阵”，一次读大块会阻塞十几秒，
      期间无法判断该不该收手。
    """
    req = urllib.request.Request(url)
    for k, v in headers_for(referer, ua, cookies).items():
        req.add_header(k, v)
    total = 0
    head = b""
    first_at = None
    counted = 0
    try:
        with opener(proxy).open(req, timeout=timeout) as r:
            while total < need:
                b = r.read(8192)
                if not b:
                    break
                if len(head) < 512:
                    head += b[:512 - len(head)]
                total += len(b)
                if first_at is None:
                    first_at = time.time()
                else:
                    counted += len(b)
                    if time.time() - first_at >= max_seconds:
                        break
            code = r.status
    except urllib.error.HTTPError as e:
        return False, 0, e.code, "", 0.0
    except Exception as e:
        # 已经读到内容后超时：按“能通、速率算到此刻”处理，不判失败
        if total > 0:
            elapsed = max(time.time() - (first_at or time.time()), 0.05)
            return True, total, 200, "", counted / elapsed if counted else 0.0
        return False, 0, None, str(e)[:80], 0.0

    if head.lstrip()[:1] == b"<":
        return False, total, code, "返回内容为 HTML，可能是错误页", 0.0
    if total <= 0:
        return False, 0, code, "没有收到数据", 0.0
    rate = counted / max(time.time() - first_at, 0.05) if first_at and counted else 0.0
    return True, total, code, "", rate


def probe_first_chunk(url, referer=None, ua=None, proxy=None, cookies=None,
                      need=32768, timeout=12):
    """取一小段，判断这条路是否真的可用。

    只看状态码不够：有些 CDN 会返回 200 但内容是错误页。
    能读到 need 字节（或读到 EOF）且不是 HTML，才算通。
    """
    ok, got, code, why, _ = sample(url, referer, ua, proxy, cookies,
                                   need=need, max_seconds=timeout, timeout=timeout)
    return ok, got, code, why
