# stream-grabber

给它一个视频页面地址，或者一条 m3u8 直链，它把视频下下来。

Python 写的命令行工具。找播放地址这件事自己干，下载交给 yt-dlp，最后用 ffmpeg 封成 mp4。默认 16 线程，中断了能续。

> A small CLI that digs the real playlist out of a video page and hands the  
> transfer to yt-dlp.

![左边是浏览器插件看到的，右边是本工具看到的](demo/hero.gif)

有些站的播放器先放贴片，正片摆在另一条 CDN 上。浏览器插件会把看到的地址都列出来，广告和正片混在一起，通常先点到广告。这里换个办法：数分片。正片一千多个，贴片几十个，取最多的那条。上图左边是插件的视角，右边是数完之后的结果。

---

## 能下什么

先看页面用哪种方式拉流，两种情况的处理完全不同。

### m3u8（HLS）

条件是两条：播放器用 m3u8 拉流、没有 DRM。都满足就能下，跟站大站小没关系。自建 CDN 的影视站、各种独立小站、部分成人站（missav、jable），都属于这一类。这类站没有官方 API，也正是浏览器插件最容易只抓到广告的地方。

### 不是 m3u8 的（DASH / mpd）

嗅探器一条 m3u8 都看不到，会转去问 yt-dlp 认不认识这个站。认识就能下，默认选 1080P 的 H.264 加 AAC 合成 mp4。

B 站是这一类。它的播放器请求 `video.m4s` 和 `audio.m4s`，整个网络面板里没有一条 m3u8，所以嗅探器对它无能为力，全靠兜底。普通投稿匿名就能拿到 1080P，实测 BV1edWM63Ewd 是 `30080+30280`，1920×1080 h264 + aac，56.6 MB。

番剧不一样，清晰度跟着账号走。拿 ep1583982 试，匿名只列出 `32 mp4 480p` 一条；登录后才有 720P，1080P 要大会员。这类内容必须带登录态，做法见「高画质要登录态」。

### 下不了的

腾讯视频、爱奇艺、优酷、Netflix 这一批走 Widevine / PlayReady，分片是密文，密钥不在客户端，别指望。抖音网页版的分片带签名，抓到的地址几分钟就废。直播流没做，只处理点播。

判断一个站走哪条路：开发者工具打开网络面板，过滤框输 `m3u8`。有，走第一条；只有 `.mpd`、`.m4s`，走第二条。

---

## 安装

```bash
git clone https://github.com/Zephyr-Wing/stream-grabber.git
cd stream-grabber
pip install -r requirements.txt

# Windows 上一步到位：把 yt-dlp 和 ffmpeg 下到 tools/
python setup_tools.py
```

macOS / Linux 用系统包管理器更省事：

```bash
brew install yt-dlp ffmpeg          # macOS
sudo apt install ffmpeg && pip install yt-dlp   # Debian / Ubuntu
```

GitHub 直连不稳的话，`setup_tools.py` 会自动换镜像，也能手动指定：`python setup_tools.py --mirror https://gh-proxy.com/`

需要 Python 3.8+，以及 Chrome 或 Edge。只下 m3u8 直链的话不需要浏览器。

---

## 使用

### 双击就行

Windows 双击仓库根目录的 `grab.bat`，粘贴链接，回车。文件落到 `downloads/`，窗口最后会打印完整路径和体积。macOS / Linux 用 `./grab.sh`，一样。

第二句它会问你带不带登录态。带的话得先把浏览器关掉，代价有点麻烦，所以默认是不带。**如果下下来画质比网页上低，就是这一步没带登录态。**

![双击 grab.bat 到下载完成](demo/usage.gif)

*示例输出，实际文件名、体积和速度随站点变化。*

### 参数

| 参数 | 作用 |
| --- | --- |
| `-n, --name` | 保存文件名，不带扩展名 |
| `-o, --out` | 保存目录 |
| `-t, --threads` | 并发分片数，默认 16 |
| `--list` | 只解析地址，不下载 |
| `--login` | 用你日常的浏览器配置嗅探，画质按账号来（要求浏览器已关闭） |
| `--cdp-port` | 连一个已开着调试端口的浏览器，不用关它，例如 `9222` |
| `--cookies` | 让 yt-dlp 去读浏览器的 cookie 库，如 `chrome`（要求该浏览器已关闭） |
| `--proxy` | 打开页面用的代理 |
| `--no-proxy` | 打开页面时不走代理 |
| `--via-proxy` | 下载也走代理 |
| `--no-fallback` | 嗅不到 m3u8 就停，不要转 yt-dlp |
| `-c, --config` | 指定配置文件 |

### 高画质要登录态

多数站的清晰度是跟着账号走的：访客 480p，登录 720p，会员 1080p。嗅探用的是浏览器，所以画质直接取决于那个浏览器登没登录。

默认启的是临时配置，干净、不碰你的东西，但等于访客。想按自己的账号拿画质，两条路：

