# CardPulse 飞牛 NAS 部署

## 当前状态

本仓库已经具备 fnOS FPK 的目录、生命周期和运行时契约；这些是本机代码与自动化
检查的证据，不等同于真实 NAS 交付。56 号 NAS 已完成一次实时只读基础核验：主机为
`x86_64`，Docker 网络可用且存储充足。

尚未在 56 上执行 FPK 安装、统一网关 socket 验证、容器设备透传、升级或卸载验收。
QDC507 已接入 56 并被识别为 `2ca3:4006`（`BAIWANG/Baiwang`，USB 路径
`/sys/bus/usb/devices/1-3`）。初始只读核验时，五个 Vendor Specific interface 均未绑定
驱动；当前运行期的宿主预置已完成：`option` 已绑定五个 USB
interface，`/dev/ttyUSB3` 已通过 `AT`、`AT+CPIN?` 和 `AT+CREG?` 的只读验收，固定别名
`/dev/cardpulse-at` 解析到该端口，源设备权限为 `root:dialout 0660`。

由于 `option` 不包含 `2ca3:4006` 的内置 alias，也没有持久模块参数，56 的管理员另行
预置了 root-owned `/usr/local/lib/cardpulse/qdc507-option-bind` 和
`/etc/udev/rules.d/80-cardpulse-qdc507-bind.rules`。它们只在匹配的 USB add 事件中加载
`option` 并注册 `new_id`，不运行 AT、不触碰 Docker、FPK、调度或短信。它们是宿主资产，
不属于 FPK，也不会由 FPK 创建、升级、删除或调用。post-helper 的物理拔插已验证：设备
断开后自动重新枚举、五个 `option` TTY 与 `/dev/cardpulse-at` 恢复，且 AT/SIM/网络注册
只读检查通过。重启稳定性、非 root 容器权限和设备 POC 仍待实机验收。只有使用真实的
`linux/amd64` OCI digest 构建并验证过的 `.fpk` 才能用于手动上传；示例或占位镜像不能
作为部署包。

## FPK 产品形态

飞牛 NAS 是唯一生产宿主。飞牛环境仅支持以下形态：手动上传 `.fpk`、一个 Docker Compose 项目、一个
CardPulse 容器。应用由飞牛统一网关在 `/app/cardpulse` 提供服务，不开放 LAN TCP
端口，也不使用 Caddy。

- `${TRIM_APPDEST}/app.sock` 是宿主上的网关 socket；容器在
  `/run/cardpulse/app.sock` 创建同一个 socket。
