from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
from pathlib import Path
import re
from urllib.parse import quote, urlparse
import httpx
from backend.config import Settings
from backend.errors import ConfigurationNeeded, OutcomeUnknown, WaitForDependency
from backend.db import utcnow


class GitHub:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, sessions=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=45, follow_redirects=False)
        self.sessions = sessions
        self.base = settings.github_api_url.rstrip("/")

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def _repo_from_path(path: str) -> str | None:
        match = re.match(r"^/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)", path)
        return match.group(1) if match else None

    def _token(self, repo: str | None = None) -> str:
        if self.sessions is not None:
            if repo is None:
                raise ConfigurationNeeded("无法确定 GitHub 请求所属的项目")
            from backend.projects import project_token

            return project_token(self.settings, self.sessions, repo, require_active=True)
        token = self.settings.github_token
        if not token:
            raise ConfigurationNeeded("尚未配置 GITHUB_TOKEN")
        return token

    async def request(self, method: str, path: str, **kwargs):
        token = self._token(self._repo_from_path(path))
        supplied_headers = kwargs.pop("headers", {})
        headers = {key: value for key, value in supplied_headers.items() if key.lower() != "authorization"}
        headers.setdefault("Accept", "application/vnd.github+json")
        headers["Authorization"] = f"Bearer {token}"
        response = await self.client.request(method, self.base + path, headers=headers, **kwargs)
        if response.status_code == 429 or (response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
            raise WaitForDependency("GitHub API 额度暂时不足，稍后重新同步")
        response.raise_for_status()
        return response.json() if response.content else None

    async def pages(self, path: str, params: dict | None = None, max_pages: int = 100, list_key: str | None = None):
        # Fail explicitly rather than treating a truncated list as complete.
        for page in range(1, max_pages + 1):
            response = await self.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            values = response[list_key] if list_key else response
            for value in values:
                yield value
            if len(values) < 100:
                return
        raise WaitForDependency("GitHub 分页达到本次同步上限，将保留已有记录并等待处理")

    async def pull(self, repo: str, number: int):
        return await self.request("GET", f"/repos/{repo}/pulls/{number}")

    async def checks(self, repo: str, sha: str):
        values: dict[str, dict] = {}
        async for check in self.pages(f"/repos/{repo}/commits/{sha}/check-runs", {"filter": "latest"}, list_key="check_runs"):
            state = "pending" if check["status"] != "completed" else ("success" if check.get("conclusion") in {"success", "neutral", "skipped"} else "failure")
            item = {"name": check["name"], "state": state, "id": check.get("id", 0), "kind": "check", "app_id": (check.get("app") or {}).get("id")}
            # Do not let a different check with the same name hide a failure.
            if check["name"] in values and values[check["name"]]["state"] != "success":
                continue
            values[check["name"]] = item
        statuses = await self.request("GET", f"/repos/{repo}/commits/{sha}/status", params={"per_page": 100})
        for status in statuses.get("statuses", []):
            name = status["context"]
            state = "success" if status["state"] == "success" else ("pending" if status["state"] == "pending" else "failure")
            if name not in values or values[name]["state"] == "success":
                values[name] = {"name": name, "state": state, "kind": "status"}
        return list(values.values())

    async def set_review_status(self, repo: str, sha: str, state: str, description: str):
        return await self.request("POST", f"/repos/{repo}/statuses/{sha}", json={"state": state, "context": "flowdeck/agent-review", "description": description[:140]})

    async def comment_once(self, repo: str, number: int, marker: str, body: str):
        async for comment in self.pages(f"/repos/{repo}/issues/{number}/comments"):
            if marker in (comment.get("body") or ""):
                return comment
        return await self.request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": marker + "\n" + body})

    async def merge(self, repo: str, number: int, sha: str, method: str):
        try:
            result = await self.request("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"sha": sha, "merge_method": method})
        except httpx.TransportError as exc:
            raise OutcomeUnknown("合并请求结果未知，恢复任务时必须先查询 GitHub") from exc
        if not result.get("merged"):
            raise WaitForDependency("GitHub 尚未允许合并：" + result.get("message", "条件未满足"))
        return result

    async def commit(self, repo: str, ref: str):
        return await self.request("GET", f"/repos/{repo}/commits/{quote(ref, safe='')}")

    async def _optional(self, method: str, path: str, **kwargs):
        try:
            return await self.request(method, path, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def tag_exists(self, repo: str, tag: str) -> bool:
        return await self._optional("GET", f"/repos/{repo}/git/ref/tags/{quote(tag, safe='')}") is not None

    async def release_by_tag(self, repo: str, tag: str) -> dict | None:
        return await self._optional("GET", f"/repos/{repo}/releases/tags/{quote(tag, safe='')}")

    async def create_release(self, repo: str, tag: str, sha: str, name: str = "") -> dict:
        existing = await self.release_by_tag(repo, tag)
        if existing:
            return existing
        try:
            return await self.request("POST", f"/repos/{repo}/releases", json={
                "tag_name": tag,
                "target_commitish": sha,
                "name": name or tag,
                "draft": False,
                "prerelease": False,
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {409, 422}:
                current = await self.release_by_tag(repo, tag)
                if current:
                    return current
            raise

    async def linked_issues(self, repo: str, number: int) -> list[dict]:
        # GitHub's explicit PR closing references; never infer links from branch names.
        if self.base != "https://api.github.com":
            graphql_url = self.base.removesuffix("/v3") + "/graphql"
        else:
            graphql_url = "https://api.github.com/graphql"
        owner, name = repo.split("/")
        query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:100){nodes{databaseId number title url repository{nameWithOwner}} pageInfo{hasNextPage}}}}}"
        response = await self.client.post(graphql_url, headers={"Authorization": f"Bearer {self._token(repo)}"}, json={"query": query, "variables": {"owner": owner, "name": name, "number": number}})
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise ConfigurationNeeded("无法读取 GitHub 的 Issue/PR 关联，请检查 GraphQL 权限")
        connection = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("closingIssuesReferences") or {}
        if connection.get("pageInfo", {}).get("hasNextPage"):
            raise ConfigurationNeeded("PR 关联超过 100 个 Issue，需要扩展关联分页")
        return connection.get("nodes", [])

    async def update_notes(self, repo: str, release_id: int, public: str, technical: str):
        current = await self.request("GET", f"/repos/{repo}/releases/{release_id}")
        start, end = "<!-- flowdeck:notes:start -->", "<!-- flowdeck:notes:end -->"
        body = current.get("body") or ""
        block = f"{start}\n## 更新说明\n\n{public}\n\n<details>\n<summary>研发版本记录</summary>\n\n{technical}\n\n</details>\n{end}"
        if start in body or end in body:
            if body.count(start) != 1 or body.count(end) != 1 or body.index(start) >= body.index(end):
                raise ConfigurationNeeded("GitHub 更新记录区段标记不完整，请先修复，避免覆盖人工内容")
            body = body[:body.index(start)] + block + body[body.index(end) + len(end):]
        else:
            body = body.rstrip() + ("\n\n" if body.strip() else "") + block
        if body != (current.get("body") or ""):
            await self.request("PATCH", f"/repos/{repo}/releases/{release_id}", json={"body": body})

    async def download_asset(self, repo: str, asset: dict, target: Path):
        if asset.get("size", 0) > 1024 * 1024 * 1024:
            raise ConfigurationNeeded("APK 超过 1 GiB，需调整下载策略")
        headers = {"Authorization": f"Bearer {self._token(repo)}", "Accept": "application/octet-stream"}
        url = f"{self.base}/repos/{repo}/releases/assets/{int(asset['id'])}"
        response = await self.client.get(url, headers=headers, follow_redirects=False)
        if response.is_redirect:
            url = response.headers["location"]
            host = urlparse(url).hostname or ""
            api_host = urlparse(self.base).hostname or ""
            if urlparse(url).scheme != "https" or not (host.endswith(".githubusercontent.com") or host.endswith(".github.com") or host == api_host):
                raise ConfigurationNeeded("APK 下载跳转到了未允许的主机")
            headers = {}  # Never forward the GitHub token to an asset/CDN host.
        elif response.status_code == 200 and "application/json" not in response.headers.get("content-type", ""):
            target.write_bytes(response.content)
            return self._verify_asset(asset, target)
        else:
            response.raise_for_status()
            raise WaitForDependency("APK 附件尚不可下载")
        size = 0
        temporary = target.with_suffix(".partial")
        async with self.client.stream("GET", url, headers=headers, timeout=300, follow_redirects=False) as stream:
            stream.raise_for_status()
            with temporary.open("wb") as output:
                async for chunk in stream.aiter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024 * 1024:
                        raise ConfigurationNeeded("APK 下载超过大小限制")
                    output.write(chunk)
        temporary.replace(target)
        return self._verify_asset(asset, target)

    @staticmethod
    def _verify_asset(asset: dict, target: Path):
        if asset.get("size") and target.stat().st_size != asset["size"]:
            raise WaitForDependency("APK 下载大小不符，将重新获取")
        with target.open("rb") as apk:
            digest = hashlib.file_digest(apk, "sha256").hexdigest()
        expected = asset.get("digest")
        if expected and expected.startswith("sha256:") and expected != "sha256:" + digest:
            raise RuntimeError("APK SHA-256 与 GitHub 附件校验值不符")
        return digest


async def previous_published_release_tag(github, repo: str, *, release_id, published_at: str, configured: str = "") -> str:
    value = (configured or "").strip()
    if value:
        return value
    candidates = []
    async for item in github.pages(f"/repos/{repo}/releases", max_pages=20):
        if item.get("id") == release_id or item.get("draft") or item.get("prerelease"):
            continue
        earlier = item.get("published_at") or ""
        if not earlier or not published_at or earlier >= published_at:
            continue
        candidates.append(item)
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item["published_at"])["tag_name"]


