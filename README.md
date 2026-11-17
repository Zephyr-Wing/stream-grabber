# stream-grabber

**把网页里的 HLS 视频抓下来。**

一个命令行小工具：给它一个视频页面地址（或一条 m3u8 直链），它会自动拿到真实的播放列表、绕过防盗链、多线程下载，最终产出一个能直接播放的 mp4。

> A small CLI that extracts and downloads HLS videos from pages protected by
> anti-leech checks and browser challenges. It uses a real browser to sniff the
> playlist, then hands the transfer to yt-dlp for concurrent, resumable downloads.

---

## 它解决什么问题

浏览器里的「视频下载」插件，通常有三个绕不过去的硬伤：

| 问题 | 后果 |
| --- | --- |
| 在页面里逐段下载，单连接串行 | 高延迟线路上速度极低 |
| 必须等全部分片到齐才落盘 | 中途一断，进度全废 |
| 拿不到加密流的密钥、不带来源页凭据 | 加密流直接失败，或只能抓到广告 |

这个工具把三件事拆开做：

- **拿地址** —— 启动一个真实浏览器（独立配置，不碰你日常那个），自动点播放，从网络请求里挑出正片；
- **过防盗链** —— 逐个尝试来源页，找出能让播放列表返回 200 的那个；
- **下载** —— 交给 yt-dlp，并发拉分片、自动解密、断点续传，最后由 ffmpeg 封装成 mp4。

---

## 特性

- 两种输入都支持：**页面地址** 或 **m3u8 直链**
- 自动探测防盗链 **Referer**，无需手动填
- 自动嗅探播放地址：按「分片数量」区分正片与贴片广告，正片通常上千分片，广告只有几十
- 默认 **16 线程**并发，可调
- **断点续传**：中断后重跑同一条命令即可继续
- 支持 **AES-128 加密**的 HLS
- 自动识别本地代理（Clash / v2ray / Shadowsocks 常见端口）
- Windows / macOS / Linux

实测参考：同一线路下，浏览器内下载约 0.06 MB/s，本工具约 8 MB/s。

---

## 环境要求

- Python **3.8+**
- **Chrome 或 Edge**（用于嗅探页面地址；只下 m3u8 直链则不需要）
- yt-dlp、ffmpeg（可用 `setup_tools.py` 自动安装）

---

## 安装

```bash
git clone https://github.com/Zephyr-Wing/stream-grabber.git
cd stream-grabber

pip install -r requirements.txt

# Windows：自动下载 yt-dlp + ffmpeg 到 tools/
python setup_tools.py
```

macOS / Linux 用包管理器装更省事：

```bash
# macOS
brew install yt-dlp ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
pip install yt-dlp
```

> GitHub 直连不稳定时，`setup_tools.py` 会自动切换镜像；也可以手动指定：
> `python setup_tools.py --mirror https://gh-proxy.com/`

---

## 使用

```bash
# 1) 给页面地址，自动嗅探
python grab.py https://example.com/videos/abc/

# 2) 给 m3u8 直链
python grab.py https://cdn.example.com/hls/xxxx/index.m3u8

# 3) 指定文件名和目录
python grab.py <地址> -n demo -o ./downloaded

# 4) 只解析地址，不下载（想先看看抓到哪条流）
python grab.py <地址> --list

# 5) 页面需要代理才能打开
python grab.py <地址> --proxy http://127.0.0.1:7897

# 6) 下载也走代理（默认是直连，通常更快）
python grab.py <地址> --via-proxy
```

Windows 上直接双击 `grab.bat`，会提示粘贴链接。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-n, --name` | 保存文件名（不含扩展名） |
| `-o, --out` | 保存目录 |
| `-t, --threads` | 并发分片数，默认 16 |
| `--list` | 只解析，不下载 |
| `--proxy` | 解析页面用的代理 |
| `--no-proxy` | 解析页面时不走代理 |
| `--via-proxy` | 下载也走代理 |
| `-c, --config` | 指定配置文件 |

---

## 工作原理

```
页面地址
   │
   ├─ 启动 Chrome（独立 profile，开启 DevTools 端口）
   │      │
   │      ├─ 监听 Network.requestWillBeSent，收集所有 .m3u8
   │      ├─ 循环执行 JS：让所有 <video> 播放、点掉覆盖层播放按钮
   │      └─ 对每条候选播放列表数 #EXTINF 个数
   │             └─ 分片最多的那条 = 正片
   │
   ├─ 拿到播放列表后，试探 Referer
   │      └─ 裸请求 → 配置映射 → 站点根域 → 带 www → CDN 自身
   │
   └─ yt-dlp：16 线程拉分片 → AES-128 解密 → ffmpeg 封装 mp4
