# stream-grabber

把网页里的视频保存到本地。提供两种使用方式：

- **命令行**：`grab.py` 读取页面地址或 m3u8 直链，用浏览器嗅探播放地址，交给 `yt-dlp` 下载。不需要安装扩展，不需要常驻服务。
- **浏览器扩展**：读取当前页面的播放地址与登录态，推送给本地服务下载。适合需要登录才能拿到高画质的站点。

两种方式共用同一套配置和下载参数。命令行是默认入口，扩展用于命令行拿不到登录态的场景。

Python 3.8+ · Chrome / Edge 110+ · MIT

![网络面板里多条地址，工具只保留分片数最多的那条](demo/hero.gif)

---

## 特性

- **两条取址路线。** 页面用 HLS（m3u8）时由浏览器嗅探；页面走 DASH（mpd，如 B 站、YouTube）时回落到 `yt-dlp` 的站点解析器。
- **自动区分正片与贴片。** 统计播放列表的分片数，正片通常上千片、贴片只有几十片，取最多的一条。
- **多线程下载与合并。** `yt-dlp` 并发拉分片，`ffmpeg` 合并为 mp4，编码优先 H.264。
- **解析与下载分开选路。** 页面所在域名常被阻断、需要代理，而视频所在的 CDN 往往直连更快。默认解析走代理、下载走直连，直连取不到分片时自动改用代理。
- **断点续传。** 中断后重跑同一命令，`yt-dlp` 读取 `.part` 文件继续。
- **登录态可选。** 命令行使用独立的浏览器配置目录保存登录态；扩展直接读取当前浏览器的 cookie。
- **纯本地。** 服务仅监听 `127.0.0.1` 并拒绝非本机请求，cookie 以临时文件形式使用，任务结束即删除。

---

## 工作原理

```
命令行
  grab.py
    ├─ m3u8 直链  → 探测可用 Referer → 下载
    └─ 页面地址   → 启动/附着带调试端口的浏览器
                     CDP 监听网络请求，自动点播放
                     取分片数最多的 m3u8
                     └─ 未捕获到 m3u8 → 回落到 yt-dlp 站点解析器

扩展
  浏览器 popup
    ├─ 扫描页面已加载的资源，收集 m3u8 / mpd 候选
    ├─ 读取该站点的 cookie
    └─ POST → 127.0.0.1:8765
                 │
          本地服务 server.py
            ├─ /api/probe  并发拉取播放列表，统计分片、拆分清晰度、排序
            ├─ /api/grab   写入临时 cookie 文件，任务进入队列
            └─ yt-dlp 多线程下载 → ffmpeg 合并 → 删除 cookie 文件
```

职责划分的原因：

- **扩展无法独立完成下载。** Chrome 的下载接口一次只处理一个文件，而一条 HLS 流包含上千个分片，合并必须在浏览器之外进行。
- **本地服务无法独立获取登录态。** Python 自行启动的浏览器使用独立配置目录，与日常浏览器不共享登录状态；扩展运行在日常浏览器内，可直接读取现有 cookie。
- **不采用 Native Messaging。** 该方案需要写注册表并写死扩展 ID 与绝对路径，而未上架扩展的 ID 可能变化。改用本机 HTTP 端口，Chrome 与 Edge 通用。

`127.0.0.1` 是回环地址，始终指向运行它的那台机器，不是固定的服务器地址。扩展与服务安装在同一台电脑上即可通信，更换设备无需修改任何代码或配置。

页面与视频通常不在同一台服务器上：页面域名常被阻断，必须走代理才能打开；视频 CDN 多数可以直连，且直连少一跳、速度更快。因此两条网络路径分开决定——解析走代理，下载默认直连，直连拿不到分片时才改用代理。

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10/11（提供 `.bat` 脚本）；macOS / Linux 可用 `grab.sh`，依赖需自行安装 |
| Python | 3.8 及以上 |
| 浏览器 | Chrome 或 Edge，版本 ≥ 110；仅扩展方式需要 |
| 外部工具 | `yt-dlp`、`ffmpeg`，可用 `setup_tools.py` 自动安装 |

