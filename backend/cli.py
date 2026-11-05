from __future__ import annotations

import argparse
import asyncio
import json
import getpass
from pathlib import Path
import shutil
from datetime import timedelta
from sqlalchemy import select
from backend.config import get_settings
from backend.db import InboxEvent, Job, Project, ProjectConnection, Record, database, initialize_schema, utcnow
from backend.worker import Worker
from backend.auth import set_admin
from backend.projects import public_status
from backend.workspace import Workspaces


def cleanup_tasks(settings, sessions, older_than_days):
    if older_than_days < 1:
        raise ValueError("保留期限至少为 1 天")
    manager = Workspaces(settings, sessions=sessions)
    removed = 0
    with sessions() as db:
        jobs = list(db.scalars(select(Job).where(Job.status.in_(["completed", "superseded"]), Job.updated_at < utcnow() - timedelta(days=older_than_days))))
        for job in jobs:
            target = manager.task_dir(job.id)
            if not target.exists():
                continue
            resolved = target.resolve()
            if target.absolute() != resolved or target.is_symlink() or getattr(target, "is_junction", lambda: False)() or resolved.name != job.id or resolved.parent.name != "workspaces":
                raise ValueError("拒绝清理未验证的任务目录")
            manifest = resolved / "task.json"
            if manifest.is_symlink() or not manifest.is_file():
                continue
            value = json.loads(manifest.read_text(encoding="utf-8"))
            record = db.get(Record, job.record_id)
            if value.get("repo", "").lower() != record.repo.lower():
                raise ValueError("任务目录与仓库不一致，拒绝清理")
            shutil.rmtree(resolved)
            removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description="Flowdeck 服务端管理工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="验证配置，不连接外部服务")
    commands.add_parser("sync", help="同步 GitHub 记录")
    commands.add_parser("generate-key", help="生成凭据加密主密钥；请保存到服务端环境配置")
    admin = commands.add_parser("admin-init", help="初始化或重置唯一管理员账号，并注销旧会话")
    admin.add_argument("--username", default="admin")
    commands.add_parser("events", help="查看未处理及失败事件")
    event_retry = commands.add_parser("retry-event", help="重新处理指定异常事件")
    event_retry.add_argument("event_id")
    retry = commands.add_parser("retry", help="将指定失败任务重新排队")
    retry.add_argument("job_id")
    retry.add_argument("--new-analysis", action="store_true", help="明确重新发起 Agent 分析")
    retry.add_argument("--acknowledge-uncertain", action="store_true", help="已核对外部状态，允许重新执行结果未知的发布/部署步骤")
    cleanup = commands.add_parser("cleanup", help="清理已完成任务的临时代码副本，保留数据库与发布产物")
    cleanup.add_argument("--older-than-days", type=int, default=14)
    args = parser.parse_args()
    settings = get_settings()
    settings.project_config()  # Validate server automation policies before any operation.
    if args.command == "generate-key":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return
    if args.command == "sync":
        async def run():
            worker = Worker(settings)
            await worker.initialize()
            try:
                await worker.sync()
            finally:
                await worker.github.close()
                await worker.agent.close()
        asyncio.run(run())
        return
    engine, Session = database(settings.database_url)
    initialize_schema(engine)
    with Session() as db:
        if args.command == "admin-init":
            password = getpass.getpass("管理员密码（至少 12 个字符）：")
            if password != getpass.getpass("再次输入密码："):
                raise SystemExit("两次密码不一致")
            set_admin(db, args.username, password)
            print("管理员已保存，旧会话已注销。")
            return
        if args.command == "validate":
            from backend.credentials import cipher
            from backend.storage import storage_roots
            if settings.secrets_encryption_key:
                cipher(settings)
            print(json.dumps({"mode": settings.app_mode, "projects": [{"repo": p.repo, **public_status(db.get(ProjectConnection, p.id), p.demo)} for p in db.scalars(select(Project))], "storage_roots": [str(p) for p in storage_roots(settings)], "encryption_configured": bool(settings.secrets_encryption_key), "public_webhook_configured": bool(settings.webhook_public_base_url)}, ensure_ascii=False, indent=2))
            return
        if args.command == "events":
            for event in db.scalars(select(InboxEvent).where(InboxEvent.error.is_not(None) | InboxEvent.processed_at.is_(None)).order_by(InboxEvent.received_at.desc()).limit(50)):
                print(json.dumps({"id": event.id, "type": event.event_type, "attempt": event.attempt, "error": event.error}, ensure_ascii=False))
            return
        if args.command == "retry-event":
            event = db.get(InboxEvent, args.event_id)
            if not event or not event.error:
                raise SystemExit("只能重新处理存在的异常事件")
            event.processed_at, event.error, event.available_at = None, None, utcnow()
            db.commit()
            print("事件已重新排队：" + event.id)
            return
        if args.command == "retry":
            from backend.service import retry_job
            try:
                job = retry_job(db, db.get(Job, args.job_id), new_analysis=args.new_analysis, acknowledge_uncertain=args.acknowledge_uncertain)
            except LookupError as exc:
                raise SystemExit(str(exc)) from exc
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            db.commit()
            print("任务已重新排队：" + job.id)
        else:
            removed = cleanup_tasks(settings, Session, args.older_than_days)
            print(f"已清理 {removed} 个完成任务的代码副本；仓库缓存、数据库与产物保留。")


if __name__ == "__main__":
    main()
