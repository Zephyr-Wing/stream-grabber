# -*- coding: utf-8 -*-
"""单文件直链的下载器。

yt-dlp 拿到一个 .mp4 会先当网页抓一遍（"Downloading webpage"），
对几百 MB 的文件等于从头再拉一次，进度也很久不出现。
直链自己下：Range 续传、分块读、实时速率，够用且可控。
"""
import os
import re
import time
import urllib.error
import urllib.request

from .. import net

CHUNK = 262144


def _total_from(headers, start):
    cr = headers.get("Content-Range") or headers.get("content-range") or ""
    m = re.search(r"/(\d+)", cr)
    if m:
        return int(m.group(1))
    cl = headers.get("Content-Length") or headers.get("content-length")
    if cl and str(cl).isdigit():
        return start + int(cl)
    return 0


def download(job, on_progress, should_stop):
    """下载到 out_dir/name.mp4。返回 (状态, 说明)，状态为 done/paused/cancelled/failed。"""
    url = job.get("segment_base") or job["url"]
    path = os.path.join(job["out_dir"], job["name"] + ".mp4")
    start = os.path.getsize(path) if os.path.exists(path) else 0

    headers = net.headers_for(job.get("referer"), job.get("ua"), job.get("cookies"))
    if start:
        headers["Range"] = "bytes=%d-" % start

    req = urllib.request.Request(url)
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        resp = net.opener(job.get("proxy")).open(req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 416 and start:
            return "done", path
        return "failed", "HTTP %s" % e.code
    except Exception as e:
        return "failed", str(e)[:120]

    total = _total_from(resp.headers, start)
    done = start
    t0 = time.time()
    base = done
    mode = "ab" if start else "wb"
    try:
        with open(path, mode) as f:
            while True:
                if should_stop():
                    return ("paused" if job.get("_pause") else "cancelled"), "已停止"
                b = resp.read(CHUNK)
                if not b:
                    break
                f.write(b)
                done += len(b)
                dt = time.time() - t0
                if dt > 0.2:
                    rate = (done - base) / dt
                    pct = (done * 100.0 / total) if total else 0.0
                    eta = int((total - done) / rate) if (total and rate > 0) else 0
                    on_progress(pct, rate, eta, done, total)
                    t0 = time.time()
                    base = done
    except Exception as e:
        return "failed", str(e)[:120]
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if total and done < total:
        return "failed", "传输提前结束（%d/%d 字节）" % (done, total)
    on_progress(100.0, 0.0, 0, done, total or done)
    return "done", path
