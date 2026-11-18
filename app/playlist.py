# -*- coding: utf-8 -*-
"""播放列表解析：m3u8（HLS）与 mpd（DASH）。

只做“看清结构”这件事，不负责下载：
  · m3u8 区分 master / 媒体列表，取档位与分片数
  · 识别是否带 EXT-X-KEY（AES-128 / SAMPLE-AES）与 EXT-X-MAP（fMP4）
  · mpd 只判定类型，具体交给 yt-dlp
"""
import re
import urllib.parse

RE_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line):
    out = {}
    for k, v in RE_ATTR.findall(line):
        out[k] = v.strip('"')
    return out


def is_m3u8(text):
    return text.lstrip().startswith("#EXTM3U")


def parse_m3u8(text, base_url):
    """返回结构化的播放列表描述。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = {
        "master": False,
        "variants": [],
        "segments": [],
        "key": "none",
        "fmp4": False,
    }
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        out["master"] = True
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            a = _attrs(line)
            uri = lines[i + 1] if i + 1 < len(lines) else ""
            if not uri or uri.startswith("#"):
                continue
            res = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
            out["variants"].append({
                "url": urllib.parse.urljoin(base_url, uri),
                "width": int(res.group(1)) if res else 0,
                "height": int(res.group(2)) if res else 0,
                "bandwidth": int(a.get("BANDWIDTH", 0) or 0),
                "codecs": a.get("CODECS", ""),
                "name": a.get("NAME", ""),
            })
        out["variants"].sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
        return out

    for line in lines:
        if line.startswith("#EXT-X-KEY"):
            a = _attrs(line)
            method = (a.get("METHOD") or "NONE").upper()
            if method in ("AES-128",):
                out["key"] = "aes128"
            elif method.startswith("SAMPLE-AES"):
                out["key"] = "sample-aes"
            out["key_uri"] = urllib.parse.urljoin(base_url, a.get("URI", "")) if a.get("URI") else ""
        elif line.startswith("#EXT-X-MAP"):
            out["fmp4"] = True
            a = _attrs(line)
            if a.get("URI"):
                out["map_uri"] = urllib.parse.urljoin(base_url, a["URI"])
        elif not line.startswith("#"):
            out["segments"].append(urllib.parse.urljoin(base_url, line))
    return out


def segment_count(parsed):
    if parsed.get("master"):
        return -1
    return len(parsed.get("segments") or [])


def media_detail(text, base_url):
    """媒体列表的逐条细节：分片时长、密钥、初始段。

    浏览器取流时要在本地重建一份 m3u8 交给 ffmpeg，
    所以这里把 #EXTINF 的时长和 #EXT-X-KEY 的原始属性一起带出来。
    """
    out = {
        "entries": [],          # [{uri, dur}]
        "key_method": "",
        "key_uri": "",
        "key_iv": "",
        "media_sequence": 0,
        "target_duration": 0,
        "map_uri": "",
        "byterange": False,
    }
    dur = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            try:
                out["media_sequence"] = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION"):
            try:
                out["target_duration"] = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY"):
            a = _attrs(line)
            out["key_method"] = (a.get("METHOD") or "").upper()
            out["key_uri"] = urllib.parse.urljoin(base_url, a["URI"]) if a.get("URI") else ""
            out["key_iv"] = a.get("IV", "")
        elif line.startswith("#EXT-X-MAP"):
            a = _attrs(line)
            if a.get("URI"):
                out["map_uri"] = urllib.parse.urljoin(base_url, a["URI"])
        elif line.startswith("#EXT-X-BYTERANGE"):
            out["byterange"] = True
        elif line.startswith("#EXTINF"):
            try:
                dur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 0.0
        elif not line.startswith("#"):
            out["entries"].append({"uri": urllib.parse.urljoin(base_url, line),
                                   "dur": dur})
            dur = 0.0
    return out


def looks_like_mpd(text):
    head = text.lstrip()[:400].lower()
    return head.startswith("<?xml") and "<mpd" in head


def kind_of(text):
    if is_m3u8(text):
        return "hls"
    if looks_like_mpd(text):
        return "dash"
    return "unknown"
