"""The console's community profile: saved through the agent, validated, backed up, and reversible."""

from pathlib import Path

import pytest
import yaml
from community_stack.admin.store import AdminStore
from community_stack.agent import community
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService
from community_stack.config import validate_config

PASSWORD = "a-long-safe-test-password"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    (stack / "data" / "matrix").mkdir(parents=True)
    (stack / "data" / "matrix" / "homeserver.yaml").write_text("server_name: x\n", encoding="utf-8")
    (stack / "community-stack.yaml").write_text(
        (REPO / "config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (stack / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (stack / ".env").write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    store = AdminStore(stack / "data" / "announcements" / "announcements.db")
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")
    return stack


@pytest.fixture
def service(root):
    return AgentService(AgentContext(project_dir=root))


def _profile(**overrides):
    values = {
        "name": "Riverside Collective",
        "description": "Neighbours helping each other.",
        "url": "https://community.example.org",
        "logo": "logo.png",
        "contact_email": "hello@example.org",
        "emergency_instructions": "Use the feed if chat is down.",
        "social": [
            {
                "platform": "instagram",
                "url": "https://instagram.com/riverside",
                "handle": "@riverside",
            },
            {"platform": "", "url": "", "handle": ""},
        ],
        "independent": {
            "website": True,
            "rss": True,
            "email": True,
            "matrix": False,
            "activitypub": False,
        },
    }
    values.update(overrides)
    return values


def _run(service, actor, values):
    return service.handle(
        {"op": "community.apply", "actor": actor, "params": {"community": values}}
    )


def test_owner_saves_the_profile_and_the_rest_of_the_file_is_kept(root, service):
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    reply = _run(service, "olga", _profile())
    assert reply["ok"], reply

    after = (root / "community-stack.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(after)
    assert config["community"]["name"] == "Riverside Collective"
    assert config["community"]["social"] == [
        {"platform": "instagram", "url": "https://instagram.com/riverside", "handle": "@riverside"}
    ]
    assert config["community"]["independent"]["matrix"] is False
    assert validate_config(config) == []
    # Everything outside the community block is unchanged, including comments.
    assert config["services"]["matrix"]["enabled"] is True
    assert "# starter | community" in after
    assert after.startswith(before.split("\ncommunity:")[0])


def test_a_safety_backup_and_the_previous_file_are_kept(root, service):
    _run(service, "olga", _profile())
    assert (root / "community-stack.yaml.previous").is_file()
    assert "community:" not in (root / "community-stack.yaml.previous").read_text(encoding="utf-8")
    assert list((root / "backups").glob("snap_*"))


@pytest.mark.parametrize(
    "values",
    [
        _profile(url="http://insecure.example.org"),
        _profile(name=""),
        _profile(surprise="field"),
        _profile(logo="missing.png"),
        _profile(social=[{"platform": "myspace", "url": "https://myspace.com/x"}]),
        _profile(independent={"carrier_pigeon": True}),
        _profile(contact_email="not-an-address"),
    ],
)
def test_invalid_profiles_are_refused_and_nothing_changes(root, service, values):
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    reply = _run(service, "olga", values)
    assert reply["ok"] is False
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before
    assert not (root / "community-stack.yaml.previous").exists()


def test_publishers_may_not_change_the_profile(root, service):
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    reply = _run(service, "pat", _profile())
    assert reply["ok"] is False
    assert (
        "may not run this operation" in reply["error"]["message"]
        if isinstance(reply.get("error"), dict)
        else True
    )
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before


def test_splice_adds_a_missing_block_at_the_end():
    text = "project:\n  name: x\n"
    out = community.splice(text, {"name": "Hall"})
    assert out.startswith("project:\n  name: x\n")
    assert yaml.safe_load(out)["community"] == {"name": "Hall"}


def test_splice_replaces_only_the_existing_block():
    text = "community:\n  name: Old\nservices:\n  matrix:\n    enabled: true\n"
    out = community.splice(text, {"name": "New"})
    data = yaml.safe_load(out)
    assert data["community"] == {"name": "New"}
    assert data["services"]["matrix"]["enabled"] is True


def test_unknown_social_platforms_are_refused_by_the_builder():
    with pytest.raises(community.CommunityError):
        community.build_block(
            _profile(social=[{"platform": "myspace", "url": "https://myspace.com/x"}])
        )
