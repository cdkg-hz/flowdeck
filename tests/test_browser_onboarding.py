"""Real frontend and API on a disposable localhost server; no GitHub account calls."""
import asyncio
import os
import socket
import threading
from pathlib import Path
import pytest
import httpx
import uvicorn
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, expect
from sqlalchemy import select
from backend.auth import set_admin
from backend.config import Settings
from backend.credentials import unseal
from backend.db import ProjectConnection, database, initialize_schema
from backend.main import create_app
from backend.projects import verify_connections
from tests.test_ingestion import signed

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 for Chromium acceptance")


@pytest.fixture
def onboarding_site(tmp_path, monkeypatch):
    # Exercise the real local UI/API without registering a hook on GitHub.
    monkeypatch.setattr("backend.github.ensure_repository_webhook", lambda **kwargs: "browser-hook-1")
    storage = tmp_path / "projects"
    storage.mkdir()
    settings = Settings(_env_file=None, app_mode="live", database_url=f"sqlite:///{(tmp_path / 'browser.db').as_posix()}",
        data_root=tmp_path / "data", storage_roots=[str(storage)], config_path=str(tmp_path / "empty.yml"),
        secrets_encryption_key=Fernet.generate_key().decode(), session_secure=False,
        webhook_public_base_url="https://hooks.example.test", frontend_dist=Path("frontend/dist").resolve())
    engine, sessions = database(settings.database_url)
    initialize_schema(engine)
    with sessions() as db:
        set_admin(db, "admin", "local-browser-test-password")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    yield settings, sessions, f"http://127.0.0.1:{port}", server, tmp_path
    server.should_exit = True
    thread.join(timeout=8)
    sock.close()
    engine.dispose()


async def test_browser_create_bind_verify_and_mobile(onboarding_site):
    settings, sessions, url, server, output = onboarding_site
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1100})
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_role("button", name="管理员登录", exact=True).click()
        dialog = page.get_by_role("dialog")
        await dialog.get_by_label("用户名").fill("admin")
        await dialog.get_by_label("密码", exact=True).fill("local-browser-test-password")
        await dialog.get_by_role("button", name="登录", exact=True).click()
        await expect(dialog).to_have_count(0)
        await page.get_by_role("button", name="创建项目", exact=True).first.click()
        await dialog.get_by_label("项目名称", exact=False).fill("Android 客户端")
        await dialog.get_by_label("GitHub 仓库", exact=False).fill("acme/browser-app")
        await dialog.get_by_label("项目存放目录", exact=False).fill(str(Path(settings.storage_roots[0]) / "android-app"))
        await dialog.get_by_role("button", name="下一步", exact=True).click()
        await expect(dialog.get_by_role("checkbox")).to_have_count(8)
        assert all([await checkbox.is_checked() for checkbox in await dialog.get_by_role("checkbox").all()])
        await dialog.get_by_label("GitHub PAT", exact=False).fill("local-test-pat-for-browser")
        await dialog.get_by_role("button", name="下一步", exact=True).click()
        await expect(dialog.get_by_text("镜像与安卓凭据都是选填")).to_be_visible()
        await expect(dialog.get_by_role("group", name="阿里云镜像 选填", exact=True)).to_be_visible()
        await expect(dialog.get_by_role("group", name="安卓签名 选填", exact=True)).to_be_visible()
        await dialog.get_by_role("button", name="跳过并创建", exact=True).click()
        await expect(dialog.get_by_role("heading", name="接入准备度")).to_be_visible()
        await dialog.get_by_role("button", name="显示 Secret", exact=True).click()
        await expect(dialog.get_by_role("button", name="隐藏 Secret", exact=True)).to_be_visible()
        await dialog.get_by_role("button", name="隐藏 Secret", exact=True).click()
        with sessions() as db:
            connection = db.scalar(select(ProjectConnection))
            secret = unseal(settings, connection.project_id, "webhook", connection.secret_ciphertext)
            webhook_id = connection.webhook_id
        async def verifier(*args):
            return "456"
        await verify_connections(settings, sessions, verifier=verifier)
        raw, headers = signed({"repository": {"full_name": "acme/browser-app", "id": 456}}, secret, "browser-ping", "ping")
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{url}/api/webhooks/github/{webhook_id}", content=raw, headers=headers)
            assert response.status_code == 202
        await expect(dialog.get_by_text("已就绪", exact=True)).to_have_count(3, timeout=10000)
        await expect(dialog.get_by_text("已激活", exact=True)).to_be_visible()
        await page.screenshot(path=str(output / "onboarding-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await expect(dialog).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert await dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        await page.screenshot(path=str(output / "onboarding-mobile.png"), full_page=True)
        await dialog.get_by_role("button", name="关闭", exact=True).click()
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.get_by_role("button", name="退出管理", exact=True).click()
        await expect(page.get_by_role("button", name="管理员登录", exact=True)).to_be_visible()
        assert not errors
        await browser.close()