Python 依赖只有 `websocket-client`（命令行嗅探使用），本地服务仅依赖标准库。

---

## 快速开始

### 1. 获取代码

```bash
git clone https://github.com/Zephyr-Wing/stream-grabber.git
cd stream-grabber
```

未安装 Git 时，在仓库页面选择 `Code` → `Download ZIP`，解压后进入该目录。后续命令均在此目录执行，配置中的相对路径也以此为基准。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

同一台机器可能装有多份 Python，依赖必须装在**运行脚本的那一份**里。`grab.bat` 会自动选择已装好依赖的解释器；手动执行命令时，用与安装依赖相同的解释器运行 `grab.py`：

```bash
python -m pip install -r requirements.txt    # 用 python 运行 grab.py 时
py -3 -m pip install -r requirements.txt     # 用 py -3 运行 grab.py 时
```

### 3. 安装 yt-dlp 与 ffmpeg

```bash
python setup_tools.py
```

该脚本从 GitHub 下载 `yt-dlp.exe` 与 `ffmpeg` 至 `tools/` 目录，直连失败时自动切换镜像，也可手动指定：

```bash
python setup_tools.py --mirror https://gh-proxy.com/
```

macOS / Linux 不使用该脚本：

```bash
brew install yt-dlp ffmpeg                     # macOS
sudo apt install ffmpeg && pip install yt-dlp  # Debian / Ubuntu
```

### 4. 运行

```bash
python grab.py <页面地址或 m3u8 直链>
```

Windows 也可双击 `grab.bat`，粘贴链接后回车。至此命令行方式已可用，扩展为可选步骤。

### 5.（可选）安装扩展

1. 地址栏打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 打开右上角「开发者模式」
3. 点「加载已解压的扩展程序」，选择仓库中的 `extension` 文件夹

工具栏出现下载图标即安装完成。另需启动本地服务：

```bash
python server.py           # 或双击 server.bat
```

服务窗口需保持运行，重复启动不会产生第二个实例，仅报告已在运行的端口与版本。若需随系统启动，可将 `server.bat` 的快捷方式放入 `Win+R` → `shell:startup`。

使用扩展前需要了解两点：

- **开发者模式必须保持开启。** Chrome 134 起，关闭开发者模式会立即停用以此方式加载的扩展。
- **启动浏览器时会提示「停用开发者模式扩展」。** 这是未上架扩展的固定提示，选择「保留」即可。

本项目通过 GitHub 分发，未上架 Chrome Web Store。原因是扩展需要 `<all_urls>`、`cookies`、`webRequest` 权限，属于商店审核重点；视频下载类扩展存在被投诉下架的风险；且每次更新都需重新审核。若后续上架，需准备隐私政策页面、权限说明、商店素材，并支付一次性注册费。

---

## 使用

### 命令行（默认方式）

```bash
python grab.py https://example.com/videos/abc/          # 页面地址
python grab.py https://cdn.example.com/hls/xxxx.m3u8    # m3u8 直链
python grab.py https://example.com/videos/abc/ --list   # 只解析，不下载
```

双击 `grab.bat` 时，提示符后可直接粘贴链接；参数写在链接之后，同一行即可。

#### 页面地址的处理流程

1. 检查本机是否已存在带调试端口的浏览器，有则直接附着，不新开进程。
2. 否则以 `.chrome-profile` 目录启动浏览器，打开目标页面。
3. 通过 DevTools 协议监听网络请求，每 5 秒尝试点击播放，收集所有 m3u8 地址。
4. 统计各候选的分片数，取最多者；累计超过 300 片时提前结束，不再等待。
5. 未捕获到 m3u8 时，交由 `yt-dlp` 站点解析器处理（B 站、YouTube 等走此路径）。

