# 探测结果（2026-10-23）

5 个只读接口全部 `HTTP 200 / code: Success`，**不带 `AppVersion` 头也能用**。请求头只要 `Authorization: <JWT>` 和 `Content-Type: application/json`。

## JWT

- 字段：`aud, exp, iat, persist_uuid, tenant, uid, uuid`。
- 有效期：签发 2026-10-23 20:36，过期 2026-12-22 05:00，约 **60 天**（与 autodl-autopilot 作者说法一致）。正式客户端在剩 7 天时提醒更换。

## 金额单位

所有金额都是 **厘（1/1000 元）**：`assets: 12340` = ￥12.34，`payg_price: 1140` = ￥1.14/时。
核对：无卡 761 秒按 ￥0.1/时 = ￥0.0211，账单记 `asset: 20`（￥0.02）。

## 各接口要点

| 接口 | 用到的字段 |
|---|---|
| `GET /api/v1/time_sync` | `data` = 服务器 unix 秒 |
| `GET /api/v1/wallet/balance?charge_type=payg&product_type=instance` | `assets`（余额）、`voucher_balance`、`accumulate`（累计消费） |
| `POST /api/v1/instance`（列表） | `uuid`、`status`（`running` / `shutdown` …）、`start_mode`（`gpu` / `non_gpu`）、`gpu_idle_num` / `gpu_all_num`（主机空闲卡）、`timed_shutdown_at {Time, Valid}`、`payg_price`、`proxy_host` + `ssh_port`、`started_at` / `stopped_at {Time, Valid}`；分页 `max_page`、`result_total` |
| `GET /api/v1/instance/detail?instance_uuid=` | `status`、`machine.gpu_idle_num`、`machine.gpu_number`、`machine.payg_price` |
| `POST /api/v1/bill/balance_statement/list` | 每行：`uuid`（流水号）、`product_uuid`（实例 ID）、`created_at`（交易时间）、`asset`（厘）、`balance`、`charge_type`（`payg` / `payg_no_gpu`）、`bill_sub_type`（`charge_settle` 开机扣费 / `data_disk_settle` 数据盘）、`details.charge_from` / `charge_to` / `charge_duration`；分页 `page_index`、`page_size`、`max_page` |

比网页抄文字强的地方：账单行直接带实例 ID、有卡/无卡、开机扣费还是数据盘扣费、计费起止时间，记账不用再从备注里解析。

## 写操作实测（2026-10-23 21:10，一台无卡模式的普通实例，`autodl_write_test.py`）

全部 `HTTP 200 / code: Success / data: null`，整个流程 38.7 秒。只测了无卡，**有卡开机没测**（`payload: "gpu"`，同一接口）。

| 操作 | 请求体 | 结果 |
|---|---|---|
| 设定时关机 | `{instance_uuid, shutdown_at: "2026-10-23 22:10"}`（北京时间，到分钟） | 立即生效，列表里 `timed_shutdown_at = {Time: "2026-10-23T22:10:00+08:00", Valid: true}` |
| 取消定时 | `{instance_uuid, shutdown_at: null}` | 立即生效，`Valid: false` |
| 关机 | `{instance_uuid}` | `running` → `shutting_down`（0.3 秒）→ `shutdown`（约 7 秒） |
| 无卡开机 | `{instance_uuid, payload: "non_gpu"}` | `shutdown` → `starting`（1 秒）→ `running`（约 21 秒），`start_mode: non_gpu` |
| 关机 | `{instance_uuid}` | `shutting_down` → `shutdown`（约 8 秒） |

- `sub_status` 全程为空字符串。关机后 `start_mode` 保留上次的模式，不变成 null。
- 状态值：`running`、`shutting_down`、`shutdown`、`starting`。
- 扣费（收支明细）：
  - 关机时结一笔，`bill_sub_type: "charge_shutdown"`；整点结的是 `charge_settle`。
  - 这次开机 1 秒，`charge_from 21:10:59 → charge_to 21:11:00`，扣 10 厘（￥0.01，**每次开机最低消费**）。
  - 计费止于关机请求的时刻（21:11:00），不是状态变成 `shutdown` 的时刻（21:11:06）。
- 测试本身只花了 ￥0.01（余额只少了这次开机的 10 厘，加上测试前已在运行那段的扣费）。

## 算力市场（2026-10-23，只读）

接口来自控制台网页的脚本（`service-code` 模块里的 `user/machine/list` 等）。

| 接口 | 要点 |
|---|---|
| `POST /api/v1/user/machine/list` | 请求体同市场页：`charge_type: "payg"`、`gpu_type_name: [...]`、`region_sign_list: [...]`、`page_index`、`page_size`（100 可用）等。回答 `list`、`result_total`、`max_page`。每行：`machine_id`、`region_name` / `region_sign`、`machine_alias`、`gpu_name`、`gpu_memory`（字节）、`gpu_idle_num` / `gpu_number`、`payg_price`（厘/卡/时）、`highest_cuda_version`、`cpu_per_gpu`、`mem_per_gpu`、`max_instance_disk_size`、`max_data_disk_expand_size`、`on_line`、`max_instance_num` / `binding_instance_num`、`rent_deadline` |
| `GET /api/v1/region/list` | `region_sign`、`region_name`；一个地区名对应多个 sign（如内蒙B区 = nm-B1、nm-B2） |
| `GET /api/v1/machine/gpu_type` | `gpu_name`、`gpu_memory`、`gpu_rank`、`gpu_series_type` |