- **统一网关 socket 约束**：[飞牛官方文档](https://developer.fnnas.com/docs/core-concepts/gateway-registration/)
  规定 `gatewaySocket` 只能填写 socket 文件名，且必须位于已安装应用的 `target`
  根目录；Docker 应用也必须挂载 `${TRIM_APPDEST}`，再在其中创建该 socket。因此根
  `app.sock` 是平台契约，不能凭猜测改为子目录。只读 POC 会验证实际 Docker 挂载，并
  确认降权后的 `cardpulse` 进程不能写入 `docker/`、`ui/` 和诊断包资产；镜像以固定
  digest 运行，入口脚本设置目录权限后降权，由非 root 的长期进程创建 socket。
- `${TRIM_PKGVAR}` 持久化为容器内 `/var/lib/cardpulse`，保存 `config/`、`state/`、
  历史、审计和调度状态；不声明用户可见的 `data-share`。
- `config/` 由 lifecycle 账户以 `2750` 独占；`config/config.yaml` 是唯一的跨身份配置
  对象，以私有 package group 的 `0660` 模式供 hook 与非 root `cardpulse` runtime 使用。
  认证、审计和其他状态文件仍为 `0600`。
- 网关仅允许飞牛管理员访问。FPK 模式不需要第二个 CardPulse 密码；本地开发或
  诊断模式仍保留原有密码鉴权。
- Web 以 `--base-path /app/cardpulse` 工作，API、静态资源、Cookie、CSRF 和重定向
  都被限制在该前缀下。
- FPK 不使用 `privileged: true`，不整挂 `/dev` 或 USB 总线，不自动选择“第一个
  串口”。

## 安装前边界

构建方应在具备 `fnpack` 和发布权限的本地构建环境中，使用发布后的固定
`linux/amd64` 镜像 digest 运行：

```bash
CARDPULSE_IMAGE='registry.example/cardpulse@sha256:<real-64-hex-digest>' \
  sh scripts/build-fnos-fpk.sh
```

将占位镜像引用替换为真实的 64 个十六进制字符 digest。脚本会先在线拉取该引用的
`linux/amd64` 镜像并核对架构，运行 `CardPulse --version` 并要求其与
`CARDPULSE_FPK_VERSION` 完全一致，再输出带发布版本和完整镜像 digest 的唯一文件，例如
`packaging/fnos/dist/cardpulse-1.1.1-sha256-<64-hex-digest>.fpk`，并校验包结构。应始终使用
构建脚本打印的精确路径；不得凭旧文件名选择或上传先前残留的 `.fpk`。
只有在获授权
的维护窗口，才可将该输出在飞牛应用中心手动上传。首次安装的向导默认选择
**no-device** 模式，应用可以在未接入 QDC507 时启动并提供受限状态。

只有管理员显式选择设备模式 `enabled`，且宿主已经存在经过验证的字符设备
`/dev/cardpulse-at` 时，生命周期才会启用唯一的
`/dev/cardpulse-at:/dev/cardpulse-at` 映射。别名缺失、不是字符设备或设备模式未启用
时，应用保持 no-device 降级模式；不会猜测或扫描其他串口。

QDC507 的驱动、USB 识别和 `udev` 稳定别名是一次性的宿主预置责任，必须在另行
授权的维护窗口完成。FPK 只消费已经验证的 `/dev/cardpulse-at`，不会安装或修改
宿主驱动、`udev` 规则、Caddy、`systemd` 单元或 cron。旧原生 NAS 脚本也不是 FPK
生命周期的一部分，不能用于 FPK 安装、升级或设备准备。

56 的持久驱动绑定使用的 `/usr/local/lib/cardpulse/qdc507-option-bind` 与
`/etc/udev/rules.d/80-cardpulse-qdc507-bind.rules` 也必须始终由宿主管理员单独维护。
这两个文件不在 `packaging/fnos/`、不由 `scripts/build-fnos-fpk.sh` 复制，并且 FPK
lifecycle 不得读取、修改或调用它们。若将来需要改变绑定逻辑，管理员必须在独立维护窗口
手动更新宿主文件并重新完成硬件验收。

### 原生迁移门禁

每次 FPK 安装和升级都会只读检查旧原生部署的 `/opt/cardpulse`、
`/var/lib/cardpulse`、已知 CardPulse `systemd` 单元，以及 root/cardpulse crontab、
`/etc/crontab`、`/etc/cron.d` 中的 CardPulse 条目。发现任一项会在创建或变更 FPK
应用路径前拒绝继续。

FPK 不会停止、禁用、删除或编辑这些宿主服务、cron、驱动、`udev` 规则或旧数据。
管理员应先备份旧配置与状态，审阅拒绝信息中列出的准确对象，在已授权的迁移窗口内
人工处理对应旧原生调度，再重新执行 FPK 生命周期。这样可以避免宿主
`systemd`/cron 与容器调度器形成两条短信发送路径；旧状态不会被自动迁入 FPK 私有
目录。

## 无设备平台验收

在 QDC507 尚未完成宿主 AT 串口预置、或未被应用启用时，只能在获授权的维护窗口运行
打包后的平台 POC：

```bash
"${TRIM_APPDEST}/diagnostics/fnos-platform-poc.sh" \
  --socket "${TRIM_APPDEST}/app.sock" \
  --container cardpulse \
  --record-persistence
```

它检查单容器 Compose、非特权与 `no-new-privileges`、无设备映射与端口映射、
`${TRIM_PKGVAR}`/`${TRIM_APPDEST}` 的真实挂载源、长期 Web 和 scheduler 进程的非 root
身份，以及私有卷写后读回。`--record-persistence` 只会在私有 state 目录写入平台 POC
记录，不构成 QDC507 验收，也不会开启调度。

该脚本通过 Unix socket 注入的管理员 header 只验证后端连通性；它明确**不**验证飞牛
HTTPS 网关、管理员隔离或伪造 header 清洗。后续仍必须在 56 上分别以管理员、普通用户
和带伪造 `X-Trim-Isadmin`/`X-Forwarded-*` 的外部请求完成真实 HTTPS 验收。

## QDC507 只读验收

设备模式已启用且 `/dev/cardpulse-at` 已由宿主验证后，管理员才可在获授权的实机验收
窗口运行打包后的只读 POC。其实际路径和 socket 路径如下：

```bash
"${TRIM_APPDEST}/diagnostics/fnos-readonly-poc.sh" \
  --socket "${TRIM_APPDEST}/app.sock" \
  --container cardpulse \
  --require-device \
  --record-acceptance
```

该 POC 会确认容器不是特权容器、未使用 host network、没有 `/dev`/USB bind mount 或端口映射，
并确认唯一固定设备映射、后端可经 Unix socket 访问，以及 `cardpulse` 非 root 用户可读写
该字符设备；然后只运行 `--doctor`、`--info`、
`--sms-status` 和 `--status`。它不发送短信、不删除短信。成功后才会把只读验收记录
写入容器持久目录的
`/var/lib/cardpulse/lifecycle/qdc507-readonly-acceptance.json`；该目录和记录归
fnOS lifecycle 身份所有，`cardpulse` runtime 只能读取，不能替换或写入。失败不会产生
可用的验收记录。该记录必须包含固定别名解析后的设备路径、`2ca3:4006` USB ID、当前
runtime version、完整 `repo@sha256:<digest>` image reference、FPK package version 和
`enabled` 设备模式；运行容器的 `CARDPULSE_RUNTIME_VERSION`、
`CARDPULSE_RUNTIME_IMAGE` 和 `CARDPULSE_FPK_VERSION` 必须分别完全匹配。镜像 digest、
FPK 版本、设备模式、运行版本、宽松权限或未来时间戳任一变化都会使其失效。no-device 模式故意不满足这个 POC，不能将其结果
视作硬件验收。Unix socket 检查不替代上节的真实 HTTPS 网关验收。

## Web 与调度

设备 overlay 只用于让已固定的 `/dev/cardpulse-at` 参与 QDC507 只读 POC；它本身不构成发送授权。自动保号仍必须同时满足有效短信配置、成功的只读验收记录以及飞牛管理员在 Web 设置页的显式开启。fnOS 写操作只接受统一 HTTPS 网关注入的管理员与转发来源头；56 实机验收必须确认普通用户即使伪造 `X-Trim-Isadmin` 或 `X-Forwarded-*` 也不能访问应用或取得 CSRF token。

FPK Web 启动时会执行一次 `--sms-status` 作为收件箱容量基线审计。这是只读查询，
不发送也不删除短信。启动时还会收紧配置、鉴权和受管状态文件权限。

`scheduler.enabled` 在首次安装、容器重启和应用升级后均保持 `false`。Web 设置页只有
在收件人、短信内容和周期有效，且 QDC507 只读验收记录通过时，才允许飞牛管理员
显式开启调度；容器调度器在每次轮询前也会复核同一验收记录，记录缺失或无效时不会
发送。不存在第二条宿主
`systemd`/cron 发送路径。

真实发送仍需要用户另行明确授权。收件箱删除继续使用现有确认令牌与审计约束；满仓
只告警，不会自动清理。

## 升级、卸载与实机验收

本地 `upgrade_init` 会把持久化的 `scheduler.enabled` 写为 `false`，替换容器启动后也会
再次保持关闭；这不等同于飞牛已经先停止旧容器。56 实机升级 POC 必须明确证明旧容器已经完全停止
或已经完成调度静默，且旧 Web/scheduler 在 hook 执行后不能再读取或写入该配置，更不能
保留一条已读取的发送路径。若平台不能提供这个顺序保证，FPK 不得发布。不得用 lifecycle
脚本调用 Docker CLI 来尝试关闭旧容器，因为这既不是可移植的 fnOS 生命周期契约，也会绕开
平台的应用管理边界。

卸载向导把私有数据默认设为 `retain`，因此默认保留 `${TRIM_PKGVAR}` 中的配置、历史、审计和
验收记录；只有管理员在卸载向导中明确选择 `delete` 时，`uninstall_init` 才会清理该目录。
卸载不会改动宿主驱动或 `udev` 规则。

56 上仍需在获授权的维护窗口完成以下验收：手动安装、启停、管理员网关访问、设备
缺失降级、App Center 的 `status` 能否以包用户经 `${TRIM_APPDEST}/app.sock` 请求健康接口、
升级时旧容器停止与调度静默的顺序、QDC507 只读 POC、配置与审计跨升级保留、默认卸载保留数据
及显式删除数据，以及拔插和重启后的稳定性。
以上完成前，不应把本机契约测试或基础只读核验表述为 FPK 已实机交付。

## 非飞牛参考

原生 `systemd`、Caddy、回环端口和 timer 模型仅保留为历史迁移参考，不能作为 fnOS
FPK 的安装、升级、卸载或调度步骤。非飞牛本地开发、WSL 和诊断模式的说明保留在
[README](../README.md) 与 [Web 控制台说明](web-control.md) 中；它们不是 56 的
生产部署入口。
