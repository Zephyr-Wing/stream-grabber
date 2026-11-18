# -*- coding: utf-8 -*-
"""yt-dlp 封装：只负责“把一条已确认的地址下下来”，不做判断。

进度通过 stdout 行解析回传；暂停 = 终止进程并保留 .part，
继续 = 用同样的命令再跑一次，yt-dlp 自己从断点续。
"""
import os
import re
import subprocess

from .. import config

RE_PCT = re.compile(r"(\d+\.\d+)%")
RE_SIZE = re.compile(r"of\s+~?\s*([\d.]+)(KiB|MiB|GiB)")
RE_RATE = re.compile(r"at\s+([\d.]+)(KiB|MiB|GiB)/s")
RE_ETA = re.compile(r"ETA\s+(\d+):(\d+)")


def _decode(b):
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def cookie_jar(cookies, path):
    """把扩展给的 cookie 数组写成 Netscape 格式，供 yt-dlp --cookies 使用。"""
    lines = ["# Netscape HTTP Cookie File", "# stream-grabber", ""]
    n = 0
    for c in cookies or []:
        name = (c.get("name") or "").strip()
        domain = (c.get("domain") or "").strip()
        if not name or not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expirationDate") or c.get("expires") or 0
        try:
            exp = int(float(exp))
        except Exception:
            exp = 0
        dom = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append("\t".join([dom, flag, c.get("path") or "/", secure,
                                str(exp), name, c.get("value") or ""]))
        n += 1
    if not n:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def build_cmd(cfg, yt, ff, job):
    url = job["url"]
    out_tmpl = os.path.join(job["out_dir"], job["name"] + ".%(ext)s")
    extra = []
    if ff:
        extra += ["--ffmpeg-location", ff]
    if job.get("proxy"):
        extra += ["--proxy", job["proxy"]]
    if job.get("jar"):
        extra += ["--cookies", job["jar"]]

    common = ["--newline", "--no-warnings", "--no-playlist",
              "--retries", str(cfg["retries"]),
              "--socket-timeout", str(cfg["socket_timeout"]),
              "-o", out_tmpl] + extra

    variant = job.get("variant") or {}
    if variant.get("url"):
        # 指定档位：直接下这一档，不用再让 yt-dlp 挑
        return [yt, variant["url"], "--referer", job.get("referer") or "",
                "--user-agent", job.get("ua") or "",
                "-N", str(job.get("threads") or cfg["threads"]),
                "--fragment-retries", str(cfg["fragment_retries"])] + common

    if job.get("page_mode"):
        return [yt, url, "-f", cfg["format_pref"],
                "--merge-output-format", "mp4",
                "--retries", str(cfg["retries"]),
                "--socket-timeout", str(cfg["socket_timeout"]),
                "--newline", "--no-warnings", "--no-playlist",
                "-o", out_tmpl] + extra

    return [yt, url,
            "--referer", job.get("referer") or "",
            "--user-agent", job.get("ua") or "",
            "-N", str(job.get("threads") or cfg["threads"]),
            "--fragment-retries", str(cfg["fragment_retries"])] + common


def run(job, cfg, yt, ff, on_line):
    """执行下载，逐行回调 on_line(text)。返回进程对象。"""
    cmd = build_cmd(cfg, yt, ff, job)
    job["cmd"] = " ".join(cmd[:8]) + " …"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, creationflags=(
                                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    return proc


def parse_line(line, job):
    """从一行输出里更新进度。"""
    p = job["progress"]
    m = RE_PCT.search(line)
    if m:
        p["percent"] = float(m.group(1))
    m = RE_SIZE.search(line)
    if m:
        p["total_text"] = m.group(1) + " " + m.group(2)
    m = RE_RATE.search(line)
    if m:
        mul = {"KiB": 1024, "MiB": 1048576, "GiB": 1073741824}[m.group(2)]
        p["rate_bps"] = float(m.group(1)) * mul
    m = RE_ETA.search(line)
    if m:
        p["eta_sec"] = int(m.group(1)) * 60 + int(m.group(2))
