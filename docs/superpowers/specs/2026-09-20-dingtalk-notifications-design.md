# 钉钉通知渠道设计

日期: 2026-09-20
状态: 已与需求方确认

## 背景与目标

Flowdeck 现在只有一种「通知」:项目策略里的 `managers`(GitHub 用户名)。审查不通过、必需检查失败、任务以失败/不确定/缺配置收尾时,`worker.notify()` 在 PR 上评论 `@用户`,或给 Release 另开 Issue。没有钉钉,也不能按事件或按项目选择投递目标。

本设计用**群自定义机器人渠道**替换 GitHub @ 通知。管理员在集成配置里维护多条渠道,每条有独立 Webhook、加签 SEC、事件勾选和项目勾选。项目接入里的「通知人」改为钉钉手机号,匹配到的消息每次都 @。审查评论、检查状态、合并等 GitHub 行为不变,只去掉「通知管理人」这条评论/Issue。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 投递方式 | 仅钉钉群自定义机器人 Webhook;不做企业内部应用、不做单聊 |
| 配置模型 | 多条「通知渠道」,每条独立 Webhook / 加签 / 事件 / 项目 |
| 加签 | 必填 SEC;发送时按钉钉算法带 `timestamp` + `sign` |
| 与 GitHub 通知 | **替换**:删除 `notify()` 的 GitHub 评论和 Issue;不再保留双通道 |
| 「通知人」字段 | 仍存 `managers`,语义改为中国大陆手机号;每条钉钉都 `atMobiles` |
| 新项目 | 不会自动进入已有渠道,需在渠道里再勾选 |
| 落库 | 仅数据库(对齐模型接口);不写入 `projects.yml` 作为渠道来源 |
| 看板事件 | 只在看板列切换时计算:`待处理` / `进行中` / `需要关注` / `已完成` |
| 同一步去重 | 若已发送更具体的事件,同一批次不再发送 `board_column_change` |

## 第 1 节 · 总体架构

新增表 `dingtalk_channels`,管理方式和 `AgentConnection` 相同:管理员 CRUD、密钥 Fernet 加密、公开接口不回传密钥。

新增 `backend/dingtalk.py`:校验机器人 URL、加签、组装 markdown、POST。Worker 只调用投递函数,不实现 HMAC。

`worker.notify()`(及 GitHub 同步写库后的状态变化)改为 `dispatch_dingtalk(...)`:按「项目 + 事件」过滤启用中的渠道,逐条发送。演示项目(`record.demo`)不发。没有匹配渠道时静默,不再写「管理人尚未配置」。

看板列规则在 `backend/board.py` 与前端 `RecordsBoard.tsx` 的 `boardColumn` 保持同一套集合,用测试锁住。

## 第 2 节 · 数据模型与密钥

`dingtalk_channels`:

| 列 | 说明 |
|---|---|
| `id` | 32 位 hex,与其他表一致 |
| `name` | 唯一,1–120 字,去空白 |
| `enabled` | 默认 true |
| `webhook_ciphertext` | `seal(settings, "dingtalk:"+id, "webhook", url)` |
| `sign_secret_ciphertext` | `seal(settings, "dingtalk:"+id, "sign_secret", sec)` |
| `events` | JSON 字符串列表,见第 4 节;允许空(空则永不匹配) |
| `project_ids` | JSON 项目 ID 列表;允许空(空则永不匹配) |
| `created_at` / `updated_at` | UTC |

创建时 Webhook 与 SEC 都必填。更新时对应字段留空表示不改,不能清空。名称冲突返回 409。

Webhook 只接受:

- `https`
- 主机名 `oapi.dingtalk.com`
- 路径 `/robot/send`
- 查询串含非空 `access_token`
- 无 userinfo、无 fragment

其它地址 422。`project_redactions` 把每条渠道解出的 Webhook 与 SEC 纳入脱敏,Worker 日志不得打印查询串。

`managers`(项目策略):

- UI 文案:「钉钉通知人」,占位「手机号,逗号分隔,可空」
- 校验:`1[3-9]\d{9}`,去重保序
- 发送时只把通过该校验的号码写入 `atMobiles` 和正文 `@手机号`;库里遗留的 GitHub 用户名忽略,不阻断发群
- 下次在接入页保存时,非手机号会 422,必须改成手机号或清空

YAML `config/projects.example.yml` 注释改为手机号说明。渠道本身不进 YAML。

删除项目时,从所有渠道的 `project_ids` 去掉该 ID。保存渠道时,未知项目 ID 返回 422。

## 第 3 节 · 管理界面与 API

放在「集成配置」,交互对齐「模型接口」:

- 列表:名称、启用、事件数、项目数、测试、编辑、删除确认
- 表单:名称、Webhook、SEC、启用、事件多选、项目多选(非演示项目)
- 事件或项目都未勾选时仍可保存,表单提示「当前不会发出任何消息」
- 编辑不回填密钥;测试发送**不 @**、不写看板时间线

API(均需管理员写权限与 CSRF):

- `GET /api/admin/integrations` 增加 `dingtalk_channels`(公开字段 + `webhook_configured` / `sign_configured`)
- `POST /api/admin/dingtalk-channels` → 201
- `PUT /api/admin/dingtalk-channels/{id}`
- `DELETE /api/admin/dingtalk-channels/{id}`
- `POST /api/admin/dingtalk-channels/{id}/test` 走同一发送函数,正文为「Flowdeck 测试:渠道 {name}」