**A. `--login`**，用完就散。需要先完全关掉浏览器，因为同一个配置目录不能开两个实例。

```bash
python grab.py <地址> --login
```

**B. `--cdp-port`**，关一次就够，之后一直能用。用 `chrome-debug.bat` 起一个带调试端口的 Chrome（还是你的登录态），之后每次下载都连它：

```bash
chrome-debug.bat            # 只需一次，Chrome 保持开着
python grab.py <地址> --cdp-port 9222
```

这条路顺手解决另一件事：嗅探完会把当前会话的 cookie 导出来，交给 yt-dlp 去下载。所以拿到的画质和你在网页上看到的一致，不用再让 yt-dlp 自己去读浏览器那个被锁住的库。cookie 文件用完即删。

### 命令行

```bash
# 页面地址，自动嗅探
python grab.py https://example.com/videos/abc/

# m3u8 直链，跳过嗅探
python grab.py https://cdn.example.com/hls/xxxx/index.m3u8

# 指定文件名和目录
python grab.py <地址> -n demo -o ./downloaded

# 只解析，先看清楚抓到的是正片还是广告
python grab.py <地址> --list

# 页面要代理才能打开
python grab.py <地址> --proxy http://127.0.0.1:7897

# 下载也走代理（默认直连，一般更快）
python grab.py <地址> --via-proxy
```

---

## 它怎么工作

```
页面地址
   │
   ├─ 起 Chrome，三种起法
   │     ├─ 临时配置（默认）：干净，但没有登录态
   │     ├─ --login：你的日常配置，要独占，用前得关掉浏览器
   │     └─ --cdp-port：连你已经开着的那个，不动它，只借用一只标签页
   │     │
   │     ├─ 监听 Network.requestWillBeSent，把 .m3u8 全收下来
   │     ├─ 循环跑 JS：让 <video> 播放，点掉盖在上面的播放按钮
   │     ├─ 每条候选播放列表数一遍 #EXTINF，最多的那条是正片
   │     └─ 顺手把会话里的 cookie 导出来，给下载端用
   │
   ├─ 有 m3u8？
   │    ├─ 有 → 试 Referer（裸请求 → 配置映射 → 根域 → 带 www → CDN 自身）
   │    │        └─ yt-dlp：16 线程拉分片 → AES-128 解密 → ffmpeg 封装
   │    │
   │    └─ 没有 → 问 yt-dlp 认不认识这个站
   │             └─ 认识 → 整页交给它，选 H.264 + AAC，合并成 mp4
```

几个当时的选择：

**为什么真开一个浏览器。** 这类站普遍挂 Cloudflare 人机验证，命令行直接请求只能拿到挑战页。播放地址也是 JS 现算出来的，源码里没有静态链接。真浏览器跑一遍，验证和签名都不用自己处理。

**为什么默认用临时配置。** 用你日常的配置要独占那个目录，得先关浏览器，对「下个视频」来说太重。所以默认干净启动，需要画质时再显式要求登录态。

**为什么要自己导 cookie。** yt-dlp 的 `--cookies-from-browser` 要读浏览器的 cookie 数据库，那个文件在浏览器运行时是独占的，读不到就报错。既然浏览器已经开着并且登录了，从 DevTools 里直接读明文更省事，也不用关浏览器。

**为什么没用 N_m3u8DL-RE。** 试过，它基于 .NET，TLS 握手指纹会被某些 CDN 当成机器人，请求头一字不差地照抄也回 403；同样的头用 curl 发就正常。yt-dlp 走 Python 的 TLS 栈，过得了。

**为什么下载默认不走代理。** 视频 CDN 一般不要求代理，绕一圈反而慢。只有打开页面那一步需要。

**为什么仓库里没有 exe。** 太大。用 `setup_tools.py` 现装，装到 `tools/`，这个目录不进 git。

---

## 配置

配置文件的作用是把常用的参数存下来，双击 `grab.bat` 和命令行都会读。不建也能用，所有项都有默认值。

```bash
cp config.example.json config.json
```

改得最多的两项：

| 想改什么    | 键         | 说明                                     |
| ------- | --------- | -------------------------------------- |
| 视频存哪    | `out_dir` | 默认 `downloads`。建议写成绝对路径，比如 `D:/Videos` |
| 一次拉几个分片 | `threads` | 默认 16。网络差降到 8，跨境线路可以试到 24-32           |

其余项：

| 键                    | 默认      | 说明                                      |
| -------------------- | ------- | --------------------------------------- |
| `tools_dir`          | `tools` | yt-dlp / ffmpeg 所在目录                    |
| `retries`            | `10`    | 整体重试次数                                  |
| `fragment_retries`   | `30`    | 单个分片重试次数                                |
| `socket_timeout`     | `20`    | 连接超时，秒                                  |
| `proxy`              | `auto`  | `auto` 自动探测；`null` 不用；也可以填 `7897` 或完整地址 |
| `download_via_proxy` | `false` | 下载阶段是否也走代理                              |
| `referer_map`        | `{}`    | 防盗链映射，见下                                |

