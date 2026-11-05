# 使用与支持

- 启动与部署：[README](README.md)。
- 开发环境与测试：[贡献指南](CONTRIBUTING.md)。
- 事件处理、任务与存储结构：[架构说明](docs/ARCHITECTURE.md)。
- Bug 和功能建议：在本仓库的 Issues 中选择相应模板。
- 安全问题：[私密报告渠道](SECURITY.md)，不要公开漏洞细节。

提问时请提供版本或提交号、系统、Python / Node.js / pnpm 版本、启动方式、复现步骤、脱敏后的报错，以及 `/api/health` 的结果。问题涉及界面时可附脱敏截图。

常见排查：

| 现象 | 检查项 |
| --- | --- |
| 页面返回 503 / Frontend is not built | 在项目根目录执行 `pnpm --dir frontend install --frozen-lockfile` 和 `pnpm --dir frontend build` |
| HTTP 本地开发登录后仍显示未登录 | 仅在本地 HTTP 环境设置 `SESSION_SECURE=false`，重新启动并登录 |
| GitHub Webhook 无法到达 | 检查公网 HTTPS、反向代理、Webhook URL 和 Secret；管理页面可访问不代表 GitHub 能访问本机 |
| 项目未激活 | 查看接入详情中的 API 验证、有效 ping、Worker 目录检查和公网入口配置 |
| 重启后 Token 无法解密 | 确认恢复的是同一 `SECRETS_ENCRYPTION_KEY`，不要重新生成覆盖旧密钥 |
| Agent 无法启动 | 检查 Copilot runtime、模型接口地址、模型名、API Key 与网络；本地安装后需下载 runtime |

不要附上完整 `.env`、数据库、生产工作区或备份。社区按可用时间提供帮助，目前没有服务等级或固定响应时间承诺。
