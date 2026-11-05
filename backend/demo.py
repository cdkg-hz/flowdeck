from datetime import timedelta
from sqlalchemy import select
from backend.db import Job, Project, Record, Timeline, utcnow


def seed_demo(db):
    if db.scalar(select(Project).limit(1)):
        return
    projects = [("example/mobile-app", "Mobile App", "Android 客户端 · 用户体验与版本发布"), ("example/core-api", "Core API", "业务服务 · 数据与接口"), ("example/admin-console", "Admin Console", "运营后台 · 内容与版本管理")]
    for repo, name, description in projects:
        db.add(Project(repo=repo, name=name, description=description, demo=True, last_synced_at=utcnow()))
    items = [
        ("example/mobile-app", "pr", 128, "修复弱网环境下登录请求重复提交", "普通", "changes_requested", "review", ["Lin"], "12 分钟前", {"head_sha": "a3f819d" + "0" * 33, "base_sha": "b" * 40, "head_ref": "fix/login-retry", "base_ref": "main", "additions": 84, "deletions": 23, "changed_files": 4, "review": {"verdict": "changes_requested", "summary": "发现重试期间重复提交的问题，需要补充请求互斥。", "findings": [{"file": "app/src/main/LoginRepository.kt", "line": 86, "severity": "high", "title": "重试过程中仍可重复创建登录请求", "explanation": "重试定时器尚未结束时，按钮会再次变为可点击，导致两个认证请求同时执行。", "suggestion": "在重试结束前保持请求互斥，并补充快速重复点击的测试。"}]}, "checks": [{"name": "Android CI", "state": "success"}, {"name": "Unit tests", "state": "success"}]}),
        ("example/mobile-app", "release", 42, "Android 2.8.0", None, "running", "notes", ["Chen"], "刚刚", {"tag": "v2.8.0", "version_code": 20800, "version_name": "2.8.0", "package_name": "com.example.mobile", "apk_name": "mobile-v2.8.0.apk", "public_notes": "新增离线收藏功能。\n优化首页加载速度。\n修复弱网下图片加载异常。", "technical_notes": "新增本地收藏持久化与同步队列；调整首页缓存策略。示例内容。", "steps": {"release": "success", "workspace": "success", "notes": "running"}}),
        ("example/core-api", "pr", 87, "增加订单状态变更的幂等校验", "普通", "running", "review", ["Zhou"], "5 分钟前", {"head_sha": "f41c280" + "0" * 33, "base_sha": "c" * 40, "head_ref": "feat/order-idempotency", "base_ref": "main", "additions": 136, "deletions": 18, "changed_files": 6}),
        ("example/admin-console", "issue", 56, "版本管理页支持按渠道筛选", "普通", "waiting", "issue", ["Wu"], "28 分钟前", {}),
        ("example/mobile-app", "issue", 131, "部分设备从后台恢复时出现白屏", "紧急", "waiting", "issue", [], "42 分钟前", {}),
        ("example/core-api", "release", 38, "Core API 1.16.2", None, "failed", "notes", ["Zhou"], "1 小时前", {"tag": "v1.16.2", "version_name": "1.16.2", "error": "版本记录缺少可追溯依据，当前记录保留为分析失败。", "steps": {"release": "success", "notes": "failed"}}),
        ("example/admin-console", "pr", 53, "优化版本详情页的字段校验提示", "普通", "merged", "merge", ["Wu"], "2 小时前", {"head_sha": "d" * 40, "base_sha": "e" * 40, "head_ref": "fix/version-validation", "base_ref": "main", "additions": 32, "deletions": 12, "changed_files": 3, "review": {"verdict": "pass", "summary": "未发现需要修改的问题。", "findings": []}, "checks": [{"name": "Web CI", "state": "success"}]}),
        ("example/admin-console", "issue", 48, "旧版筛选条件不再需要", "普通", "closed", "closed", ["Wu"], "3 小时前", {"github_state": "closed", "deleted": False}),
        ("example/mobile-app", "issue", 118, "启动闪退已无法复现", "普通", "closed", "closed", ["Lin"], "昨天", {"github_state": "deleted", "deleted": True}),
        ("example/mobile-app", "release", 39, "Android 2.7.3", None, "success", "verify", ["Chen"], "昨天", {"tag": "v2.7.3", "version_code": 20703, "version_name": "2.7.3", "steps": {x: "success" for x in ["release", "workspace", "notes", "verify"]}}),
    ]
    for i, (repo, kind, number, title, level, status, stage, owners, _, details) in enumerate(items):
        time = utcnow() - timedelta(minutes=i * 17 + 2)
        labels = [level] if level else []
        if kind == "issue" and number in {118, 131}:
            labels.append("bug")
        record = Record(repo=repo, kind=kind, external_id=str(number), number=number if kind != "release" else None, title=title, body="这是一条演示记录，用于查看工作流与操作结果。", url="", priority=level, status=status, stage=stage, assignees=owners, labels=labels, details={**details, "bug": "bug" in labels}, created_at=time - timedelta(hours=4), updated_at=time, demo=True)
        db.add(record)
        db.flush()
        stages = [("issue", "success", "需求已分派"), ("pr", "success", "提交 Pull Request"), ("checks", "success", "必需检查已通过"), (stage, status, "Agent 审查发现问题" if status == "changes_requested" else "当前执行阶段")]
        if kind == "release":
            titles = {"release": "收到 Release 事件", "workspace": "准备版本代码", "notes": "生成更新记录", "build": "构建 Docker 镜像", "deploy": "更新后端服务", "verify": "验证上线结果"}
            stages = [(s, state, titles[s]) for s, state in details.get("steps", {}).items() if state != "waiting"]
        elif kind == "issue":
            stages = [("issue", "waiting", "Issue 已创建，等待处理")]
            if status == "closed":
                stages.append(("closed", "closed", "GitHub 已删除" if details.get("deleted") else "Issue 已关闭"))
        for j, (step, state, title_) in enumerate(stages):
            db.add(Timeline(record_id=record.id, stage=step, status=state, title=title_, actor="Agent" if step in {"review", "notes"} else "GitHub" if step in {"issue", "pr", "release", "closed"} else "执行服务", message="演示事件 · 不代表实际项目执行", occurred_at=time - timedelta(minutes=(len(stages) - j) * 4)))
        if kind != "issue":
            db.add(Job(record_id=record.id, key=f"demo:{kind}:{repo}:{number}", kind="review" if kind == "pr" else "release", status=status if status in {"running", "failed"} else "completed", step=stage, payload={}, result={"demo": True}, updated_at=time))
    db.commit()
