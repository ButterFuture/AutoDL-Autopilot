# AutoDL 控制台接口只读探测

`autodl_probe.py` 用控制台登录 JWT 调下面 5 个**只读**接口，把返回结构打码后打印出来，用来在写正式客户端之前确认请求和返回的格式。脚本里只有这 5 个接口，没有开关机、定时、克隆、释放的代码。

| 名字 | 请求 | 想确认的 |
|---|---|---|
| `time_sync` | `GET /api/v1/time_sync` | 服务器时间（unix 秒） |
| `balance` | `GET /api/v1/wallet/balance` | 余额字段与单位（`assets`、`voucher_balance`） |
| `instances` | `POST /api/v1/instance` | 实例列表字段：状态、无卡模式、GPU 余量、定时关机 |
| `detail` | `GET /api/v1/instance/detail?instance_uuid=` | 单个实例详情 |
| `bills` | `POST /api/v1/bill/balance_statement/list` | 收支明细的分页与字段（记账用） |

只需要 Python 3.8+，不装任何依赖。

## 1. 取 JWT

1. 浏览器登录 <https://www.autodl.com/console/instance/list>。
2. 按 F12 打开控制台，运行：`copy(localStorage.token)`。JWT 已在剪贴板里。
3. **不要**把它贴进聊天、终端命令行参数或项目文件。

## 2. 存起来（脚本按 环境变量 → 配置文件 → 系统凭据库 的顺序找）

**推荐用配置文件（A）。** 系统凭据库在没有桌面登录态时经常读不到：macOS 登录钥匙串对 SSH 会话、后台任务会直接拒绝（`User interaction is not allowed`，在这台 Mac 的 SSH 会话里实测过），Linux 的 libsecret 也要已解锁的桌面会话。AI 代理、定时任务、息屏后的自动开关机恰好都是这种情况。脚本读凭据库最多等 5 秒，读不到就跳过，不会卡住，但只存在凭据库里的话那时就取不到 JWT。

### A. 配置文件（推荐，所有平台通用，SSH / 锁屏 / 后台都能读）

文件位置：macOS / Linux 是 `~/.config/autodl-autopilot/credentials`，Windows 是 `%APPDATA%\autodl-autopilot\credentials`（也可用环境变量 `AUTODL_REMOTE_CONFIG` 指定）。内容一行：

```
AUTODL_WEB_JWT=<粘贴 JWT>
```

macOS / Linux 上先建目录：

```bash
mkdir -p ~/.config/autodl-autopilot && chmod 700 ~/.config/autodl-autopilot
```

不经过命令行参数、直接从剪贴板写进文件（macOS；Linux 把 `pbpaste` 换成 `xclip -o -selection clipboard` 或 `wl-paste`）：

```bash
(umask 077; printf 'AUTODL_WEB_JWT=%s\n' "$(pbpaste)" > ~/.config/autodl-autopilot/credentials)
```

文件必须是 0600，否则脚本拒绝读取。手工编辑过的话补一下：

```bash
chmod 600 ~/.config/autodl-autopilot/credentials
```

安全上它和钥匙串差别不大：用 `security` 命令存进去的条目，同一用户下的任何程序也都能用 `security` 免提示读出来。磁盘层面的加密靠 FileVault。

### B. 系统凭据库（可选，只在有桌面登录态时可靠）

下面的命令都会**提示你输入**，粘贴 JWT 回车即可，JWT 不会进 shell 历史。

macOS（钥匙串）：

```bash
security add-generic-password -U -s autodl-autopilot -a web-jwt -w
```

Linux（libsecret，需 `secret-tool`，Debian/Ubuntu 装 `libsecret-tools`）：

```bash
secret-tool store --label='autodl-autopilot web JWT' service autodl-autopilot account web-jwt
```

Windows（凭据管理器，PowerShell 或 cmd）：

```bat
cmdkey /generic:autodl-autopilot:web-jwt /user:web-jwt /pass
```

> Windows 与 Linux 这两条还没在真机上测过；不行就用 A。配置文件和凭据库都有时，用配置文件里的。

### C. 环境变量（临时用、CI）

`AUTODL_WEB_JWT`。别写进 `.zshrc` 之类会被同步或分享的文件。

## 3. 运行

用配置文件的话，在哪里跑都行（本机终端、SSH、AI 代理）：

```bash
python3 -I dev/probe/autodl_probe.py
```

常用参数：

- `--uuid <实例ID>`：`detail` 查哪台；不给就取列表里第一台。
- `--source env|file|store`：只从某一处取 JWT，用来验证那一种存法。想确认锁屏时能不能用：锁屏后从另一台机器 SSH 进来跑一次 `--source file`。
- `--only balance bills`：只探测其中几个。
- `--app-version <版本>`：带上控制台前端会发的 `AppVersion` 头。默认不带；若接口报错，再带上试一次。
- `--save-raw <目录>`：另存一份**不打码**的原始返回（目录 0700、文件 0600）。只给你自己看，别放在 Dropbox 里，也别贴出来，例如 `~/.autodl-autopilot/probe`。

## 4. 把结果给我

直接贴打印出来的 JSON 就行。已经打码的有：密码、各类 token、`ssh_command`、手机号、邮箱、姓名/昵称、看起来像 JWT 的字符串；列表只留前 3 条。贴之前扫一眼，看到漏网的个人信息就手动删掉。

`jwt` 一栏只显示签发和过期时间（`days_left` 是剩余天数），用来确认 JWT 的有效期到底多长。
