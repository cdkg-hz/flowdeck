# 参与 Flowdeck

欢迎提交问题、文档改进、测试和代码。参与交流前请阅读[行为准则](CODE_OF_CONDUCT.md)；疑似漏洞请按[安全政策](SECURITY.md)私下报告。

## 提交问题或建议

- Bug：使用仓库 Issues 中的「Bug 报告」，提供版本或提交号、部署方式、复现步骤、预期与实际结果。
- 新功能：使用「功能建议」，描述使用场景。较大的改动请先通过 Issue 确认范围，避免重复开发。
- 使用问题：先查看 [README](README.md) 和[支持说明](SUPPORT.md)。

日志、截图和示例配置请去掉 GitHub Token、模型 API Key、Webhook Secret、Cookie、手机号、私有仓库内容和服务器地址。不要上传 `.env`、数据库或生产备份。

## 开发环境

使用 Python 3.12、Node.js 24、pnpm 11.19.0 和 Git，与 Docker 构建和 CI 的主版本保持一致。以下命令在项目根目录运行。

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt -c requirements.lock
npm install --global pnpm@11.19.0
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

Linux / macOS：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt -c requirements.lock
npm install --global pnpm@11.19.0
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

若 PowerShell 不允许激活脚本，直接使用 `.venv\Scripts\python` 执行 Python 命令即可。

未创建 `.env` 时默认使用 demo 数据，无需 GitHub 或模型凭据。若本地已有 `.env`，请确保演示使用独立的数据库；详见 README 的本地启动章节。开发前端可另开终端运行 `pnpm --dir frontend dev --port 5173`，访问 `http://127.0.0.1:5173`，API 由 Vite 代理到 8765。

只有运行真实 Agent 任务时，才需执行 `python -m copilot download-runtime` 并配置模型接口。常规测试使用替身，不需要下载 Copilot runtime。

## 提交前验证

在已激活的虚拟环境中执行：

```sh
python -m pip check
python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider
pnpm --dir frontend build
```

浏览器验收需要构建前端并安装 Chromium。测试启动临时本地服务，使用临时数据库和模拟 GitHub 验证，不使用真实账户：

```sh
python -m playwright install chromium
# Linux 首次安装可改用：python -m playwright install --with-deps chromium
```

PowerShell：

```powershell
$env:RUN_BROWSER_TESTS = "1"
python -m pytest tests/test_browser_onboarding.py --basetemp=./.pytest-browser -p no:cacheprovider
Remove-Item Env:RUN_BROWSER_TESTS
```

Linux / macOS：

```sh
RUN_BROWSER_TESTS=1 python -m pytest tests/test_browser_onboarding.py --basetemp=./.pytest-browser -p no:cacheprovider
```

普通测试默认跳过浏览器验收；CI 的 `Frontend & browser` 检查会实际运行它。改动 Docker 或部署配置时，还应执行 `docker build -t flowdeck:check .`，并按 README 用隔离的数据目录检查 Compose 启动。

## Pull Request

1. Fork 仓库，从默认分支创建工作分支，一次 PR 解决一个清晰的问题。
2. 沿用现有 Python / TypeScript 风格。行为变化应有能复现问题的测试；纯文档改动检查链接和命令即可。
3. 如配置、部署流程、数据格式或用户可见行为变化，同时更新文档及 `CHANGELOG.md` 的 Unreleased 部分。涉及数据兼容性时写明升级与恢复方法。
4. 填写 PR 模板，说明改了什么、为什么、如何验证，关联 Issue。界面改动附脱敏截图。
5. 确认 CI 通过，按评审意见更新。不要在贡献分支中加入生产凭据或自动发布步骤。

## 依赖与许可证

- Python 的声明位于 `requirements.txt` / `requirements-dev.txt`，固定版本位于 `requirements.lock`；Docker 直接安装锁文件。变更直接依赖时同步检查锁文件，在干净虚拟环境安装并验证。CI 同时使用声明与锁文件约束。
- 前端使用 pnpm，提交 `frontend/pnpm-lock.yaml`，不要额外生成 npm / Yarn 锁文件。pnpm 版本记录在 `frontend/package.json`，升级时同步更新 Dockerfile 和开发文档。
- 保留第三方代码和依赖的版权、许可证与必要声明。提交代码应为你有权贡献的内容，按本项目 [MIT License](LICENSE) 提供；第三方内容仍遵循其原许可证。
