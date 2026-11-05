---
name: releasing-version-docs
description: Orchestrates Beluga/白鲸智存 new-version release documentation for a Flowdeck Codex agent. Use when generating 发版文档, 产品功能介绍与使用手册, 现状技术实现与架构说明书, 更新日志, changelog.html, or 版本详情 HTML.
---

# 发版统御（Flowdeck）

你是唯一执行者。不要派子任务、不要问用户、不要用终端或 git。对应规范不抄进本文件；写对应产出前先读 payload.skills 里的同名全文。

必读规范：

- [角色设定和任务目标](角色设定和任务目标.md) → 产品手册 + 技术说明书
- [生成新版本的更新日志](生成新版本的更新日志.md) → `更新日志_YYYY-MM-DD.md`
- [changelog-convention](changelog-convention.md) → `changelog.html` + `{version}.html`
- [写作要点](dispatch-prompts.md) → 各份文档的读者边界与产出路径

## 纪律

- 输入以 payload 为准：`release.tag`、`release.docs_date`、`release.baseline_docs`；若存在 `.flowdeck-changes.diff` / `.flowdeck-commits.txt` 必须先读
- 不要派 Task/子 Agent；按 P0→P4 顺序自己写完
- 不得跳过手册和说明书，直接用 commit 列表写更新日志或 HTML
- 不得修改历史版本详情页
- 对外 HTML 禁止代码、接口、源码路径、长操作步骤
- 不改 `apps/` 产品代码；不改写三份规范 md
- 不要自己执行 git。写完后把相对路径列入结构化结果的 `release_docs.paths`，并设 `push_requested=true`，由 Flowdeck 提交推送
- 缺旧版基线时不要停下来提问：能写的写，证据不足标「文档无法确认」

## 进度清单

```
- [ ] P0 输入门
- [ ] P1a 产品手册
- [ ] P1b 技术说明书
- [ ] P2 更新日志
- [ ] P3 changelog.html + 版本详情 HTML
- [ ] P4 文件与链接自检
```

## P0 输入门

| 项 | 规则 |
|---|---|
| 发布标识 | `{versionName}-{versionCode}`。`versionName` 用 `payload.release.tag`（去掉意外空白）。`versionCode` 只从当前工作目录 Android Gradle 的 `defaultConfig.versionCode` 读取；读不到就省略，发布标识只用 versionName |
| 文档日期 | `payload.release.docs_date`（`YYYY-MM-DD`）。Markdown 文件名用这个日期，不要另起“生成当天” |
| 旧版基线 | `payload.release.baseline_docs`。若为空，再在 `docs/updata_log/` 里找日期最新的一对《产品功能介绍与使用手册》《现状技术实现与架构说明书》。仍然没有则后续对比写「文档无法确认」 |
| 比较材料 | `.flowdeck-changes.diff` 的范围是上一版文档日期到本 tag，不是上一个 GitHub Release。先读差异和旧文档，再按需打开其中的路径。不要扫描 `node_modules`、`.git` 或整仓盲走 |
| 输出目录 | `docs/updata_log/` |
| HTML 日期 | 与文档日期相同 |

## P1 两份文档（自己顺序写）

先读角色设定全文。不要普查四端代码，不要从零重写全书。证据以旧版文档和 `.flowdeck-changes.diff` 为准；只在需要核对 diff 中的路径时打开文件。禁止脑补。

| 文档 | 产出 |
|---|---|
| 产品手册 | `docs/updata_log/产品功能介绍与使用手册_YYYY-MM-DD.md` |
| 技术说明书 | `docs/updata_log/现状技术实现与架构说明书_YYYY-MM-DD.md` |

**P1a 产品手册：** 以 `payload.release.baseline_docs` 中的上一版产品手册为底稿，复制为新日期文件后，只修订 diff 能证明的用户可感知变化。未改模块整段沿用。不要从路由/页面重新梳理四端，不要为未变化场景重写操作步骤。

**P1b 技术说明书：** 只写约一页摘要，三块：本版实现变化、未闭环、一句范围。不要目录树、接口、函数名、源码路径、流程链路、模型、部署或接手说明。不要为写摘要去打开 diff 未涉及的文件。实现状态仅用于 diff 能判断的模块：已实现 / 部分实现 / 仅入口/占位 / 疑似废弃/遗留 / 未发现实现。

无旧版基线时：能写的写，证据不足标「文档无法确认」，不要整仓补写全书。

**P1 验收（两份都齐再写 P2）：**

- 两文件存在，文件名日期与 P0 一致
- 产品手册无代码目录、文件路径、函数名、组件名、接口路径、数据库表、技术栈细节
- 技术说明书是摘要：无代码路径、无十二章结构；实现状态只出现在本版变化与未闭环中
- 不确定处标明“不确定”，未把需求文档当成已实现
- 未对未变化章节做无依据改写

任一明显违反读者边界 → 先改这份文件，再进入 P2。

## P2 更新日志

必读 `生成新版本的更新日志.md`。输入：旧手册、新手册、新说明书摘要、`.flowdeck-changes.diff`（基线缺失的那侧标无法确认）。产出：`docs/updata_log/更新日志_YYYY-MM-DD.md`。

硬规则：禁止把“新文档多写了”当成“产品新增”；禁止只用 git log 或 commits.txt 当正文来源。diff 可作为辅助证据。

**P2 验收：** 文件存在；按端归组写出用户可感知的新增/增强/修复/移除，以及注意事项与未闭环；无证据结论标“文档无法确认”。不要写产品形态专章、流程专章、技术架构专章或产品↔技术对照大表。

## P3 对外 HTML

必读 `changelog-convention.md`。复制当前最新详情页的结构与样式，不要另起 CSS。

产出：

1. 新建 `docs/updata_log/{versionName}-{versionCode}.html`（无 versionCode 时用 `{versionName}.html`）
2. 更新 `docs/updata_log/changelog.html`：新版本插到列表顶部、版本计数 +1、把「最新」徽章移到新版本

内容来源：产品手册已支持能力 → 技术说明书/代码核对 → 更新日志增量。未实现、仅入口、有限制的不写进「新功能」。

**P3 验收：** 文件名、页面标题、列表 `Version:` 文案三者完全一致；旧详情页未改。

## P4 文件与链接自检

不要打开浏览器。

- 五个文件都在（两份手册、更新日志、新详情 HTML、`changelog.html`），命名与版本号、日期一致
- `changelog.html` 新版本链到对应详情页；详情页「返回更新日志」指向 `changelog.html`（相对路径）
- 空分类不出现；历史详情页未被改动

写完后：`release_docs.paths` 列出上述相对路径，`push_requested=true`。

## 红旗 — 停下来改做法

- 派子任务、问用户、执行 git / shell
- 先用 commit 列表写一版更新日志
- 整仓盲走，或从零重写产品手册
- 把说明书写成架构全书或带上代码路径
- P1 还没齐就搭 HTML
- “新手册里出现了，所以是本版本新增”
- 改历史版本 HTML、在对外页写接口或源码路径
- 自己 git commit / git push