def encrypt_actions_secret(public_key: str, secret_value: str) -> str:
    from base64 import b64encode
    from nacl import encoding, public

    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    encrypted = public.SealedBox(key).encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def _github_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def ensure_repository_webhook(api_url: str, token: str, repo: str, url: str, secret: str, events: list[str]) -> str:
    if not url.startswith("https://"):
        raise ConfigurationNeeded("Webhook 地址必须是 GitHub 可访问的 HTTPS 公网入口")
    prefix = url.rsplit("/", 1)[0] + "/"
    payload = {
        "name": "web",
        "active": True,
        "events": list(events),
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }
    headers = _github_headers(token)
    base = api_url.rstrip("/")
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        listed = client.get(f"{base}/repos/{repo}/hooks", headers=headers, params={"per_page": 100})
        listed.raise_for_status()
        existing = None
        for hook in listed.json() or []:
            config = hook.get("config") or {}
            current = config.get("url") or ""
            if isinstance(current, str) and current.startswith(prefix) and hook.get("id") is not None:
                existing = hook
                break
        if existing:
            response = client.patch(f"{base}/repos/{repo}/hooks/{existing['id']}", headers=headers, json=payload)
            response.raise_for_status()
            return str(existing["id"])
        created = client.post(f"{base}/repos/{repo}/hooks", headers=headers, json=payload)
        created.raise_for_status()
        hook_id = (created.json() or {}).get("id")
        if hook_id is None:
            raise ConfigurationNeeded("GitHub 未返回 Webhook ID")
        return str(hook_id)


def put_repository_secrets(api_url: str, token: str, repo: str, secrets: dict[str, str]):
    if not secrets:
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        key = client.get(f"{api_url.rstrip('/')}/repos/{repo}/actions/secrets/public-key", headers=headers)
        key.raise_for_status()
        payload = key.json()
        public_key, key_id = payload.get("key"), payload.get("key_id")
        if not isinstance(public_key, str) or not isinstance(key_id, str) or not public_key or not key_id:
            raise ConfigurationNeeded("GitHub 未返回可用的 Actions Secrets 公钥")
        for name, value in secrets.items():
            response = client.put(
                f"{api_url.rstrip('/')}/repos/{repo}/actions/secrets/{name}",
                headers=headers,
                json={"encrypted_value": encrypt_actions_secret(public_key, value), "key_id": key_id},
            )
            response.raise_for_status()