```

几个实现上的取舍：

- **为什么用真实浏览器而不是 requests**：目标页面普遍有 Cloudflare 人机验证，命令行请求只会拿到挑战页；播放地址也是 JS 动态下发的，源码里没有静态链接。
- **为什么不用 N_m3u8DL-RE**：它基于 .NET，TLS 握手指纹会被部分 CDN 识别成机器人，请求头完全正确也返回 403。yt-dlp 走 Python 的 TLS 栈，能过。
- **为什么可执行文件不随仓库分发**：体积太大。用 `setup_tools.py` 现装。
- **为什么下载默认不走代理**：视频 CDN 一般不需要代理，走代理反而被限速。只有「打开页面」这一步需要。

---

## 配置

```bash
cp config.example.json config.json
```

常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tools_dir` | `tools` | yt-dlp / ffmpeg 所在目录 |
| `out_dir` | `downloads` | 下载保存目录 |
| `threads` | `16` | 并发分片数 |
| `proxy` | `auto` | `auto` 自动探测；`null` 不用；也可填端口或完整地址 |
| `download_via_proxy` | `false` | 下载阶段是否也走代理 |
| `referer_map` | `{}` | 防盗链映射：CDN 域名 → 来源页候选列表 |

`referer_map` 示例（当 CDN 域名和站点域名完全不同时，自动猜测会失效，需要显式指定）：

```json
{
  "referer_map": {
    "cdn-a.example.com": ["https://www.example.com/"],
    "cdn-b.example.net": ["https://example.com/", "https://m.example.com/"]
  }
}
```

---

## 常见问题

**下载时报 403**
先看 `--list` 输出的 Referer 是否为空或猜错。CDN 与站点不同域时，需要在 `config.json` 的 `referer_map` 里显式指定来源页。

**页面解析失败，捕获不到播放列表**
1. 站点是否需要代理才能打开？加 `--proxy`；
2. 视频是否需要点击才加载？本工具会自动点 `<video>` 和常见播放按钮，但个别播放器需要手动交互；
3. 换 `--proxy` 无效果时，试着先开代理再运行。

**速度慢**
1. 确认没开 `--via-proxy`；
2. 提高 `-t`（如 32），但过多会被 CDN 限流；
3. 换时段。跨境线路在晚高峰会被严重限速。

**中断了怎么办**
重跑同一条命令，yt-dlp 会基于 `.part` 文件续传，不会从头再来。

**DRM 加密的能下吗**
不能。Widevine / PlayReady 这类 DRM 不在本工具范围内。

---

## 已知限制

- 只处理 **HLS (m3u8)** 以及能直连的 mp4，DASH (mpd) 未做专门适配（yt-dlp 原生支持的站点除外）；
- 嗅探依赖浏览器窗口，运行时会短暂弹出一个窗口（约 20-40 秒）；
- 单码率播放列表无法「升级画质」——源站给什么就只能拿什么；
- 目标站点改版可能导致嗅探失效，属于预期内的维护成本。

---

## 目录结构

```
stream-grabber/
├── grab.py              # 命令行入口
├── sgrab/
│   ├── config.py        # 配置加载、工具与代理探测
│   ├── referer.py       # 防盗链探测
│   └── sniffer.py       # 浏览器嗅探播放地址
├── setup_tools.py       # 一键安装 yt-dlp + ffmpeg
├── config.example.json
├── requirements.txt
├── grab.bat / grab.sh
└── tools/               # 运行时生成，不入库
```

---

## 免责声明

本项目仅用于**个人学习、技术研究与已获授权内容的备份**。

使用者应自行确认其行为符合当地法律法规及目标站点的服务条款。作者不对任何因使用本工具产生的版权纠纷、账号风险或其他后果承担责任。请勿将本工具用于批量抓取、二次分发或任何商业用途。

如果内容创作者提供了官方下载渠道，请优先使用官方渠道。

---

## License

[MIT](LICENSE)
