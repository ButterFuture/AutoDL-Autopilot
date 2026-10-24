# AutoDL Autopilot 设计

## 目标

让 AI（Claude Code 等）替用户管理 AutoDL 普通容器实例：有卡或无卡开机、在实例上运行任务、空闲自动关机、按预算控制花费。本机只需 Python 3.8+ 与 OpenSSH 客户端，macOS、Linux、Windows 通用，不依赖浏览器。

## 组成

```
autopilot/            本机 Python 包（只用标准库）
  console.py          AutoDL 控制台 HTTP 接口：实例、余额、账单、服务器时间、开关机、定时关机
  creds.py            控制台令牌：环境变量 → 凭据文件(0600) → 系统凭据库（5 秒超时，失败即跳过）
  paths.py            各平台的配置目录与状态目录
  state.py            本机状态（JSON + 文件锁 + 原子写）：授权、账本、开关机记录
  budget.py           授权与预算判断；账本以控制台账单为准
  remote.py           SSH：由控制台读出主机与端口直接连接，先核对主机名再执行
  guard.sh            部署到实例上的空闲守护（bash）
  cli.py              命令行入口 autopilot
bin/autopilot         POSIX 启动器；bin/autopilot.cmd：Windows 启动器
SKILL.md              给 AI 的操作规程
```

## 关键决定

1. **控制台是唯一的事实来源。** 状态、价格、空闲 GPU、SSH 地址从实例列表读；花了多少钱从收支明细读。本机不自己推算会话时长，只缓存账单行（按流水号去重）。
2. **SSH 不需要别名。** 每次从实例列表取 `proxy_host` 与 `ssh_port`，用 `ssh -p 端口 root@主机` 加用户指定的私钥连接。主机密钥记在本工具自己的 `known_hosts`（首次自动接受，之后变了就拒绝）。每条远端命令先比较 `hostname` 与 `autodl-container-<实例ID>`，不一致立刻退出，不做任何事。
   实例拒绝这把密钥时（`Permission denied`，什么都没执行），若设置 `add_key` 为 true（默认），就从实例列表读出该实例的 `root_password`，用密码登录一次（同样先核对主机名），把公钥追加到 `/root/.ssh/authorized_keys`（已有则不重复，目录 0700、文件 0600），然后用密钥重试原命令。密码经 `SSH_ASKPASS` 交给 ssh：本工具在状态目录写一个只回答密码提问的小程序，密码只放在 ssh 子进程的环境变量里，不进命令行、文件、输出与日志。每条命令最多补一次；补完仍被拒绝就报错退出（退出码 2）。`authorized_keys` 在系统盘上，关机再开仍在。
3. **开机即设兜底定时。** 每次经本工具开机，都先在控制台设一个"现在 + 兜底时长"（默认 30 分钟）的定时关机，再发开机请求；守护程序在实例上启动成功后才取消它。所以从开机到守护就位之间，即使 AI 的对话断了，实例也会被平台关掉。用户要求的硬上限（`--hard-limit`）则保留为最终的定时关机。
4. **守护程序每次开机由本工具启动**，不往系统盘写开机钩子。用户手动在控制台开机时没有守护（文档说明）。
5. **请求只发一次。** 开机、关机、定时关机的请求没收到回答时不重发，而是读状态查明，命令以"不确定"退出（退出码 6）。
6. **只做开关机与租用这一类事。** 接口白名单之外的请求（释放、重置、克隆、迁移、充值、续费等）代码中不存在。租用只走按量计费（`/api/v1/order/instance/create/payg`），每次都要用户同意那一份方案（`--yes` 与用户原话），请求只发一次；没有回答时按机器 ID 与创建时间在实例列表里找新实例，找不到就以"不确定"退出。算力市场只读：`/api/v1/user/machine/list`（POST 查询，按型号 `gpu_type_name` 与地区 `region_sign_list` 筛选）、`/api/v1/region/list`、`/api/v1/machine/gpu_type`。请求带 `gpu_idle_num: N` 时，控制台把它算作有 N 张空闲卡的机器排在最前面（其余的排在后面，并不去掉），所以读到第一页没有可租机器就停（约 1 秒）；它的排序比实时空闲数慢几分钟，刚空出来的机器可能排在后面，`--thorough` 读完全部页（约 16 秒），空闲那一段一台都不合适时也自动读完。筛选、排序在本机做；可租的判断与市场页相同：`on_line` 不是 3–6、有空闲卡、主机还能再开实例。

## 预算

授权按实例保存：允许的模式（gpu、nogpu、both）与每月金额上限（可为不设）。开机前：

```
本月已花 = 本月该实例账单行之和（开机前先同步账单）
在途估计 = 若实例正在运行：(现在 - 最近一笔扣费的结算时刻) × 单价
本次预计 = 单价 × 计划小时数
本月已花 + 在途估计 + 本次预计 ≤ 上限  才开机
```

月份按北京时间划分（AutoDL 的账期）。延长运行（`autopilot extend`）同样先过这道判断。

## 守护程序（实例端）

`guard.sh` 以后台进程运行在实例上，工作目录 `/root/autodl-tmp/.autopilot/`：

