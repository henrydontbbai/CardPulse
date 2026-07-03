# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

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
