from __future__ import annotations

import httpx
import pytest

from backend.github import ensure_repository_webhook


def _client(monkeypatch, *, hooks=None, post_status=201, patch_status=200):
    recorded = []
    hooks = list(hooks or [])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            recorded.append({"method": "GET", "url": str(url), "params": params})
            request = httpx.Request("GET", url)
            return httpx.Response(200, json=hooks, request=request)

        def post(self, url, headers=None, json=None):
            recorded.append({"method": "POST", "url": str(url), "json": json})
            request = httpx.Request("POST", url)
            if post_status >= 400:
                return httpx.Response(post_status, json={"message": "denied"}, request=request)
            return httpx.Response(post_status, json={"id": 55}, request=request)

        def patch(self, url, headers=None, json=None):
            recorded.append({"method": "PATCH", "url": str(url), "json": json})
            request = httpx.Request("PATCH", url)
            if patch_status >= 400:
                return httpx.Response(patch_status, json={"message": "denied"}, request=request)
            return httpx.Response(patch_status, json={"id": 12}, request=request)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorded


def test_ensure_repository_webhook_creates_hook(monkeypatch):
    recorded = _client(monkeypatch)
    hook_id = ensure_repository_webhook(
        "https://api.github.com",
        "github_pat_test",
        "acme/mobile",
        "https://hooks.example.test/api/webhooks/github/abcdef",
        "webhook-secret",
        ["issues", "release", "workflow_run"],
    )
    assert hook_id == "55"
    created = next(item for item in recorded if item["method"] == "POST")
    assert created["url"].endswith("/repos/acme/mobile/hooks")
    assert created["json"]["active"] is True
    assert created["json"]["events"] == ["issues", "release", "workflow_run"]
    assert created["json"]["config"] == {
        "url": "https://hooks.example.test/api/webhooks/github/abcdef",
        "content_type": "json",
        "secret": "webhook-secret",
        "insecure_ssl": "0",
    }


def test_ensure_repository_webhook_updates_existing_flowdeck_hook(monkeypatch):
    recorded = _client(
        monkeypatch,
        hooks=[{
            "id": 12,
            "config": {"url": "https://hooks.example.test/api/webhooks/github/oldid", "content_type": "json"},
        }],
    )
    hook_id = ensure_repository_webhook(
        "https://api.github.com",
        "github_pat_test",
        "acme/mobile",
        "https://hooks.example.test/api/webhooks/github/newid",
        "new-secret",
        ["pull_request"],
    )
    assert hook_id == "12"
    assert not any(item["method"] == "POST" for item in recorded)
    updated = next(item for item in recorded if item["method"] == "PATCH")
    assert updated["url"].endswith("/repos/acme/mobile/hooks/12")
    assert updated["json"]["events"] == ["pull_request"]
    assert updated["json"]["config"]["url"].endswith("/newid")
    assert updated["json"]["config"]["secret"] == "new-secret"


def test_ensure_repository_webhook_rejects_missing_permission(monkeypatch):
    _client(monkeypatch, post_status=403)
    with pytest.raises(httpx.HTTPStatusError):
        ensure_repository_webhook(
            "https://api.github.com",
            "github_pat_no_hooks",
            "acme/mobile",
            "https://hooks.example.test/api/webhooks/github/abcdef",
            "webhook-secret",
            ["issues"],
        )