相对路径以仓库根目录为准，不是相对 `config.json`。优先级是命令行 > `config.json` > 内置默认值。

`referer_map` 用在 CDN 域名和站点域名完全不同的地方，自动猜会猜不中，得写死：

```json
{
  "referer_map": {
    "cdn-a.example.com": ["https://www.example.com/"],
    "cdn-b.example.net": ["https://example.com/", "https://m.example.com/"]
  }
}
```

---

## 实际遇到的问题

**403**

先跑 `--list`，看 Referer 是不是空的或者猜错了。CDN 和站点不同域时，去 `referer_map` 里补一条。

**下下来的画质比网页上低**

基本可以断定是没带登录态。嗅探起的是临时配置，等于访客，站给访客什么就给什么。加 `--login`（先关浏览器），或者用 `chrome-debug.bat` 加 `--cdp-port 9222` 常驻登录态。

**卡在 `--cookies chrome` 不生效**

这个参数要读浏览器的 cookie 库，而那个库在浏览器运行时是独占的，yt-dlp 会报 `Could not copy Chrome cookie database` 然后继续用匿名身份跑。要么先关浏览器，要么干脆用 `--login`，它会绕开这个库，直接从页面会话里读。

**解析不到地址**

看它有没有转 yt-dlp。转了并且打印「认出来了：《标题》」，说明第一条路失败但兜底成功，直接下就行。

两条都失败，通常是要登录：`--login`。

再就是代理。有些站不开代理根本打不开页面，加 `--proxy`。

**慢**

先确认没开 `--via-proxy`。然后把 `-t` 调大试试，32 差不多是上限，再高容易被 CDN 限流。跨境线路晚高峰会掉速，换个时间可能好很多。

**中断了**

重跑同一条命令。yt-dlp 会读 `.part` 文件接着拉，不从头来。

---

## 已知限制

嗅探这一段只管 m3u8。页面是 DASH 的话它找不到东西，会转手给 yt-dlp，所以那类站能下多少，取决于 yt-dlp 支持多少，不取决于这里。

每次运行都会弹一个浏览器窗口，二十到四十秒。走兜底路线这段时间也省不掉，因为得先确认页面上真的没有 m3u8。

源站给什么画质就拿什么。m3u8 里只写了一条码率的话，没有提升空间。

清晰度最终由账号决定，工具只能把登录态带过去。番剧、会员内容这类，没大会员就是拿不到 1080P，这不是抓取姿势的问题。

`--login` 要独占浏览器的配置目录，所以用之前必须把它关掉。不想反复关的话用 `--cdp-port`。

站改版就可能失效。这类工具都有这个问题，不是 bug。

---

## 和别的工具比

| 工具          | 能干什么                   | 干什么不行                                            |
| ----------- | ---------------------- | ------------------------------------------------ |
| yt-dlp      | 600 多个站的解析器，格式、字幕、直播都管 | 没写过解析器的站只能走通用模式，通用模式拿不到 JS 下发的地址，也过不了 Cloudflare |
| N_m3u8DL-RE | 下 HLS/DASH 质量最好，参数最全   | 得先自己搞到 m3u8 地址；.NET 的指纹会被部分 CDN 拦成 403           |
| 浏览器插件（猫抓等）  | 装完就能点，覆盖面还广，网络层什么都看得见  | 分片串行下载，慢；拿不到密钥和 Referer；广告和正片混在一起，得自己挑           |
| 本工具         | 一条命令走完全程，自动挑正片，多线程加续传  | 只有 m3u8 这一段是自己找的，DASH 页面转手给 yt-dlp；要装 Python     |

它不打算取代谁。yt-dlp 报 `Unsupported URL` 或者 403 的时候，换这个试试；插件下不动或者挑不准的时候，也换这个。你要是不想记哪个站该用哪个工具，那就都丢给它。

---

## 目录

```
stream-grabber/
├── grab.py              命令行入口
├── sgrab/
│   ├── config.py        配置、工具定位、代理探测
│   ├── referer.py       防盗链探测
│   └── sniffer.py       浏览器嗅探播放地址 + 导出登录态
├── setup_tools.py       装 yt-dlp + ffmpeg
├── grab.bat / grab.sh
├── chrome-debug.bat     起一个带 9222 调试端口的 Chrome，供 --cdp-port 用
├── config.example.json
├── demo/                README 里的动图
│   ├── hero.gif
│   ├── usage.gif
│   └── record.bat       想自己录一段真实演示时用，需要 ffmpeg
└── tools/               运行时生成，不进仓库
```

---

## 声明

个人学习、技术研究、备份自己有权保存的内容，用这个。使用者自己确认行为合法、不违反目标站点的条款。作者不承担由此产生的任何后果，也别拿它做批量抓取、二次分发或商业用途。

内容创作者有官方下载渠道的话，走官方。

License: [MIT](LICENSE)
