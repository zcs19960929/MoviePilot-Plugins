# MoviePilot-Plugins

自用 MoviePilot 插件仓库，目录结构与官方仓库 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 完全一致（`plugins.v2` / `plugins.v3` + `package.v2.json` / `package.v3.json`），同时支持 MoviePilot **V2 / V3**。

## 插件列表

| 插件 | 版本 | 说明 |
|---|---|---|
| [MetaTube 番号刮削](#metatube-番号刮削) | v1.1.0 | 独立访问 115，关联改名视频与字幕，生成本地 STRM、字幕、NFO 和图片 |

---

## MetaTube 番号刮削

调用 NAS 上自建的 **MetaTube** 服务（`metatube-server` v1 API），按番号检索元数据，生成
`movie.nfo` + `poster.jpg` + `fanart.jpg` 并写入媒体目录。
**1.1.0 新增 115 Cookie 直连模式，无需安装或运行 115Lite**，同时负责目录浏览、视频与字幕关联改名、
本地 STRM 与字幕生成，以及播放时获取 115 直链。仍在 MoviePilot 内运行，视频流量通过 302 跳转到 115。

### 115 独立模式快速配置（推荐）

1. 存储类型选择 **115 Cookie 直连（独立生成 STRM）**。
2. 填写 **115 Cookie**（可与 115Lite 相同，包含 `UID/CID/SEID`，有 `KID` 时一并保留）；
   此凭据与 **MetaTube Token** 不同。Cookie 仅由 MoviePilot 保存，不写入 STRM、NFO 或日志。
3. 填写 MetaTube 后端地址和 Token，以及 **MoviePilot 播放入口地址**，如 `https://mp.example.com`。
   该地址须能被 Emby/播放器访问；STRM 使用本插件的稳定入口，播放时重新获取临时直链。
4. **先保存配置**，再检查 Cookie、浏览网盘目录并添加扫描目标。目录可输入 `/jav` 或 `id:123456`，
   浏览器选择的目录以稳定 ID 保存；支持扫描根目录中的散装视频。
5. 填写容器内可写的 **本地 STRM 根目录**，首次使用独立测试目录，例如 `/media/metatube-test`。
6. 启用后先处理一部带字幕的影片。确认视频、字幕改名及本地产物正确，再扩大处理上限。

本地输出示例：

```text
/media/metatube-test/DSOD-060/
  DSOD-060.strm
  DSOD-060.zh.ass
  movie.nfo
  poster.jpg
  fanart.jpg
```

**云盘行为与恢复：**识别到完整元数据并成功暂存字幕、图片后，按文件 ID 在原目录改名视频与关联字幕，
不移动目录、不删除原文件。多段视频保留 `-CD1/-CD2`，字幕保留语言及 `forced/default` 标记。
支持 `srt/ass/ssa/sub/idx/vtt/sup`，单个字幕上限 64 MiB；IDX 必须有配对 SUB。
重名、版本冲突及字幕关联不明会跳过并报告，不擅自覆盖。已改名文件和字幕关联记录在插件数据目录的 SQLite 中，
部分失败后按实际文件状态续跑；已有 NFO 不会阻止补齐缺失 STRM 或字幕。

**本地文件保护：**只更新本插件记录中归属当前影片、且内容未被人工修改的文件。已有其他工具生成的同名文件会报告冲突，
不会自动接管或删除；旧 115Lite STRM 保持原样。更换账户时旧播放标识不可用于新账户。

**请求与播放：**后台请求默认间隔 5 秒并串行执行，这不是保证不触发风控的安全阈值。
遇到 Cookie 失效、验证码或频控会停止任务。视频只提供 302 直连，不能直连时明确报错，不经 NAS 转发视频。
停止定时刮削不影响已生成 STRM 的播放；播放需要 MoviePilot 和本插件持续运行。

**依赖：**直连模式要求 Python 3.12+，插件锁定 `p115client==0.0.9.6.5.1` 与 `python-concurrenttools==0.1.8`，
与当前 madrays 115Lite 的依赖一致。低版本 Python 跳过这些依赖，仍可使用原有模式。
两个插件不会共享可变 Cookie 文件，也不会自动反复扫码登录。

**与 115Lite 共存：**可以同时安装，以上依赖版本与已核对的 madrays 115Lite 版本对齐。
同一个云盘目录建议只交给一个插件整理，本地输出目录也应分开；115Lite 若开启同目录实时监控，
可能再次处理 MetaTube 的改名事件。共用 Cookie 时请求量会叠加，两者的请求间隔并不互相协调。
切换 MetaTube 到直连模式不会自动停用 115Lite 的任务，请调整其监控、同步和整理范围。

新安装默认直连模式；已有配置保留原模式，需主动切换并填写上述配置。以下涉及 StorageChain、115Lite 事件、
网盘写入和自动归位的说明适用于兼容模式，直连模式不依赖这些功能。

### 功能特性

- 按番号调用 MetaTube 检索，支持多数据源按序 fallback（FANZA、JavBus、MGS 等，以 `/v1/providers` 返回为准）
- 生成 Kodi 标准格式 `movie.nfo`，Emby/Jellyfin 勾选 NFO 读取器即可直接使用
- **可视化目录选择**：先选存储（本地 / 115Lite / u115 等），再逐级浏览选择媒体目录，避免手输错误
- 灵活的输出位置：写入本地 STRM 目录（推荐）或网盘目录，支持自动归位（资源库 → 媒体库）
- 刮完可自动通知 115Lite 插件生成 STRM
- 内置防风控：文件写入间隔、单次处理上限、增量跳过、任务运行互斥
- 表单内置「测试数据源 / 立即执行」按钮，插件详情页展示上次运行统计

### 一、适配的服务端

官方镜像 `ghcr.io/metatube-community/metatube-server:latest`，使用 v1 API：

| 接口 | 用途 |
|---|---|
| `GET /v1/movies/search?q={番号}&provider={源}&fallback=true` | 按番号检索 |
| `GET /v1/movies/{provider}/{id}` | 获取完整元数据 |
| `GET /v1/images/primary/{provider}/{id}` | 海报（服务端代理，规避源站防盗链） |
| `GET /v1/images/backdrop/{provider}/{id}` | 背景图（同上） |

服务地址填 MoviePilot 容器视角可达的地址（例如 `http://192.168.1.60:8090`）。
若 MoviePilot 也是 Docker 容器且非 host 网络，不要填 `127.0.0.1`。

### 二、安装

1. MoviePilot 网页端 → 插件 → 插件市场 → 添加仓库：

   ```
   https://github.com/zcs19960929/MoviePilot-Plugins
   ```

2. 刷新插件市场，搜索「MetaTube 番号刮削」安装并启用。

> 备选：直接把 `metatubescraper` 文件夹拷进 MoviePilot 容器的插件目录并重启。

### 三、配置说明

| 配置 | 默认 | 说明 |
|---|---|---|
| MetaTube 服务地址 | - | 如 `http://192.168.1.60:8090` |
| Token | 空 | 服务端启用鉴权时填写相同的原始 Token，**无需添加 `Bearer` 前缀**；未启用时留空。修改后先保存再测试 |
| 数据源优先级 | FANZA,JavBus,MGS,... | 逗号分隔按序尝试；**ID 必须与 `/v1/providers` 返回一致（区分大小写）**。实测 FANZA 最稳；FC2PPVDB/fc2hub 仅对 FC2 番号有效 |
| **存储类型** | 115_direct | `115_direct` Cookie 直连；兼容 `local` 本地存储 / `115Lite` / `u115` / 其他存储名 |
| 115 Cookie | 空 | 直连凭据；保存后再检查与浏览目录 |
| MoviePilot 播放入口地址 | 空 | 直连模式必填，Emby/播放器可访问的 MoviePilot 地址 |
| 115 后台请求间隔 | 5 秒 | 仅直连模式生效；与旧模式的文件写入间隔分别配置 |
| **媒体目录** | - | 每行一个；本地填容器内绝对路径，网盘填网盘内路径（如 `/jav`），用目录浏览器选择最稳妥 |
| 扫描模式 | 一级子目录 | 每个子目录视为一部影片，减少遍历 |
| 文件写入间隔 | 3 秒 | **每个文件写入前等待，防风控核心参数** |
| 单次处理上限 | 30 部 | 分多次执行，避免单次大批量写盘 |
| 定时 | 每日 04:30 | 时 / 分均可自定义，建议夜间执行 |
| 强制覆盖 NFO | 关 | 增量模式：已有 `movie.nfo` 的目录自动跳过 |
| 下载剧照 | 关 | 直连源站原图，可能被防盗链拦截；开启后可设置单部影片剧照张数上限（默认 10） |
| 重命名视频为番号 | **关** | 115 上的重命名属敏感操作，谨慎开启 |
| 识别无字母番号 | 关 | 加勒比/一本道等纯数字番号，有误识别风险 |

### 四、兼容模式的 115 写入注意事项

1. **只增不删不改**：插件默认只新增 `movie.nfo / poster.jpg / fanart.jpg`，不移动、不删除、不重命名
   （重命名功能默认关闭）。挂载盘写小文件总体风险较低，但仍需克制频率。
2. **写入间隔 ≥ 3 秒**：不要为了快把间隔调小；首次全量刮削量大时，靠「单次上限」分几天跑完。
3. **优先用一级子目录模式**：避免递归遍历大量目录触发频繁的列目录请求。
4. **夜间定时 + 增量跳过**：重复执行不会重复写盘，已处理的目录直接跳过。
5. **不要频繁手动触发**：任务运行中重复点击无效（内置互斥锁）。
6. 若遇 115 风控（写入报错/挂载卡死），立即停止任务，暂停 24 小时后再以更大间隔小批量处理。

### 五、媒体服务器配合

- MoviePilot 本身对这类内容识别不了 TMDB 属正常现象，刮削产物以 NFO 为准；
- Emby/Jellyfin 中该媒体库建议：元数据读取器勾选 **NFO**（Kodi 标准格式，本插件产物兼容），
  图片抓取器选择「本地」，即可直接使用 poster/fanart；
- 也可以在 Emby/Jellyfin 安装 MetaTube 官方插件，与本插件互补（本插件负责落盘，媒体插件负责在线兜底）。

### 六、手动触发与 API

- 插件配置表单内置「立即执行」按钮；插件详情页可查看上次运行统计（成功/跳过/未识别/未找到/失败）；
- API 手动触发：`GET /api/v1/plugin/metatubescraper/run`（需 MP 登录凭证或 API_KEY）；
- 数据源自检：`GET /api/v1/plugin/metatubescraper/selftest?q=SSIS-110`，
  用指定番号逐个测试已配置的数据源，区分「provider 不存在 / 搜不到 / 有结果」，排障利器。

**测试前先保存配置**：测试按钮使用服务端已保存的 MetaTube 地址、Token 和数据源，
不会读取表单中尚未保存的修改。自检与正式刮削均通过 `Authorization: Bearer <Token>` 鉴权。
`/v1/providers` 可公开访问，返回 200 不能证明 Token 有效；应使用搜索接口验证。

### 七、存储与目录选择

### 交互方式（与 115Lite 一致的「先选存储、再选目录」）

1. **存储类型**：下拉选择 `本地存储（local）` / `115Lite 网盘` / `MP 原生 115（u115）`，
   也可直接手填其他已配置存储名；
2. **当前浏览路径**：输入起始路径（如 `/` 或 `/jav`），点「读取目录」；
3. **子目录**：下拉列出当前路径下的全部子目录，选中即自动进入；
4. 「上一级」逐层回退；
5. 到达目标目录后点**「添加为媒体目录」**，路径自动追加到下方文本框（每行一个）。

实现原理：表单事件调用 MoviePilot 自带接口 `POST /api/v1/storage/list`
（body: `{storage, path, type:"dir"}`），该接口由宿主统一鉴权，插件无需自行处理 Cookie/Token，
因此 115Lite 里能看到的目录，这里同样能浏览。

### ⚠️ 115Lite 的能力限制（务必了解）

| 存储 | 目录浏览 | 上传（写 NFO/海报） | 说明 |
|---|---|---|---|
| `local` | ✅ | ✅ | 文件系统直写，适用于 rclone / CloudDrive2 挂载路径 |
| `115Lite` | ✅ | ❌ | 存储模块只提供 浏览/建目录/移动/重命名/删除/下载，**不提供上传** |
| `u115`（MP 原生） | ✅ | ✅ | 走 115 开放平台 OSS 分片上传，需先在「存储&目录」完成 115 授权 |

因此推荐两种落地方案：

- **方案 A（推荐，改动最小）**：NAS 上装 rclone 或 CloudDrive2，把 115 挂载成目录并映射进 MP 容器，
  插件存储类型选 `local`，目录用浏览器选到该挂载路径。写入走文件系统，速度最快。
- **方案 B**：在 MP「存储&目录」中添加原生 `115` 存储并完成扫码授权，插件存储类型选 `u115`，
  目录填 `/jav`。写入走 StorageChain 上传，受 115 API 限速约束，务必保持写入间隔 ≥ 5 秒。

> 若误选 115Lite 执行任务，插件会在首次上传失败时给出明确提示并停止继续上传，不会反复重试触发风控。

### 兼容模式的 STRM 前置条件（115Lite 侧）

`/115strm` 与插件自动触发都会校验 115Lite 的三项配置，缺失时返回 `p115lite_strm_full_sync_config_error`：

| 115Lite 配置项 | 示例 | 说明 |
|---|---|---|
| MoviePilot 地址 | `http://192.168.1.60:3000` | 生成 STRM 时要回调 MP 取播放地址，优先填内网地址 |
| 全量同步路径 | `/media/link/japan#/jav#1` | 格式 `本地目录#网盘目录#1`，本地目录必须是**容器内路径**，`#1` 表示启用 |
| 可识别下载后缀 | `srt,ssa,ass`（默认） | 包含需要同步的字幕后缀 |
| 下载媒体数据文件 | 开启 | 需开启全量同步中的此开关，字幕才会下载到本地；仅填写后缀不够 |

### 八、本地 STRM 目录映射与归位

面向「115Lite 生成 STRM + Emby 读 NFO + 爆米花直连 Emby」的架构。

### 新增配置

| 配置 | 说明 |
|---|---|
| 写入本地 STRM 目录 | 默认开。NFO/海报写到本地，与本片 `.strm` 同级，零网盘写入风险 |
| 写入网盘目录 | 默认关。需要存储支持上传（u115 / alist）；115Lite 不支持 |
| 网盘媒体库根目录 | 如 `/jav`，即 115 上媒体库的根 |
| 本地 STRM 根目录 | 如 `/media/strm/jav`，对应 115Lite 配置里「本地 STRM 目录」的容器内路径 |
| 自动归位 | 默认关。把番号影片移动到 `媒体库根目录/{番号}`；需要存储实现 `move` |
| 刮完通知 115Lite 生成 STRM | 默认开。刮削完成后通过事件通知 115Lite（等价于消息命令 `/115strm <目录>`），要求 115Lite 已按上表配置好 |

路径映射规则：网盘 `/jav/SSIS-110` → 本地 `{STRM根}/SSIS-110`，与 115Lite 的 STRM 镜像结构一致。

### 两种落地姿势

**姿势一：115Lite 存储 + 手工归位（无需 115 授权）**

1. 把番号影片从资源库移动到 115 的 `/jav/{番号}/`（手工或现有流程）；
2. 115Lite 跑一次增量同步或 `/115strm`，本地 STRM 目录出现对应文件；
3. 插件存储类型选 `115Lite`，媒体目录 `/jav`，`写本地 STRM 目录=开`、`写网盘=关`；
4. Emby 库指向本地 STRM 目录，元数据读取器勾选 NFO；爆米花直连 Emby。

→ MetaTube 在此模式下将元数据写到本地；115Lite 的字幕下载等功能可能另有网盘请求，不能保证零风控风险。

**姿势二：u115 存储 + 自动归位（全自动）**

1. MP「存储&目录」添加原生 115 存储并扫码授权，记下存储名；
2. 插件存储类型填该名，媒体目录填资源库路径（如 `/待整理`）；
3. 开启「自动归位」，媒体库根目录 `/jav`，本地 STRM 根目录填好；
4. 优先级：先跑 1 部验证 move 与 NFO 落盘，再放大单次上限。

> 115Lite 存储**没有实现 move**，开启自动归位会移动失败并打日志，此时请改用姿势一或 u115。

### 九、本地调试（可选）

不装进 MoviePilot 也可以先单测番号识别与 MetaTube 连通性：

```bash
python3 - <<'EOF'
import re, requests
# 1. 番号识别（与插件逻辑一致）
t = re.sub(r"[\s_.]+", "-", "【高清】MIDV_048 4K.mp4".upper())
print(re.search(r"(?<![A-Z0-9])([A-Z]{2,6})-?(\d{2,5})(?!\d)", t).groups())
# 2. MetaTube 连通性（先查服务端实际可用的数据源）
# curl http://<metatube>:8090/v1/providers
print(requests.get("http://<metatube>:8090/v1/movies/search",
                   params={"q": "MIDV-048", "provider": "JavBus", "fallback": "true"},
                   timeout=10).json())
EOF
```

**本地插件仓调试（V3）**

1. 把本仓库整体上传到 NAS 的 MP 配置卷下，例如 `/config/local-plugins/MoviePilot-Plugins/`；
2. 配置环境变量后重建容器：

   ```yaml
   - PLUGIN_LOCAL_REPO_PATHS=/config/local-plugins/MoviePilot-Plugins
   - PLUGIN_AUTO_RELOAD=true      # 改插件源码后自动热重载，调试必开
   - DEBUG=true                   # 输出更详细的调试日志
   ```

3. MP 网页端 → 插件 → 刷新插件市场，应出现本地源中的「MetaTube 番号刮削」，安装启用。

**调试手段**

```bash
# 容器内验证 MetaTube 连通性（provider ID 区分大小写，以 /v1/providers 返回为准）
docker exec moviepilot curl -s http://<metatube>:8090/v1/providers
docker exec moviepilot curl -s "http://<metatube>:8090/v1/movies/search?q=SSIS-110&provider=JavBus&fallback=true" | head -c 500

# 验证 115 挂载路径在容器内可见、可写
docker exec moviepilot sh -c 'ls /你的挂载路径 && touch /你的挂载路径/.write_test && rm /你的挂载路径/.write_test'

# 手动触发刮削
curl -s "http://<MP地址>:3000/api/v1/plugin/metatubescraper/run?apikey=<你的API_KEY>"

# 看日志（插件日志均带【MetaTube刮削】前缀）
docker logs -f moviepilot 2>&1 | grep -i metatube
```

**常见问题定位**

| 现象 | 排查 |
|---|---|
| 直连模式无法加载 SDK | 检查 MoviePilot Python 是否为 3.12+，以及两项固定版本依赖是否安装成功；不要单独升级这组依赖 |
| 直连生成失败但已有 NFO | 查看上次运行错误，检查本地同名文件归属、字幕下载和命名冲突；直连模式不会因 NFO 存在就跳过缺失产物 |
| STRM 播放返回 404/503 | 检查是否使用本插件生成的播放标识、是否更换 115 账户、Cookie 是否失效，以及播放器能否访问配置的 MoviePilot 地址 |
| 测试数据源全部返回 401 | 更新插件至 v1.0.1 或更高版本（修复自检漏传 Token）；填写与服务端一致的原始 Token，先保存再测试。若仍为 401，检查服务端实际启用的 Token。404/500 是后续检索错误，应按数据源返回详情排查 |
| 统计里「未识别」多 | 开「识别无字母番号」试试；注意噪音词（H265/10bit 等）已内置过滤 |
| 「未检索到」多 | 先 `GET /v1/providers` 确认数据源存在，**provider ID 区分大小写**（如 `JavBus`/`FANZA`/`MGS`，官方镜像没有 javdb/javlibrary/avmoo，填错报 404 provider not found）；或直接调插件 `/selftest` API 逐源探测；看 metatube 容器日志（源站需代理） |
| NFO 写入失败/卡住 | 115 挂载写权限、uid/gid 映射；风控时会有明显延迟或报错 |
| 海报下载失败 | 检查 `/v1/images/primary/...` 是否能通；MetaTube 版本过旧可能无该代理接口 |
| FANZA 返回 `not-available-in-your-region` | **代理出口 IP 不在日本**（DMM 区域限制）。注意：`fallback=true` 命中 DB 缓存时会误以为可用，需用库里没有的番号才能测出。给 metatube 换日本节点代理，`curl https://ipinfo.io/country` 验证 |
| JAV321 报 `certificate is not valid for any names` | 代理做了 TLS 中间人，Go 客户端拒绝。关闭该域名的 TLS 解密/嗅探，或直连 |
| MGS 返回 403 | Cloudflare 拦截数据中心 IP。换住宅节点，或忽略该源 |
| DUGA 返回 503 | 站点自身不可用/限流。换源，无需处理 |
| 插件列表里找不到 | 确认目录名 `metatubescraper` 与类名 `MetaTubeScraper` 大小写对应、package.json 键名为 `MetaTubeScraper` |
| 配置保存后任务时间没变 | `get_service` 的 cron 只在插件启用/配置变更后重新注册 |