首次使用 `.chrome-profile` 时不含登录态。需要登录的站点，先双击 `chrome-debug.bat` 打开该配置的浏览器并完成登录，登录状态会保留。该浏览器保持开启时，`grab.py` 会直接附着，无需关闭。

#### 参数

| 参数 | 说明 |
| --- | --- |
| `-n, --name` | 保存文件名，不含扩展名 |
| `-o, --out` | 保存目录，覆盖配置中的 `out_dir` |
| `-c, --config` | 指定配置文件路径 |
| `-t, --threads` | 并发分片数，默认 16 |
| `--list` | 只解析播放地址，不下载 |
| `--proxy URL` | 页面解析使用的代理，如 `http://127.0.0.1:7897` |
| `--no-proxy` | 解析与下载均不使用代理 |
| `--via-proxy` | 下载阶段强制使用代理 |
| `--cookies BROWSER` | 让 `yt-dlp` 读取浏览器 cookie 库，如 `chrome`；要求该浏览器已关闭 |
| `--guest` | 使用一次性浏览器配置，不携带登录态 |
| `--cdp-port PORT` | 连接指定调试端口的浏览器，默认自动探测 |
| `--no-fallback` | 未捕获 m3u8 时直接退出，不回落 `yt-dlp` |

#### 常用组合

```bash
# 只解析地址，确认抓到的是正片而非贴片
python grab.py <地址> --list

# 页面需要代理才能打开
python grab.py <地址> --proxy http://127.0.0.1:7897

# 免登录，用一次性浏览器配置
python grab.py <地址> --guest
```

`--list` 会输出解析结果，包含路线、地址、Referer 与下载将使用的网络路径：

```
名称    : 193039199_mp4_h264_aac_hd_7
路线    : 浏览器嗅探 m3u8
地址    : https://.../193039199_mp4_h264_aac_hd_7.m3u8
Referer : https://hlsjs.video-dev.org/
下载路径: 直连（不通时改代理 http://127.0.0.1:7897）
登录态  : 已导出
```

下载开始前会打印实际选中的路径：

```
  [路径] 直连可用（3.2 MB/s）
代理    ：直连
```

### 扩展方式

适用于需要登录态才能获得高画质的站点，或希望直接在当前浏览页面上一键下载的场景。使用前需启动本地服务。

1. 打开视频页面，让视频播放数秒
2. 点击扩展图标
3. 列表按分片数排序，第一条即正片，点右侧 `↓`

文件保存至配置中的 `out_dir`，默认 `downloads/`。列表每行显示清晰度徽标（`1080p` / `HLS` / `DASH`）、分片数、CDN 域名。分片数是区分正片与贴片最可靠的依据。

页面存在两种情况：

| 页面拉流方式 | 列表表现 | 操作 |
| --- | --- | --- |
| HLS（m3u8） | 列出所有候选，按分片数排序 | 点 `↓` 下载选中项 |
| DASH（mpd / `.m4s`） | 列表为空，顶部按钮高亮 | 点「下载这个页面」，交由 `yt-dlp` 解析 |

判断方式：开发者工具 → 网络面板 → 过滤 `m3u8`。有结果走第一条，只有 `.mpd` 或 `.m4s` 走第二条。

扩展面板下方显示最近 3 个任务及进度，每 1.2 秒刷新一次。

---

## 更新

```bash
git pull
python setup_tools.py      # 可选，同步更新 yt-dlp 与 ffmpeg
```

修改 `extension/` 下的文件后，需在 `chrome://extensions` 点击该扩展卡片上的刷新按钮；修改 `server.py` 或 `sgrab/` 后，重启服务即可。

`config.json` 不在版本控制内，`git pull` 不会覆盖。

---

## 配置

```bash
cp config.example.json config.json
```

