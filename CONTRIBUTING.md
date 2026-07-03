# 贡献指南

感谢你对 CardPulse 的关注！本指南将帮助你参与项目贡献。

## 如何贡献

### 报告问题

如果你发现了 bug 或有功能建议，请通过 [GitHub Issues](https://github.com/henrydontbbai/CardPulse/issues) 提交。

提交问题时请包含：

- 清晰的问题描述
- 复现步骤
- 环境信息（操作系统、模组型号等）
- 相关日志或错误信息

### 提交代码

1. **Fork 仓库**
2. **创建功能分支**
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. **进行修改**
4. **测试验证**
   ```bash
   # 确保脚本可执行
   chmod +x bin/cardpulse lib/*.sh
   
   # 测试基本功能
   cardpulse --help
   cardpulse --version
   ```
5. **提交更改**
   ```bash
   git commit -m "feat: 添加某功能"
   ```
6. **推送分支**
   ```bash
   git push origin feature/your-feature-name
   ```
7. **创建 Pull Request**

## 代码规范

### Git Commit 规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<type>: <description>

[optional body]
```

类型（type）：

- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档更新
- `style`: 代码格式调整
- `refactor`: 重构
- `test`: 测试相关
- `chore`: 构建/工具相关

示例：
```bash
git commit -m "feat: 添加新的通知渠道"
git commit -m "fix: 修复串口检测问题"
git commit -m "docs: 更新安装指南"
```

### Shell 脚本规范

- 使用 `#!/bin/bash` 开头
- 启用 `set -euo pipefail`
- 使用有意义的函数名和变量名
- 添加必要的注释
- 避免硬编码敏感信息

## 功能建议

如果你有新功能建议，请先通过 Issue 讨论，确认方向后再开始开发。

## 许可证

贡献代码即表示你同意将代码以 [MIT 许可证](LICENSE) 授权。

## 问题反馈

如有任何问题，请通过 GitHub Issues 联系。
