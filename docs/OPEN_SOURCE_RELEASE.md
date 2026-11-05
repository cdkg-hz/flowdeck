# 开源与发布清单

本清单供仓库维护者使用。当前目录若来自源码压缩包，可能没有 `.git` 和远程地址；先准备文件，再创建自己的 GitHub 仓库。本文不假设仓库所有者、远程地址或私密联系邮箱。

## 首次公开前

- [ ] 确认有权公开项目的代码、文档、素材和附带技能包；检查所有参与者及第三方内容的授权。项目采用根目录的 MIT License，依赖和外部运行时保留各自许可。
- [ ] 检查将要上传的全部文件，排除 `.env`、真实项目配置、Token、主密钥、手机号、数据库、日志、缓存、工作区和备份。`.gitignore` 只防止新文件被跟踪，不能移除历史中已有的内容。
- [ ] 如保留已有 Git 历史，用密钥扫描工具检查完整历史；本次目录检查不能证明历史没有泄露。发现凭据先撤销或轮换，再清理历史。
- [ ] 在干净环境按 README 验证安装、演示启动和测试。执行前端构建与浏览器验收；Linux 上检查 Docker 构建和 Compose 启动。
- [ ] 核对 `config/projects.yml` 保持空配置，生产配置只保存在部署环境；检查 Issue、截图与示例中没有私人信息。

## GitHub 仓库设置

- [ ] 创建仓库并上传源码。若本地没有 Git 历史，可先执行 `git init -b main`，再用 `git add --dry-run .` 审阅待提交清单；检查后再暂存、提交并关联自己的远程仓库。不要上传虚拟环境或 `node_modules`。
- [ ] 设置仓库描述，例如「自托管 GitHub 研发看板，集成代码审查、任务追踪与发布日志管理」。可选 Topics：`github`、`webhook`、`code-review`、`release-management`、`self-hosted`、`fastapi`、`react`。
- [ ] 开启 Issues；确认许可证、贡献指南、行为准则和 Issue / PR 模板被正确识别。
- [ ] 在 **Settings → Security and quality → Advanced Security** 中启用适用的 Dependabot alerts、Secret scanning 和 Push protection；功能名称与可用性以仓库设置为准。
- [ ] 在 **Settings → Advanced Security → Private vulnerability reporting** 启用私密漏洞报告，确认 Security 页出现「Report a vulnerability」，与 [SECURITY.md](../SECURITY.md) 一致。
- [ ] 在 **Settings → Actions → General** 使用只读默认工作流权限。当前 CI 无需仓库 Secrets，也不会发布镜像、修改仓库或部署。首次外部贡献运行可能需要维护者批准。
- [ ] 等首次 CI 运行后，为默认分支建立规则，要求 PR 审查和 `Backend tests`、`Frontend & browser`、`Docker build` 三项检查通过，并限制强制推送与删除。自定义默认分支名时同步调整 `.github/workflows/ci.yml` 的 push 分支过滤。
- [ ] 确认许可证版权行反映实际权利人，仓库中的通用「Flowdeck contributors」可按真实归属修改。不要虚构维护邮箱、组织或支持承诺。

## 发布版本

1. 整理 `CHANGELOG.md` 的 Unreleased 条目，记录实际版本和发布日期，说明配置变更、数据兼容性与恢复步骤。
2. 同步 `pyproject.toml`、`frontend/package.json` 和 `backend/main.py` 中的版本。若前端版本变化，用固定版本 pnpm 更新并提交锁文件。
3. 按[贡献指南](../CONTRIBUTING.md)完成测试；使用独立数据目录演练升级并备份生产数据和主密钥。
4. 合并后创建对应标签（例如 `v0.1.0`），在 GitHub 创建 Release 并写清变更和限制。早期版本可标记为 Pre-release。
5. 当前 CI 只做验证。若以后分发容器或其他制品，再单独配置受保护的发布流程，并在制品内保留许可证及依赖要求的声明。

## 官方参考

- [GitHub：许可仓库](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
- [GitHub：启用私密漏洞报告](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
- [GitHub：管理规则集](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- CI 动作版本依据官方用法：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[setup-node](https://github.com/actions/setup-node)。
