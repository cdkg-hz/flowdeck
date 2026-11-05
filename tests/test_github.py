from backend.github import previous_published_release_tag


CURRENT = {
    "id": 99,
    "tag_name": "v2",
    "published_at": "2026-09-13T10:00:00Z",
    "draft": False,
    "prerelease": False,
}
PREVIOUS = {
    "id": 88,
    "tag_name": "v1",
    "published_at": "2026-09-01T10:00:00Z",
    "draft": False,
    "prerelease": False,
}
OLDER = {
    "id": 70,
    "tag_name": "v0",
    "published_at": "2026-08-01T10:00:00Z",
    "draft": False,
    "prerelease": False,
}


class SequenceGitHub:
    def __init__(self, items):
        self.items = items
        self.called = False

    async def pages(self, path, params=None, max_pages=100, list_key=None):
        self.called = True
        for item in self.items:
            yield item


async def test_previous_published_release_tag_uses_configured_without_listing():
    github = SequenceGitHub([PREVIOUS, CURRENT])
    tag = await previous_published_release_tag(
        github,
        "acme/mobile",
        release_id=CURRENT["id"],
        published_at=CURRENT["published_at"],
        configured="  v1  ",
    )
    assert tag == "v1"
    assert github.called is False


async def test_previous_published_release_tag_picks_latest_earlier_published():
    github = SequenceGitHub([
        CURRENT,
        {**PREVIOUS, "draft": True},
        {**OLDER, "prerelease": True},
        {**PREVIOUS, "id": 87, "tag_name": "v1.1", "published_at": "2026-09-10T10:00:00Z"},
        OLDER,
        {**PREVIOUS, "id": 89, "tag_name": "later", "published_at": "2026-09-14T10:00:00Z"},
        {**PREVIOUS, "id": 86, "tag_name": "undated", "published_at": ""},
    ])
    tag = await previous_published_release_tag(
        github,
        "acme/mobile",
        release_id=CURRENT["id"],
        published_at=CURRENT["published_at"],
    )
    assert tag == "v1.1"


async def test_previous_published_release_tag_returns_empty_when_first_release():
    github = SequenceGitHub([CURRENT])
    tag = await previous_published_release_tag(
        github,
        "acme/mobile",
        release_id=CURRENT["id"],
        published_at=CURRENT["published_at"],
    )
    assert tag == ""