- 请求体里的 `gpu_idle_num: N`（市场页的"GPU数量"，默认 1）**不去掉**别的机器，而是把它算作有 N 张空闲卡的机器**排在最前**（2026-10-24 实测，10 轮）：读到第一页没有可租机器就停，1–3 页、约 1 秒，找到的占全部可租机器的 83–100%；漏掉的是几分钟内刚空出来的（它的排序比列表里的实时空闲数慢）。全读约 3400 台、34 页、约 16 秒。`gpu_idle_type` 排序不可靠。
- 按机器查一台：请求体加 `machine_id`。
- 市场页判断"可租"：`on_line` 不在 3、4、5、6，`gpu_idle_num > 0`，`max_instance_num > binding_instance_num`。
- 每行的 `machine_base_info` 含主机内网 IP 等，Autopilot 不输出。

## 租用（2026-10-24，从网页脚本读出，**未实际下单**）

- `POST /api/v1/order/instance/create/payg`，请求体 `{instance_info: {machine_id, charge_type: "payg", req_gpu_amount, base_image_uuid | private_image_uuid, expand_data_disk（字节）, …}, price_info: {coupon_id_list: [], machine_id, charge_type, duration: 1, num, expand_data_disk}}`；成功时 `data` 是新实例 ID，网页随后用 `GET /api/v1/instance/exist?instance_uuid=` 等它出现。
- 失败码（网页里的处理）：`MachineGpuNumUpdateFailed`（空闲卡不足）、`TORecharge`（余额不足）、`TORealName`（需实名）、`OrderUnpaidMoreThanThree`。
- 基础镜像：`POST /api/v1/image/get {}`，每项 `uuid`、`framework_name`、`framework_version`、`python_version`、`cuda_version`、`chip_corp`、`cpu_arch`、`is_off`；账号镜像：`POST /api/v1/image/private/get {}`（`image_uuid`、`name`）。

## 文件存储（2026-10-24，实测：上传、列目录、下载、删除）

- 控制台：`GET /api/v1/file_storage/list` 列出各地区：`sign`、`name`、`status`（`created` / `uninitialized`）、`fs_type`（`autofs`）、`export_addr_1` + `port`（该地区的存储服务器）、`free_size`（免费额度，20 GiB）、`initialized_at`。`POST /api/v1/file_storage/init {sign}` 初始化（需绑定微信；未实测）。没有删除存储本身的接口。
- 地区存储服务器 `https://<export_addr_1>:<port>/api/v1/file_storage`，请求头同样只要 `Authorization: <JWT>`：
  - `GET /usage` → `current_usage`、`total`；`GET /detail` → 空间与 inode。
  - `GET /dir/v2?dir_path=/a/b` → `[{name, size, updated_at, is_dir}]`，不存在的目录回答空列表。
  - 上传：`POST /file`，multipart，每块 5 MiB（网页 WebUploader 的设置），字段 `id=0`、`name`、`size`、`chunks`、`chunk`、`chunk_offset`、`md5`（文件前 1 MiB 的 MD5）、`is_last`、`file_uuid`（同一文件各块相同）、`dir_path`、`filename`，文件部分名为 `file`；每块回答 `data` = 块号。目标目录不存在时自动创建。`GET /file/progress/<file_uuid>` 为进度。
  - 下载：`POST /file/download_token {filename, dir_path}` → 令牌；`GET /download/file/<令牌>?auth=<JWT>`。**这个链接不带登录令牌也能下载**，所以 Autopilot 从不输出它。
  - 删除：`DELETE /file {filename, dir_path}`；`DELETE /dir {dir_name, dir_path}`。
- 实测：两块的文件上传、列目录、下载逐字节一致、删除文件与目录，根目录与用量恢复原状。

## 数据盘扩容 / 缩容（2026-10-24，读取与报价实测，**未实际下单**）

- `GET /api/v1/instance/snapshot?instance_uuid=` → `expand_data_disk_size`（已扩容字节）、`machine_info_snapshot.max_instance_disk_size`（含的空间，如 50 GiB）。
- 报价（不下单）：`POST /api/v1/order/data_disk/change/preview {change_size, data_disk_expand_size（变化后的扩容总量）, instance_uuid, machine_id, opt_type: data_disk_expand | data_disk_reduce}` → `max_expand_size`、`max_reduce_size`、`data_disk_used`、`data_disk_deal_daily_price`（厘/天，扩容总量的价格；10 GiB 为 70 厘）、`prepay_expand_data_disk`。
- 下单：`POST /api/v1/order/data_disk/change`，请求体同上，但网页把 `data_disk_expand_size` 也填成 `change_size`。
- 网页的规则：运行中可以扩容，但提示"运行中的实例扩容数据盘后需重启实例生效"；缩容只能在关机状态（"为确保数据一致，请在关机状态下缩容数据盘"），且只限按量计费；按量计费"当日产生的费用以最高容量配置计费"。

## 仍未验证

- 有卡开机（`payload: "gpu"`）：主机空闲卡为 0 时的错误码与提示。
- 余额不足、实例不存在时的 `code` 值（正式客户端要据此给出明确提示）。
- JWT 无效 / 过期已测：仍是 HTTP 200，`code: "AuthorizeFailed"`，`msg: "登录超时，请重新登录; 登录失败，请重试"`。所以**不能只看 HTTP 状态码**，必须看 `code`；遇到 `AuthorizeFailed` 就提示更换 JWT，并退回浏览器方式。
