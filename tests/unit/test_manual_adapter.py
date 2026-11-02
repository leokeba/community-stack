from community_stack.announcements.adapters.base import UpdateMode, WithdrawMode
from community_stack.announcements.adapters.manual import ManualChannelAdapter
from community_stack.announcements.models import Announcement
from community_stack.announcements.platforms import PLATFORMS


def test_manual_adapter_returns_prepared_and_uses_platform_copy():
    adapter = ManualChannelAdapter(PLATFORMS["instagram"])
    announcement = Announcement(
        title="Food drive",
        body="Bring supplies.",
        channel_text={"instagram": "Join our food drive!"},
    )

    result = adapter.publish(announcement)

    assert adapter.name == "instagram"
    assert adapter.preview_text(announcement) == "Join our food drive!"
    assert result.success and result.prepared
    assert adapter.health_check().ok
    assert adapter.capabilities.update_mode == UpdateMode.MANUAL
    assert adapter.capabilities.withdraw_mode == WithdrawMode.MANUAL
    assert "manually" in adapter.capabilities.update_description
    assert "manually" in adapter.capabilities.withdraw_description
    assert adapter.update(announcement).prepared
    assert adapter.withdraw(announcement.id).prepared


def test_manual_adapter_rejects_over_limit_and_does_not_guess_limits():
    limited = ManualChannelAdapter(PLATFORMS["instagram"])
    limit = limited.capabilities.max_text_length
    assert limit is not None
    oversized = Announcement(
        title="Food drive",
        body="Bring supplies.",
        channel_text={"instagram": "x" * (limit + 1)},
    )
    result = limited.publish(oversized)

    assert not result.success and not result.prepared
    assert "verified limit" in result.error
    assert not limited.update(oversized).prepared
    assert ManualChannelAdapter(PLATFORMS["tiktok"]).capabilities.max_text_length is None
