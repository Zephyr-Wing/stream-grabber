# stream-grabber

把网页里的视频保存到本地。

浏览器扩展负责找到播放地址并复用你的登录态，本地服务负责解析、下载与合并。服务端只依赖 Python 标准库，下载交给 yt-dlp，合并交给 ffmpeg。

## 特性

- **自动发现播放地址**：挂钩页面的 fetch / XHR，兼容 iframe 与「点击后才播放」的播放器
- **三级取流**：直连 → 代理 → 浏览器代取，覆盖防盗链、签名校验与出口 IP 绑定
- **面向机制而非站点**：只判断是否加密、是否带签名、是否跨域，不为单个站点写特判
- **多线程分片下载**：断点续传，支持 AES-128 加密的 HLS
- **扩展内的下载面板**：文件名、目录、档位、并发、暂停/继续/取消
- **只监听 127.0.0.1**：不对外提供服务，接口仅接受本机请求

## 工作原理

```
扩展（运行在日常浏览器里）
  ├ 挂钩页面的 fetch / XHR，记录播放列表与接口下发的地址
  ├ 读取页面已加载的资源，遍历所有 iframe
  └ 把地址、Referer、cookie 交给本地服务
                 │ 127.0.0.1:8765
本地服务（仅本机）
  ├ 解析播放列表：类型、分片数、档位、加密方式、是否带签名
  ├ 选路：直连能取到分片就用直连，取不到才用代理
  ├ 下载：HLS/DASH 交给 yt-dlp，单个媒体文件用内置下载器
  └ 合并输出 mp4，回传进度与错误码
```

分片由谁请求，决定了能否下载成功：

| 级别 | 请求方 | 触发条件 | 速度 |
| --- | --- | --- | --- |
| 一 | 服务端直连 | 默认 | 最快 |
| 二 | 服务端经代理 | 直连取不到分片 | 取决于节点 |
| 三 | 扩展在页面内请求 | 前两级均被拒绝（403、签名不符） | 较慢，但身份与播放器一致 |

第三级用于 CDN 把出口 IP、会话 cookie 或浏览器指纹签入地址的场景。此时服务端无论走哪条路都会被拒，而播放器仍在正常取流，因此改由页面内的 content script 逐片取回，交由服务端落盘合并。

选路不使用单连接测速：这类 CDN 通常按连接限速，单连接速率不能代表并发吞吐。是否够快由下载过程中的实际速率决定，长期低于阈值会自动切换代理并续传。

## 环境要求