配置文件可省略，缺失项使用默认值。命令行与本地服务读取同一份配置，相对路径以仓库根目录为基准。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tools_dir` | `tools` | `yt-dlp` / `ffmpeg` 所在目录 |
| `out_dir` | `downloads` | 下载目录，建议填绝对路径，如 `D:/Videos` |
| `threads` | `16` | 并发分片数。网络较差时降至 8，跨境线路可试 24–32 |
| `retries` | `10` | 整体重试次数 |
| `fragment_retries` | `30` | 单个分片重试次数 |
| `socket_timeout` | `20` | 连接超时（秒） |
| `proxy` | `"auto"` | `auto` 自动探测常见本地端口；`null` 不使用；也可填 `7897` 或完整地址 |
| `download_via_proxy` | `"auto"` | `auto` 直连优先、不通才用代理；`"smart"` 实测择优；`true` 强制代理；`false` 始终直连 |
| `server_port` | `8765` | 服务端口，修改后需同步修改 `extension/popup.js` 中的 `API` 常量 |
| `browser_profile` | `""` | 命令行嗅探使用的浏览器配置目录，留空即 `.chrome-profile` |
| `cdp_port` | `9222` | 命令行优先探测的调试端口 |
| `referer_map` | `{}` | 防盗链映射，CDN 域名 → 来源页候选列表 |

`proxy` 为 `auto` 时依次探测 `7897`、`7890`、`7891`、`7892`、`10808`、`10809`、`1080`、`2080`、`6152`、`8889`，命中即用。

`download_via_proxy` 决定视频分片从哪条路下载。默认 `auto`：先直连，能取到分片就直连（少一跳，通常更快），完全取不到才改用代理。若某个站直连能通但明显比代理慢，把值改成 `"smart"`，程序会各测一小段吞吐再决定；也可以临时用 `--via-proxy` / `--no-proxy` 强制指定。

`referer_map` 用于 CDN 域名与站点域名不同的情况，自动推测无法命中时手工指定：

```json
{
  "referer_map": {
    "cdn-a.example.com": ["https://www.example.com/"],
    "cdn-b.example.net": ["https://example.com/", "https://m.example.com/"]
  }
}
```

---

## HTTP API

服务仅监听 `127.0.0.1`，只接受来自 `127.0.0.1` 与 `::1` 的请求，其他来源返回 403。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/ping` | 服务信息：版本、`out_dir`、`threads`、`yt-dlp` 与 `ffmpeg` 是否就绪 |
| GET | `/api/jobs` | 任务列表，按创建时间倒序，每项含最近 12 行日志 |
| GET | `/api/job?id=<id>` | 单个任务详情，含最近 80 行日志 |
| POST | `/api/probe` | 入参 `{urls: [{url, referer}], ua}`，返回分片数、是否 master、清晰度列表 |
| POST | `/api/grab` | 入参 `{url, referer, title, name, cookies, ua, mode}`，`mode` 为 `hls` 或 `page` |

任务在内存中保留最近 30 条，每条最多 400 行日志。下载串行执行，避免多任务争抢带宽或触发 CDN 限流。

---

## 目录结构

```
stream-grabber/
├── grab.py              命令行入口
├── server.py            本地下载服务
├── sgrab/
│   ├── config.py        配置加载、工具定位、代理与调试端口探测
│   ├── referer.py       防盗链探测
│   ├── route.py         选择下载走直连还是代理
│   └── sniffer.py       CDP 浏览器嗅探、cookie 导出
├── extension/           浏览器扩展（MV3）
│   ├── manifest.json
│   ├── background.js    网络层记录 m3u8 / mpd
│   ├── popup.html       扩展面板
│   ├── popup.js         资源扫描、候选合并、提交与进度轮询
│   └── icons/
├── setup_tools.py       安装 yt-dlp 与 ffmpeg 至 tools/
├── grab.bat / grab.sh   命令行启动脚本
├── server.bat           服务启动脚本
├── chrome-debug.bat     启动带 9222 调试端口的专用浏览器
├── config.example.json
├── demo/                README 动图
├── .chrome-profile/     专用浏览器配置，存登录态（不入库）
└── tools/               运行时生成（不入库）
```