- 每 `interval`（默认 60 秒）采样一次：GPU 利用率（`nvidia-smi`）、本容器 CPU 用量（cgroup `cpu.stat`，回退到 `/proc/stat`）、网络收发字节（`/proc/net/dev`）、登记的任务进程是否存活。
- **忙**（默认逻辑）：任一信号超过阈值（GPU ≥ 5%，CPU ≥ 0.2 核，网络 ≥ 200 KB/s）。读不到的信号按忙算（宁可多开）。进程存在本身不算忙，`autopilot run` 启动的任务也一样：挂住或空等、什么都不占用的进程不能让实例一直计费。
- **白名单**：`$AP/whitelist`，每行一条（名字、类型、值、到期时刻）。类型为 `pid`（进程号）、`job`（`autopilot run` 的任务名）或 `cmd`（命令行中的一段文字，按字面匹配，守护程序自身除外）。条目未到期且匹配的进程还活着时，实例视为忙（`protect:名字`）。时效 1 秒到 90 天，在实例上按实例的时钟计算；条目存放在数据盘，跨开机有效，到期后不再起作用，`unprotect` 删除。每次采样在 `status.json` 里列出各条目的状态：protecting、no_process、expired。
- **保持**：`hold_until` 之前一律视为忙（用户说"先别关"，不针对某个进程）。
- **任务跑完即关**：登记为 `then_off` 的任务结束后，宽限 `grace`（默认 2 分钟）内没有新任务就关机。
- 连续空闲满 `idle` 就执行 `sync` 后 `/usr/bin/shutdown`。默认值按开机模式：有卡 15 分钟（`idle_min`），无卡 60 分钟（`idle_min_nogpu`；无卡常用来下载，下载本来就慢，空闲一小时也只花 0.1 元）；`--idle` 只管这一次。
- 每次采样写 `status.json`（各信号读数、空闲已累计秒数、预计关机时刻），追加 `guard.log`。
- 心跳：本机可据 `status.json` 的时间判断守护是否还活着。

## 守护开关

- `config.json` 的 `guard_default`（默认 true）决定每台实例是否启用守护。
- `autopilot guard ID on|off` 在本机状态的 `instances` 里为单台实例覆盖默认值，`guard ID auto` 取消覆盖。实例正在运行时立即生效：开启即部署并启动守护，关闭即停止守护循环（白名单与任务保留）。
- `autopilot on` 的取值次序：本次命令的 `--guard`/`--no-guard`，实例自己的设置，`guard_default`。不启用守护时，实例进入运行后撤掉兜底定时（除非给了 `--hard-limit`），输出里明确警告它会一直运行到 `autopilot off`。

## 任务

`autopilot run <实例> <名字> -- <命令>`：在实例的数据盘工作目录里用 `setsid nohup bash -lc '<环境>; <命令>'` 启动，输出写 `jobs/<名字>.log`，PID 写 `jobs/<名字>.pid`，登记到守护程序。`autopilot jobs` 列出状态与退出码，`autopilot tail` 看日志末尾。

## 命令一览

| 命令 | 作用 |
|---|---|
| `token set/status/forget` | 存、查看、删除控制台令牌 |
| `status [ID]` | 实例状态、模式、空闲 GPU、单价、定时关机、释放日期；有守护时附守护状态 |
| `balance` | 账户余额 |
| `grant ID --modes --budget` / `grants` / `revoke ID` | 授权与预算 |
| `on ID --mode gpu\|nogpu --hours H` | 预算判断 → 兜底定时 → 开机 → 等到运行中 → 部署并启动守护 → 取消兜底 |
| `off ID [--force] [--console]` | 经 SSH 关机（有任务在跑则拒绝，除非 `--force`）；SSH 不通时 `--console` 走控制台 |
| `run / jobs / tail / hold` | 任务与保持（`run --protect 1d` 同时把任务加进白名单） |
| `protect ID NAME --pid\|--job\|--cmd --for 7d` / `unprotect` / `protected` | 进程白名单 |
| `guard ID on\|off\|auto\|status` | 单台实例的守护开关；全局默认值 `config set guard_default` |
| `push / pull` | 传文件（scp） |
| `sync ID` / `spend [ID]` | 同步账单；本月花费 |
| `market [--gpu --region --gpus --max-price --min-cuda --min-memory --like ID --sort --all --list gpus\|regions]` | 查算力市场：按型号、地区、空闲卡数、单价、CUDA、显存筛选机器（只读，不下单） |
| `images [--framework] [--private]` | 可用的基础镜像（或账号自己的镜像） |
| `rent MACHINE --gpus N --hours H --budget B [--image] [--expand-disk] [--yes --said]` | 在市场的一台机器上租按量实例：不带 `--yes` 只给方案；带上则下单一次 → 记授权 → 兜底定时 → 等到运行 → 装公钥、启动守护 → 取消兜底 |
| `disk ID [--add GB \| --remove GB \| --remove-all] [--yes --said]` | 数据盘：查看；扩容或缩容先给方案（扩容后的大小、日价、何时生效），带 `--yes` 才下单。运行中扩容需重启才生效；缩容只能在关机时、按量计费实例 |
| `storage list\|init\|ls\|put\|get\|rm REGION ...` | 文件存储：地区列表、初始化（`--yes`）、列目录、上传、下载、删除（文件夹需 `--recursive`） |
| `connect ID` | 确认密钥能登录运行中的实例（被拒时补上公钥），给出手动登录的 ssh 命令 |
| `doctor` | 检查 Python、ssh、令牌、密钥、目录权限 |

退出码：0 成功；1 错误；2 连不上（什么都没做）；3 拒绝（什么都没做）；6 不确定（请求已发出、未收到回答）；10 超预算；12 没有授权；13 连到的不是这台实例；14 没有可用令牌。

## 本机文件

| 平台 | 配置（令牌、私钥路径） | 状态（授权、账本、known_hosts） |
|---|---|---|
| macOS / Linux | `~/.config/autodl-autopilot/` | `~/.local/state/autodl-autopilot/` |
| Windows | `%APPDATA%\autodl-autopilot\` | `%LOCALAPPDATA%\autodl-autopilot\` |

目录在 POSIX 上为 0700、文件 0600。`AUTODL_AUTOPILOT_CONFIG` 与 `AUTODL_AUTOPILOT_STATE` 可另指（测试用）。
