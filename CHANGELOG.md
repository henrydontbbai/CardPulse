# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

---

## [1.1.0] - 2026-07-04

### 新增

- 独立 PDU 编码器，覆盖 GSM 7-bit、扩展字符和 UCS2 编码
- GitHub Actions CI，包含 ShellCheck、PDU 回归测试、配置 schema 校验和 Docker 构建
- Docker 镜像构建支持，默认使用非 root 用户运行
- 本地测试入口 `tests/run.sh`

### 修复

- 修复 `set -e` 下短信发送失败时无法进入错误处理分支的问题
- 修复 PDU 发送长度计算，使用 TPDU 长度而不是完整 PDU 长度
- 修复配置解析依赖提示，明确要求 `python3-yaml`

### 安全

- 通知 URL 和 SMTP 凭据改用私有临时目录传递，降低进程参数和临时文件泄露风险
- 配置 dump 和通知输出对手机号、消息内容和凭据进行脱敏
- 状态文件写入后显式设置 `0600` 权限
- 运行时提醒配置文件权限过宽的问题

### 改进

- 串口访问增加 `flock` 互斥，避免并发实例争用设备
- 状态写入改为原子写入，历史记录追加加锁
- 安装脚本补充 `flock`、`timeout`、`python3-yaml` 等依赖，统一 systemd timer 随机延迟，并改为 systemd 优先、cron fallback 的调度策略

---

## [1.0.0] - 2026-07-04

### 新增

- AT 命令通信模块，支持 TEXT 和 PDU 模式
- 短信发送功能，自动重试机制
- 状态持久化，记录发送时间
- 智能调度，按天数间隔发送
- 自动检测串口设备
- 多渠道通知支持
  - Telegram
  - 微信（Server酱）
  - 企业微信
  - QQ（Qmsg）
  - 飞书
  - 钉钉
  - Bark
  - Email
- Systemd 定时任务集成
- 一键安装脚本
- 完善的日志记录
- 详细的部署文档

---

> 本文件格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。
