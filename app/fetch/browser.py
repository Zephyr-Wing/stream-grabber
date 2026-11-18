# -*- coding: utf-8 -*-
"""第三级取流：分片由浏览器取回，服务端只落盘和合并。

前两级都是服务端自己去请求分片（直连 / 代理）。当 CDN 把出口 IP、会话 cookie
或 TLS 指纹签进地址时，服务端怎么试都会被拒；此时唯一必然一致的身份就是
「正在播放这个视频的那个浏览器」。于是反过来做：地址、cookie、Referer 都由
浏览器负责，服务端退化成收件箱。

约定：
  · 分片按序号 PUT 上来，落成 N.ext
  · 密钥单独 PUT（AES-128 的 key 文件）
  · 全部到齐后由 ffmpeg 读本地 m3u8 解密并合并
"""
import json
import os
import subprocess
import urllib.parse

from .. import config, jobs, profile as profile_mod
from . import local

# 分片扩展名按原地址推断，ffmpeg 靠它决定怎么解析
EXT_BY_SUFFIX = {".m4s": ".m4s", ".mp4": ".mp4", ".m4a": ".m4a",
                 ".aac": ".aac", ".ts": ".ts", ".m4v": ".m4v"}


def _dir(job):
    return os.path.join(config.ROOT, ".tmp", "fetch_%s" % job["id"])


def _ext_of(url):
    path = urllib.parse.urlparse(url).path.lower()
    for suf, ext in EXT_BY_SUFFIX.items():
        if path.endswith(suf):
            return ext
    return ".ts"


def prepare(cfg, payload):
    """解析播放列表，把分片清单交给浏览器。"""
    url = (payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "地址格式不对"}

    referer = (payload.get("referer") or "").strip()
    ua = (payload.get("ua") or "").strip()
    cookies = payload.get("cookies") or []
    proxy = config.resolve_proxy(cfg)

    prof = profile_mod.build(url, referer, ua, cookies, cfg,
                             proxies=[None, proxy] if proxy else [None])
    if prof.get("error"):
        return {"ok": False, "error": prof["error"], "profile": prof}
    if prof.get("kind") != "hls":
        return {"ok": False, "error": "浏览器取流只处理 HLS，当前类型是 %s" % prof.get("kind"),
                "profile": prof}
    if prof.get("key") == "sample-aes":
        return {"ok": False, "error": "内容是 SAMPLE-AES（DRM），不做处理", "profile": prof}
    if prof.get("byterange"):
        return {"ok": False, "error": "该列表使用 BYTERANGE 分片，浏览器取流暂不支持",
                "profile": prof}

    segs = prof.get("segments") or []
    if not segs:
        return {"ok": False, "error": "列表里没有分片", "profile": prof}

    job = jobs.register(cfg, dict(payload, url=url,
                                  name=(payload.get("name") or "").strip() or
                                       ("video_" + __import__("time").strftime("%Y%m%d_%H%M"))))
    job["state"] = "fetching"
    job["stage"] = "browser"
    job["route"] = "browser"
    job["route_note"] = "分片由浏览器取回"
    job["profile"] = prof
    job["total"] = len(segs)
    job["received"] = 0
    job["progress"]["segments_total"] = len(segs)

    d = _dir(job)
    os.makedirs(d, exist_ok=True)
    meta = {
        "url": url,
        "media_url": prof.get("media_url") or url,
        "count": len(segs),
        "durations": prof.get("durations") or [],
        "ext": [_ext_of(u) for u in segs],
        "key_method": prof.get("key_method") or "",
        "key_iv": prof.get("key_iv") or "",
        "media_sequence": prof.get("media_sequence") or 0,
        "target_duration": prof.get("target_duration") or 0,
        "map_uri": prof.get("map_uri") or "",
        "ffmpeg_target": os.path.join(job["out_dir"], job["name"] + ".mp4"),
    }
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    jobs.log(job, "浏览器取流：共 %d 个分片" % len(segs))
    plan = {
        "job": job["id"],
        "api": "http://127.0.0.1:%d" % int(cfg.get("server_port") or 8765),
        "segments": segs,
        "key": prof.get("key_uri") or "",
        "map": prof.get("map_uri") or "",
        "ext": meta["ext"],
        "total": len(segs),
        "referer": prof.get("page_origin") or referer,
    }
    job["_plan"] = plan   # 下划线开头，不进快照
    return {"ok": True, "job": jobs.snapshot(job), "plan": plan}