- Python 3.8+
- Chrome 或 Edge 111+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 与 [ffmpeg](https://ffmpeg.org/)，放在 `tools/` 或系统 PATH

## 安装

**1. 准备 yt-dlp 与 ffmpeg**

放到项目下的 `tools/`，或安装到 PATH。以 Windows 为例：

```
tools/
├── yt-dlp/yt-dlp.exe
└── ffmpeg/ffmpeg.exe
```

**2. 启动本地服务**

双击 `server.bat`，输出监听地址即启动成功。关闭服务直接关掉该窗口，或在该窗口内按 `Ctrl+C`。

需要开机自启时，把 `server.bat` 的快捷方式放入 `Win+R` → `shell:startup`。

**3. 加载扩展**

1. 打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 开启右上角「开发者模式」
3. 点击「加载已解压的扩展程序」，选择项目中的 `ext` 目录

工具栏出现图标即安装完成，未显示时可在拼图菜单中固定。未上架的扩展每次启动浏览器会提示「停用开发者模式扩展」，选择保留即可。

## 使用

1. 打开视频页面，播放几秒，让播放器加载出流
2. 点击扩展图标，列表显示捕获到的播放地址与分片数
3. 点击某一条右侧的箭头，进入下载面板
4. 修改文件名、目录、档位、并发，选择取流方式后开始下载
5. 在面板下方查看进度，可暂停、继续或取消

下载面板同时支持直接粘贴 m3u8 / mp4 地址，以及切换下载路径（自动 / 仅直连 / 仅代理）。任务失败时会显示错误码，并提供「改用浏览器取流」入口。

**更新**

```bash
git pull
```

改动的是 `ext/` 内的文件，在 `chrome://extensions` 点击刷新按钮，再刷新视频页面；改动的是 `app/` 内的文件，重启 `server.bat`。本地的 `config.json` 不会被覆盖。

## 配置

`config.json` 可省略，缺省值见 `config.example.json`。相对路径以项目根目录为准。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `out_dir` | `downloads` | 保存目录 |
| `tools_dir` | `tools` | yt-dlp 与 ffmpeg 所在目录 |
| `threads` | `16` | 分片并发数 |
| `retries` | `10` | 整体重试次数 |
| `fragment_retries` | `30` | 单个分片重试次数 |
| `socket_timeout` | `20` | 单次读写超时（秒） |
| `proxy` | `auto` | `auto` 自动探测本地常见端口；也可填 `7897` 或 `http://host:port` |
| `route_mode` | `auto` | `auto` 直连优先、取不到才用代理；`direct` / `proxy` 强制 |
| `server_port` | `8765` | 服务端口 |
| `format_pref` | `bv*[vcodec^=avc1]+ba/bv*+ba/b` | DASH 页面的格式选择表达式 |

修改 `server_port` 后，需同步修改 `ext/popup.js` 与 `ext/dashboard.js` 顶部的 `API` 常量。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v2/ping` | 版本、保存目录、工具是否就绪 |
| POST | `/v2/resolve` | `{url, referer?, ua?, cookies?}` → 机制档案 |
| GET / PUT | `/v2/settings` | 读取 / 保存设置 |
| GET | `/v2/dirs?path=` | 列目录 |
| POST | `/v2/jobs` | 创建下载任务 |
| GET | `/v2/jobs`、`/v2/jobs/{id}` | 任务列表 / 详情 |
| POST | `/v2/jobs/{id}/pause`、`resume`、`cancel` | 任务控制 |
| POST | `/v2/fetch/prepare` | 准备浏览器取流，返回分片清单 |
| PUT | `/v2/fetch/{id}/{index}`、`key`、`map` | 接收分片、密钥、初始段（二进制） |
| POST | `/v2/fetch/{id}/finish` | 校验齐全后合并出片 |
| POST | `/v2/open` | 在资源管理器中打开目录 |

## 目录结构

```
app/                  本地服务
├── api.py            HTTP 接口
├── config.py         配置加载、工具定位、代理探测
├── playlist.py       m3u8 解析
├── profile.py        地址 → 机制档案
├── route.py          选路判定
├── jobs.py           任务状态机与下载进程
├── net.py            HTTP 工具
└── fetch/            取流实现
    ├── local.py      yt-dlp 封装
    ├── direct.py     单文件直链下载
    └── browser.py    浏览器取流与合并

ext/                  浏览器扩展
├── manifest.json
├── background.js     补充捕获经 worker 转发的请求
├── content.js        页面内挂钩 fetch / XHR
├── worker.js         浏览器取流
├── popup.html/js     候选列表
└── dashboard.html/js 下载面板

tests/make_mocks.py   生成测试流
server.bat            启动脚本
```

## 本地自测

```bash
python tests/make_mocks.py
python -m http.server 8899 --bind 127.0.0.1 --directory mocks
```

生成 `mocks/m1_plain`（明文）与 `mocks/m2_aes`（AES-128），分别对应 `http://127.0.0.1:8899/m1_plain/index.m3u8` 与 `m2_aes/index.m3u8`，可验证解析、三级取流与合并。

## 常见问题

**扩展提示「服务未启动」**
`server.bat` 未运行，或端口被占用。先启动服务，再刷新扩展页面。

**任务失败，错误码 `E_SIGN_IP` 或 `E_REFERER`**
地址带有与出口绑定的签名，或被防盗链拒绝。在任务上点击「改用浏览器取流」，由页面内请求同一批分片。

**下载完成但播放器提示「不支持该格式」**
源站使用的是 10-bit H.264（Hi10P），Windows 自带播放器与浏览器不支持。可用 VLC、PotPlayer 或 mpv 播放，也可转码：

```bash
ffmpeg -i input.mp4 -c:v libx264 -pix_fmt yuv420p -crf 20 -c:a copy output.mp4
```

**端口 8765 被占用**
修改 `config.json` 的 `server_port`，并同步修改 `ext/popup.js`、`ext/dashboard.js` 中的 `API`。

## 已知限制

- 仅处理未加密或 AES-128 加密的 HLS：DASH 交给 yt-dlp；SAMPLE-AES、Widevine 等 DRM 内容无法下载
- 浏览器取流只支持 HLS，不支持 BYTERANGE 分片；依赖 CDN 的 CORS
- 清晰度取账号可用档位，可手动选择，不跟随播放器当前档位
- 暂停为断开连接并保留断点，继续时续传；浏览器取流不支持暂停
- 播放列表在服务端解析，若列表本身需要过 Cloudflare 挑战会失败（页面地址不受影响）
- 站点结构变化可能导致失效，属于此类工具的固有问题

## 许可

[MIT](LICENSE)