---

## 故障排查

**提示「缺少依赖 websocket-client」**
依赖装在了另一份 Python 里。用运行脚本的同一个解释器重新安装：`python -m pip install -r requirements.txt` 或 `py -3 -m pip install -r requirements.txt`。`grab.bat` 会自动选择装有依赖的解释器；直接运行脚本仍出现该提示，说明所有候选解释器都缺依赖。

**提示「无法解析页面」**
依次检查：依赖是否就绪；浏览器是否可用（`chrome-debug.bat` 能否正常打开）；调试端口是否被其他程序占用（用 `--cdp-port` 指定或关闭占用进程）。该提示表示嗅探的前置条件不满足，与站点是否含视频无关。

**提示「页面上没有 m3u8 流」**
该站大概率使用 DASH。不要加 `--no-fallback`，交由 `yt-dlp` 解析；若 `yt-dlp` 同样不支持该站，则不在本工具覆盖范围内。

**下载速度慢**
先看下载开始前的 `[路径]` 一行，确认走的是直连还是代理。直连被限速（常见于境外 CDN）时，在 `config.json` 把 `download_via_proxy` 设为 `true`，或运行时加 `--via-proxy`；反之代理反而更慢时加 `--no-proxy`。拿不准就用 `"smart"` 让程序实测。

**页面可播放，但下载返回 403**
多半是 Referer 不对。`--list` 会打印探测到的 Referer；CDN 与站点不同域时自动推测通常猜不中，需在 `referer_map` 中登记。命令行会先用直连、再退代理，两条路的 Referer 一致，因此不必为路径差异单独处理。

**扩展显示「本地服务没开」**
`server.bat` 窗口已关闭或未启动。重新运行 `python server.py`；服务状态可用 `http://127.0.0.1:8765/api/ping` 验证，返回 JSON 即为正常。

**扩展列表为空**
视频尚未开始加载，先播放数秒后重新点击扩展。若始终为空，按上文判断页面是否使用 m3u8。

**点击下载后任务未出现**
查看服务窗口输出。最常见原因是未安装 `yt-dlp`，重新运行 `python setup_tools.py`。

**进度停滞或中断**
分片正在重试，连接超时 20 秒、单个分片重试 30 次，超过后才报错。命令行中断后重跑同一命令即可续传；扩展任务需重新提交。

**端口 8765 被占用**
服务会直接报错退出。可修改 `server_port`，但必须同步修改 `extension/popup.js` 第 1 行的 `API` 常量。

---

## 已知限制

- **无法处理 DRM 内容。** 腾讯视频、爱奇艺、优酷、Netflix 等使用 Widevine / PlayReady，分片为密文，密钥不在客户端。
- **不支持直播流。** 仅处理点播。
- **清晰度受账号权限限制。** 工具只能传递登录态，无法突破站点对匿名用户的画质限制。
- **防盗链需手工补充。** CDN 与站点不同域且自动推测失败时，需在 `referer_map` 中登记。
- **签名地址有时效。** 部分站点的分片地址带签名，数分钟后失效。
- **站点改版可能导致失效。** 属于此类工具的固有问题。
- **扩展权限较宽。** 需「读取和更改所有网站上的数据」才能读取任意页面的资源列表与 cookie，相关代码均在 `extension/` 内。

---

## 免责声明

本项目仅用于个人学习、技术研究与备份自己有权保存的内容。使用者需自行确认行为符合当地法律及目标站点的服务条款，不得用于批量抓取、二次分发或商业用途。作者不承担由此产生的任何后果。

内容创作者提供官方下载渠道时，请优先使用官方渠道。

## License

[MIT](LICENSE)