`GET /api/integrations` 增加 `dingtalk: { configured: 是否存在至少一条启用且密钥齐全的渠道 }`。密钥永不出现在任何响应里。

工作流说明里「发现问题通知管理人」改为「发现问题发钉钉」。时间线阶段名「通知管理人」改为「通知」。

## 第 4 节 · 事件、数据流与去重

事件名固定如下,未知值保存时 422:

| 事件 | 何时发出 |
|---|---|
| `review_needs_attention` | 审查结论不是 `pass`(需要修改或无法结论) |
| `review_passed` | 审查结论 `pass`(现有 `notify()` 没有这条,要补) |
| `checks_failed` | 已配置的必需检查中有 `failure` |
| `merged` | 记录变为 `merged`(Flowdeck 合并成功,或 GitHub 同步发现已合并) |
| `job_failed` | 任务以 `failed` / `uncertain` / `needs_configuration` 收尾 |
| `release_ready` | `kind=release` 的记录经 Worker `finish("success", ...)` 成功结束发版流水线 |
| `board_column_change` | 看板列变化,且同一批次没有更具体的事件 |

看板列(与 `RecordsBoard.tsx` 一致):

- **需要关注**: `failed`, `changes_requested`, `uncertain`, `needs_configuration`, `inconclusive`
- **已完成**: `merged`, `closed`, `deleted`, `success`(含 `details.deleted`)
- **进行中**: `status=running`,或 PR 且 `stage=review` 且 status 为 `queued`/`running`
- **待处理**: 其余(含 `review_passed`、`waiting`)

投递入口 `dispatch_dingtalk(settings, sessions, record, event, message, job=None)`:

1. 演示项目直接返回。
2. `record.repo` 解析到非演示 `Project`;找不到则返回。
3. 选出 `enabled`、含该 `project_id`、含该 `event`、Webhook 与 SEC 都能解密的渠道。
4. 对每个渠道生成 markdown(渠道名不作为标题噪音):标题为「{项目名} · {事件中文}」;正文含仓库、`#编号`、记录标题、说明、GitHub `record.url`。不编造看板深链。
5. 项目 `managers` 中合法手机号每次都写入 `at` 与正文(测试按钮除外)。不发送 `@all`。
6. 钉钉 `msgtype=markdown`。HTTP 200 且响应 `errcode==0` 视为成功;超时 10 秒。渠道之间串行,一条失败继续下一条。

同一批次(同一次 Worker 步骤、同一次 `finish`、同一次异常收尾、或同一次 GitHub upsert 写库)先收集要发的 `(event, message)`,若存在非 `board_column_change` 的事件,则丢掉列切换,避免审查不通过后再跟一条「进入需要关注」。

去重:

- 有 `job`:`job.result["dingtalk_sent"]` 为 `["{channel_id}:{event}", ...]`,已存在则跳过该渠道该事件。
- 无 `job`(同步路径):`record.details["dingtalk_sent"]` 同样结构;列切换另存 `record.details["dingtalk_board_column"]` 为上次已通知的列,列未变不发。

时间线 `stage=notification`:

- 有成功的渠道:一条「已通过钉钉通知」+ 成功渠道名列表
- 失败渠道另各记一条「钉钉通知未确认发送」,错误经现有 `sanitize`,不含 token/SEC
- 解密失败视为该渠道失败,不让 Worker 崩溃
- 没有匹配渠道:不写时间线

不在同一次 `dispatch_dingtalk` 内重试,避免打满机器人频率。GitHub 审查评论(`comment_once` 的 review 标记)保留。

加签(钉钉自定义机器人):

```
string_to_sign = "{毫秒时间戳}\n{SEC}"
sign = URL-encode(Base64(HMAC-SHA256(SEC, string_to_sign)))
POST {webhook}&timestamp={毫秒时间戳}&sign={sign}
```

## 第 5 节 · 测试

不引入新框架。用 httpx mock,不打真实钉钉。

必须覆盖:

- 固定 timestamp + SEC,断言 query 中的 `sign`
- 请求为 markdown,`atMobiles` 与正文 `@手机号` 一致;测试按钮 `at` 为空
- 项目不匹配、事件未勾、渠道停用、演示项目 → 无 HTTP
- 同一 job 同一渠道同一事件第二次不请求;列未变不发;同批已有具体事件则不发列切换
- 审查不通过不再 GitHub `comment_once` 通知,也不再为 Release 建「发布需处理」Issue;不再出现「管理人尚未配置」
- 创建缺 SEC 或 Webhook 非钉钉机器人地址 → 422;更新密钥留空则旧值保留;`managers` 非 11 位手机号 → 422
- 删除项目后渠道 `project_ids` 去掉该 ID
- `board.py` 与前端四列集合对照,防止漂移
- 异常、API 响应、日志中不含 Webhook 查询串和 SEC

前端不单开 E2E;集成页与接入页文案随现有页面改动即可。

## 非目标

- 企业内部应用、单聊、Slack、邮件
- 渠道级独立 @ 名单(第一版只用项目 `managers`)
- 钉钉失败自动连打、死信队列
- 把渠道写入 YAML 或环境变量作为主配置
- 为消息编造 Flowdeck 记录深链
