# -*- coding: utf-8 -*-
"""生成本地测试流（不入库，跑一次即可）。

    python tests/make_mocks.py

产出 mocks/m1_plain（明文）与 mocks/m2_aes（AES-128），
用 `python -m http.server 8899 --directory mocks` 起静态服务即可测试。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCKS = os.path.join(ROOT, "mocks")
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"

COMMON = [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc=size=480x360:rate=20",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
    "-t", "12",
    "-c:v", "libx264", "-preset", "veryfast", "-b:v", "900k",
    "-g", "40", "-keyint_min", "40", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "96k",
    "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
]


def run(args):
    p = subprocess.run([FFMPEG] + args)
    if p.returncode != 0:
        sys.exit("ffmpeg 失败，检查是否在 PATH 里，或用 FFMPEG=<路径> 指定")


def main():
    plain = os.path.join(MOCKS, "m1_plain")
    aes = os.path.join(MOCKS, "m2_aes")
    for d in (plain, aes):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))

    run(COMMON + ["-hls_segment_filename", os.path.join(plain, "seg%d.ts"),
                  os.path.join(plain, "index.m3u8")])

    key = os.urandom(16)
    key_file = os.path.join(aes, "enc.key")
    with open(key_file, "wb") as f:
        f.write(key)
    info = os.path.join(aes, "key.info")
    with open(info, "w", encoding="utf-8") as f:
        f.write("enc.key\n%s\n" % key_file)
    run(COMMON + ["-hls_key_info_file", info,
                  "-hls_segment_filename", os.path.join(aes, "seg%d.ts"),
                  os.path.join(aes, "index.m3u8")])
    print("已生成：\n  %s\n  %s" % (plain, aes))


if __name__ == "__main__":
    main()