def _load_meta(job):
    p = os.path.join(_dir(job), "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_blob(jid, index, data):
    """收一个分片。index 为 'key' / 'map' 时存附加文件。"""
    job = jobs.JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这个任务", "stop": True}, 404
    if job.get("_cancel") or job.get("state") == "cancelled":
        return {"ok": False, "error": "任务已取消", "stop": True}, 409
    d = _dir(job)
    os.makedirs(d, exist_ok=True)

    if index in ("key", "map"):
        name = "key.bin" if index == "key" else "map.bin"
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        return {"ok": True}, 200

    try:
        i = int(index)
    except ValueError:
        return {"ok": False, "error": "序号不合法"}, 400
    meta = _load_meta(job) or {}
    exts = meta.get("ext") or []
    ext = exts[i] if i < len(exts) else ".ts"
    path = os.path.join(d, "%d%s" % (i, ext))
    with open(path, "wb") as f:
        f.write(data)
    if not os.path.exists(path + ".seen"):
        open(path + ".seen", "wb").close()
        job["received"] = int(job.get("received") or 0) + 1

    total = max(int(job.get("total") or 1), 1)
    job["progress"]["percent"] = min(99.0, job["received"] * 100.0 / total)
    job["progress"]["segments_done"] = job["received"]
    return {"ok": True, "received": job["received"], "total": total}, 200


def build_playlist(job, meta):
    """在本地重建 m3u8，密钥指向已落盘的 key.bin。"""
    d = _dir(job)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if meta.get("target_duration"):
        lines.append("#EXT-X-TARGETDURATION:%d" % int(meta["target_duration"]))
    lines.append("#EXT-X-MEDIA-SEQUENCE:%d" % int(meta.get("media_sequence") or 0))
    if meta.get("map_uri"):
        lines.append('#EXT-X-MAP:URI="map.bin"')
    key_method = (meta.get("key_method") or "").upper()
    if key_method == "AES-128":
        attrs = ['METHOD=AES-128', 'URI="key.bin"']
        if meta.get("key_iv"):
            attrs.append("IV=%s" % meta["key_iv"])
        lines.append("#EXT-X-KEY:" + ",".join(attrs))

    durs = meta.get("durations") or []
    exts = meta.get("ext") or []
    for i in range(int(meta.get("count") or 0)):
        dur = durs[i] if i < len(durs) else 0
        lines.append("#EXTINF:%.3f," % (dur or 0))
        lines.append("%d%s" % (i, exts[i] if i < len(exts) else ".ts"))
    lines.append("#EXT-X-ENDLIST")

    path = os.path.join(d, "index.m3u8")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _ffmpeg_exe(ff):
    """config.find_ffmpeg 给的是目录（供 yt-dlp 用 --ffmpeg-location），
    这里要自己拉进程，得拿到可执行文件本身。"""
    if not ff:
        return None
    if os.path.isfile(ff):
        return ff
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cand = os.path.join(ff, name)
    return cand if os.path.isfile(cand) else None


def finish(jid, cfg, ff):
    job = jobs.JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这个任务"}, 404
    meta = _load_meta(job)
    if not meta:
        return {"ok": False, "error": "任务缺少元数据"}, 400

    missing = []
    d = _dir(job)
    exts = meta.get("ext") or []
    for i in range(int(meta.get("count") or 0)):
        ext = exts[i] if i < len(exts) else ".ts"
        if not os.path.exists(os.path.join(d, "%d%s" % (i, ext))):
            missing.append(i)
    if missing:
        return {"ok": False, "error": "还有 %d 个分片没到（首个 %d）"
                % (len(missing), missing[0])}, 400
    if (meta.get("key_method") or "").upper() == "AES-128" and \
            not os.path.exists(os.path.join(d, "key.bin")):
        return {"ok": False, "error": "分片已齐但缺少密钥文件"}, 400

    exe = _ffmpeg_exe(ff)
    if not exe:
        jobs.fail(job, "E_TOOL", "找不到 ffmpeg 可执行文件")
        return {"ok": False, "error": "找不到 ffmpeg"}, 400

    pl = build_playlist(job, meta)
    out = meta["ffmpeg_target"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    job["state"] = "muxing"
    job["stage"] = "muxing"
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-allowed_extensions", "ALL", "-i", pl,
           "-c", "copy", "-bsf:a", "aac_adtstoasc", out]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=3600)
    except Exception as e:
        jobs.fail(job, "E_NET", "合并失败：%s" % e)
        return {"ok": False, "error": str(e)}, 500

    if p.returncode != 0 or not os.path.exists(out):
        detail = local._decode(p.stderr or b"")[-300:]
        jobs.fail(job, "E_NET", "ffmpeg 合并失败：%s" % detail)
        return {"ok": False, "error": detail}, 500

    job["state"] = "done"
    job["stage"] = "done"
    job["output"] = out
    job["size"] = os.path.getsize(out)
    job["progress"]["percent"] = 100.0
    job["finished"] = __import__("time").time()
    jobs.log(job, "合并完成：%s" % os.path.basename(out))
    print("[任务] %s 完成（浏览器取流）%s" % (jid, os.path.basename(out)))
    return {"ok": True, "job": jobs.snapshot(job)}, 200
