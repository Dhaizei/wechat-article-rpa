# 微信公众号文章采集器

> 在 Windows 微信客户端中按公众号采集文章，提取正文、发布时间与互动数据，并经过校验和去重后写入 MongoDB。

> **项目边界：**本仓库只负责公众号文章的采集、管理与存储，不包含文章摘要生成、每日新闻、编辑日报或消息发送功能。

## 目录

- [系统能力](#系统能力)
- [运行架构与访问边界](#运行架构与访问边界)
- [快速开始](#快速开始)
- [微信与桌面前置条件](#微信与桌面前置条件)
- [管理控制台](#管理控制台)
- [定时采集](#定时采集)
- [局域网与虚拟机部署](#局域网与虚拟机部署)
- [采集规则与数据写入](#采集规则与数据写入)
- [MongoDB 数据库](#mongodb-数据库)
- [高级命令](#高级命令)
- [配置项与数据目录](#配置项与数据目录)
- [常见问题](#常见问题)

## 系统能力

### 采集能力

- 从 MongoDB `weixin.collection_target` 读取待采集公众号；也支持本地账号名单或指定单个公众号。
- 通过微信“搜一搜”的 **账号 → 公众号** 筛选，定位公众号资料窗口；不要求提前关注公众号。
- 按“今天 / 昨天 / 今天和昨天”采集文章，自动跳过置顶内容、历史内容和招聘等推广卡片。
- 从复制出的微信文章链接解析真实的标题、公众号、发布时间与纯文本正文；图片不写入正文。
- 默认仅识别**转发数**，速度更快；需要时可切换为完整互动指标。
- 互动栏优先用 OpenCV 模板匹配和 RapidOCR 本地识别；本地识别不确定时，才由 Qwen-VL 兜底。
- 写入前校验文章窗口标题、正文标题、文章 URL 与互动栏处于同一文章，避免错配入库。

### 管理能力

- 管理员控制台：手动采集、定时任务、启动前检查、实时日志、文章管理、公众号管理、导入导出。

## 运行架构与访问边界

```text
Windows 微信客户端
       │  截图 / OCR / 鼠标键盘操作
       ▼
微信公众号视觉采集器 ──────► MongoDB
       │                         weixin.article
       │                         weixin.collection_target
       └── 管理员控制台（采集、配置、日志、数据管理）
```

控制台页面包括 `/`、`/accounts.html` 和 `/articles.html`，所有业务接口都需要 HTTP Basic 管理员认证。**不要把管理员控制台直接开放到公网。**

## 快速开始

### 1. 获取项目并准备环境

推荐直接从仓库部署，而不是复制另一台电脑的 `.venv`：虚拟环境中含有机器绝对路径，不能跨电脑复用。

```powershell
git clone git@github.com:Dhaizei/wechat-article-rpa.git
cd .\wechat-article-rpa
```

双击：

```text
setup-env.bat
```

安装脚本会在缺少 `uv` 时安装到项目 `.tools`，自动安装并管理 64 位 Python 3.10，创建 `.venv`，再使用清华 PyPI 镜像安装依赖。默认镜像为：

```text
https://pypi.tuna.tsinghua.edu.cn/simple
```

如需手动安装依赖：

```powershell
cd .\wechat-article-rpa
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

复制 `.env.example` 作为配置参考。项目不会读取或提交 `.env` 中的凭据；在 PowerShell 中启动前至少设置 MongoDB 和管理员密码：

```powershell
$env:MONGO_URI="mongodb://127.0.0.1:27017/"
$env:CONTROL_PANEL_PASSWORD="请替换为强密码"
```

也可以直接双击 `start-control-panel.bat`，启动脚本会在本次运行中安全询问管理员密码。

### 2. 启动本机管理控制台

双击：

```text
start-control-panel.bat
```

本机访问：

```text
http://127.0.0.1:8010/
```

首次启动前，请确认微信已登录并已打开“搜一搜”窗口。控制台会执行微信、搜一搜、窗口布局和屏幕缩放的启动前检查。

## 微信与桌面前置条件

这是**桌面视觉采集**，不是无界面 HTTP 爬虫。采集期间需要真实、可交互的 Windows 桌面环境。

1. 微信 Windows 客户端已登录。
2. 微信内的“搜一搜”窗口已打开；控制台可尝试自动拉起，但首次部署建议人工验证一次。
3. 不锁屏、不休眠、不注销，不在采集期间手工切换微信窗口。
4. 采集过程中不要移动鼠标、键盘操作或改变分辨率与缩放比例。
5. 推荐分辨率 `2560×1440`、缩放 `100%`；`1920×1080`、缩放 `100%` 也可使用。控制台会显示当前适配检查结果。
6. 使用远程桌面维护时，避免在采集期间断开/重连导致分辨率变化；优先使用 Hyper-V/VMware 控制台或 Parsec、AnyDesk 等稳定的远程控制方式。

## 管理控制台

控制台提供三类日常操作：

| 页面 | 用途 |
|---|---|
| 控制台 `/` | 手动采集、定时计划、启动前检查、运行进度、日志与失败原因 |
| 公众号管理 `/accounts.html` | 查询、筛选、编辑搜索别名、导入/导出账号配置 |
| 文章管理 `/articles.html` | 查询文章、查看正文、按条件导出 CSV 或 JSON |

### 手动执行

在控制台选择：

- **扫描范围**：今天、昨天、今天和昨天；
- **采集指标**：仅转发数（更快）或全部互动指标；
- **每账号上限**：单次最多读取的文章卡片数。

随后点击“立即开始”。手动任务与定时任务共用同一个进程锁，不能并发启动；需要终止时点击“停止任务”。

### 管理员认证

管理员控制台默认使用 HTTP Basic Auth。部署到局域网前，务必通过环境变量设置强密码：

```powershell
setx CONTROL_PANEL_USERNAME "admin"
setx CONTROL_PANEL_PASSWORD "请设置至少16位的强密码"
```

执行后关闭并重新打开 PowerShell，再重新启动控制台。

> 管理员账号默认是 `admin`，密码没有默认值。启动前必须通过 `CONTROL_PANEL_PASSWORD` 设置强密码。

## 定时采集

控制台中的“定时任务”保存后立即生效，配置文件位于：

```text
config\control_panel.json
```

推荐计划：

| 时间 | 扫描范围 | 目的 |
|---:|---|---|
| `08:00` | 今天和昨天 | 补齐夜间发布和昨日遗漏，供 9 点后的后续 RPA 使用 |
| `22:00` | 今天 | 同步当天晚间新增文章 |

定时器依赖控制台进程常驻。若部署到虚拟机，建议在 Windows“任务计划程序”中创建**用户登录时**启动的任务：

- 操作：运行 `start-control-panel.bat`；
- 选择“仅当用户登录时运行”；
- 不要选择“无论用户是否登录都要运行”，因为微信 UI 自动化需要交互式桌面；
- 设置失败后每 5 分钟重试，最多 3 次；
- 关闭自动睡眠、休眠和锁屏。

## 局域网与虚拟机部署

### 推荐虚拟机规格

| 项目 | 推荐 |
|---|---|
| 操作系统 | Windows 11 Pro 64 位 |
| CPU | 4 vCPU 起步，建议 6 vCPU |
| 内存 | 8 GB 起步，建议 12–16 GB |
| 磁盘 | 80–120 GB SSD |
| 分辨率 | 2560×1440 / 100% 缩放；最低 1920×1080 / 100% |
| 网络 | 固定局域网 IP，能访问你配置的 MongoDB 服务 |

在 VM 中检查 MongoDB 连通性：

```powershell
Test-NetConnection 127.0.0.1 -Port 27017
```

结果中 `TcpTestSucceeded : True` 表示网络连通。

### 在可信局域网访问管理控制台

默认服务只监听 `127.0.0.1`。若确需在可信局域网管理采集任务，可在 VM 上用以下方式启动：

```powershell
cd .\wechat-article-rpa
.\.venv\Scripts\python.exe .\rpa_control_panel.py --host 0.0.0.0 --port 8010
```

查看 VM 的局域网 IP：

```powershell
ipconfig
```

例如 VM IP 为 `192.168.1.88`，管理员访问：

```text
http://192.168.1.88:8010/
```

在 Windows 防火墙中仅允许公司内网网段访问。以下示例仅允许 `192.168.1.*` 访问 8010：

```powershell
New-NetFirewallRule `
  -DisplayName "WeChat RPA Control Panel 8010" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8010 `
  -Profile Private `
  -RemoteAddress 192.168.1.0/24
```

将 `192.168.1.0/24` 替换为你公司的实际内网网段。不要将 8010 端口映射到公网；如确有外网访问需求，应使用公司 VPN 或 HTTPS 反向代理，并额外限制来源地址。

### 迁移清单

| 内容 | 是否迁移 | 说明 |
|---|---|---|
| Git 代码 | 是 | 建议在新 VM 重新克隆远端仓库 |
| `.venv` | 否 | 不可跨电脑复制，必须重新创建 |
| `config/account_aliases.json` | 是 | 保留公众号实际搜索名别名 |
| `config/control_panel.json` | 是 | 保留定时策略与控制台配置 |
| Qwen-VL 环境变量 | 是 | 请在新 VM 独立配置，不要提交到 Git |
| `output` 历史截图和日志 | 按需 | 建议归档，不必作为运行依赖迁移 |
| `config/run_history.json` | 否 | 仅本机历史记录，不影响采集 |
| MongoDB 数据 | 否 | 继续使用部署环境通过 `MONGO_URI` 指定的数据库 |

切换时务必先停止旧机器的控制台与定时任务，再启用 VM 的定时任务，避免两台设备同时操作微信或重复采集。

## 采集规则与数据写入

### 公众号搜索与窗口流程

1. 在微信搜一搜输入目标账号名称；
2. 确认一级分类“账号”和二级分类“公众号”均被选中；
3. 通过本地 OCR 找到同名公众号结果，再打开独立资料窗口；
4. 按时间分组读取文章卡片，向下滚动至“星期 N / 周 N / 明确年月日”等历史边界；
5. 依次打开文章，复制链接解析正文；
6. 校验浏览器标签标题、正文标题和当前 URL，再读取转发数；
7. 文章完成后关闭文章标签；账号完成后关闭资料窗口；保留搜一搜主窗口。

如果数据库中的账号名与微信实际展示名称不同，在 `config/account_aliases.json` 设置一对一别名。别名仅改变微信搜索词，不会改写 MongoDB 中已有账号名称。格式参见：

```text
config\account_aliases.example.json
```

### 去重与质量保护

同一公众号的单次任务采用两层保守去重：

1. **文章卡片层**：时间分组、规范化标题、列表阅读数和列表点赞数均相同才跳过；任何字段缺失或变化都会继续打开，宁可多读不误漏。
2. **文章链接层**：打开后若 URL 与本次已成功文章完全相同，立即关闭，不再重复解析正文和互动数。

标题 OCR 截断或相似标题不会触发跳过；失败文章允许后续重试。去重集合不会跨任务保留，因此后续运行仍会刷新互动数据。

### MongoDB 写入规则

生产采集默认使用：

```text
MONGO_URI=mongodb://127.0.0.1:27017/
数据库：weixin
账号集合：collection_target
文章集合：article
```

使用 `--write-mongo` 后，正文非空文章会幂等写入 `weixin.article`：

- `article.url`：原始文章链接；
- `article.urlNormalized`：标准化链接，用于辅助去重；
- 首次出现：插入文章正文、标题、发布时间、账号和互动数据；
- 再次采集：保护已有非空正文、标题和发布时间，只补齐空字段并追加互动数据快照；
- `account.id` 优先使用 `collection_target` 中同名账号 ID，缺失时生成稳定兜底 ID；
- 正文为空的文章不会写入 MongoDB。

本地输出始终保存在本次运行的 `output` 目录，包括日志、截图、验证证据、JSONL 和 CSV。

## MongoDB 数据库

本项目使用 MongoDB 作为唯一服务端存储。文章正文、账号配置与互动历史都需要可靠持久化，Redis 不适合作为这些数据的主存储，因此开源版不要求安装 Redis。

集合结构、字段含义、索引、初始化、查询和备份方法见 `docs/DATABASE.md`。

## 高级命令

### 只读识别

只截图并识别，不操作鼠标，也不写入数据库：

```powershell
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py
```

### 采集一个已关注公众号

```powershell
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py `
  --run-one-account --live --account-index 0 --max-articles 2
```

### 按名称搜索并采集单个公众号

```powershell
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py `
  --run-search-accounts --live `
  --account-name "量子位" --max-articles 5
```

### 仅使用本地 OCR，不调用视觉模型

```powershell
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py `
  --run-search-accounts --live --local-only `
  --accounts-file .\config\accounts.txt --max-articles 20
```

本地 OCR 无法定位搜索结果、文章卡片或互动数时，此模式会记录失败，不会调用 Qwen-VL。

### 从 MongoDB 读取所有账号并写入文章

```powershell
$env:MONGO_URI="mongodb://127.0.0.1:27017/"
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py `
  --run-search-accounts --live --local-only `
  --accounts-from-mongo --write-mongo --max-articles 20
```

### 采集当前已经打开的文章

```powershell
.\.venv\Scripts\python.exe .\wechat_visual_rpa.py --collect-open-article
```

### 配置 Qwen-VL 兜底

仅在本地 OCR 无法可靠识别时调用。请在启动控制台或采集命令前设置环境变量：

```powershell
$env:QWEN_VL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:QWEN_VL_API_KEY="你的密钥"
$env:QWEN_VL_MODEL="dashscope/qwen3-vl-plus"
```

不要把真实 API Key 写进代码、README、配置文件或提交到版本库。

## 配置项与数据目录

| 路径 / 变量 | 用途 |
|---|---|
| `config/control_panel.json` | 定时任务、扫描范围、指标、每账号上限 |
| `config/account_aliases.json` | MongoDB 账号名与微信搜索名的映射 |
| `config/accounts.txt` | 可选的本地账号名单，每行一个名称 |
| `output/control-panel.log` | 控制台运行日志 |
| `output/<run-id>/run.log` | 单次采集日志 |
| `output/<run-id>/articles.jsonl` | 本次采集的完整文章结果 |
| `output/<run-id>/articles.csv` | 便于检查的表格结果 |
| `output/<run-id>/article_evidence.png` | 单篇文章截图证据 |
| `output/<run-id>/verification.json` | URL、标题和互动栏校验信息 |
| `MONGO_URI` | MongoDB 连接串 |
| `CONTROL_PANEL_USERNAME` | 管理员用户名 |
| `CONTROL_PANEL_PASSWORD` | 管理员密码 |
| `QWEN_VL_*` | Qwen-VL 兜底模型配置 |

## 常见问题

### 采集时提示“未找到搜一搜顶部搜索框”

先确认微信已登录并且“搜一搜”窗口存在且没有被最小化。控制台的“重新检测 / 自动恢复”会尝试拉起窗口；如果仍失败，手工在微信顶部搜索框输入“搜一搜”，在候选项中点击“搜一搜”后重新检测。

### 定时任务到点却没有执行

检查以下前提：

- 控制台启动窗口仍在运行；
- Windows 用户仍登录，桌面未锁定；
- VM 未休眠；
- 微信与搜一搜窗口可用；
- 控制台中已保存并启用定时计划。

### 文章没有写入 MongoDB

检查：

- 命令是否包含 `--write-mongo`；
- `MONGO_URI` 是否能连通；
- 文章正文是否为空；空正文按规则不会写入；
- `output/<run-id>/run.log` 是否出现标题、URL 或窗口校验失败。

### 为什么同一篇文章会被打开或复制链接多次

这是安全校验的一部分。程序会在互动数识别前后分别复制 URL，只有两次 URL 相同才认为互动数与正文属于同一篇文章。失败文章会在有限次数内重试；成功 URL 会在当前任务中去重。

### 为什么不要在采集期间改变远程桌面分辨率

模板匹配、OCR 截图区域和窗口布局都依赖真实桌面坐标。远程桌面重连可能导致 DPI、分辨率或窗口渲染变化，从而降低识别稳定性。

## 运行安全提示

- 不要把控制台端口暴露到公网。
- 局域网开放前必须修改默认管理员密码。
- 不要在采集过程中人工操作微信、鼠标或键盘。
- 不要同时在两台机器上运行同一个微信账号的批量采集。
- 账号别名、运行历史和输出日志属于运行数据；更新代码前建议先提交代码、再单独备份配置与输出目录。
