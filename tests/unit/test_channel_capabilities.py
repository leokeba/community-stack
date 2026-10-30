"""Channel capability declarations and pre-publication checks."""

from community_stack.announcements.adapters.activitypub import ActivityPubAdapter
from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.subscriptions.manager import SubscriberManager
from community_stack.whatsapp.announcement_forwarder import WhatsAppAnnouncementAdapter


class ShortChannel(ChannelAdapter):
    @property
    def name(self) -> str:
        return "short"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(label="Short channel", max_text_length=20)

    def publish(self, announcement):
        return DeliveryResult(success=True)

    def update(self, announcement):
        return DeliveryResult(success=True)

    def withdraw(self, announcement_id):
        return DeliveryResult(success=True)

    def health_check(self):
        return HealthResult(ok=True, details="ok")


def test_adapters_declare_how_edits_and_withdrawals_behave(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    adapters = [
        WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage),
        MatrixRoomAdapter(),
        ActivityPubAdapter(),
        EmailNewsletterAdapter(subscriber_manager=SubscriberManager(tmp_path / "subs.db")),
        WhatsAppAnnouncementAdapter(
            homeserver_url="http://matrix.test",
            room_id="!whatsapp:matrix.test",
            access_token="matrix-token-value",
        ),
    ]
    modes = {a.name: (a.capabilities.update_mode, a.capabilities.withdraw_mode) for a in adapters}
    assert modes == {
        "web_feed": (UpdateMode.IN_PLACE, WithdrawMode.DELETE),
        "matrix_room": (UpdateMode.FOLLOW_UP, WithdrawMode.NOTICE),
        "activitypub": (UpdateMode.IN_PLACE, WithdrawMode.DELETE),
        "email": (UpdateMode.FOLLOW_UP, WithdrawMode.NOTICE),
        "whatsapp": (UpdateMode.FOLLOW_UP, WithdrawMode.NOTICE),
    }
    assert all(a.capabilities.label for a in adapters)


def test_matrix_check_measures_the_text_it_actually_sends():
    announcement = Announcement(title="Hi", body="There")
    check = MatrixRoomAdapter().check(announcement)
    assert check.text_length == len("📢 Hi\n\nThere")
    assert not check.over_limit


def test_checks_flag_text_over_a_channel_limit_and_skip_disabled_channels(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    publisher = AnnouncementPublisher(
        storage=storage, adapters=[ShortChannel(), MatrixRoomAdapter()]
    )
    announcement = Announcement(title="A long title", body="And a body that is too long")

    checks = {check.name: check for check in publisher.channel_checks(announcement)}
    assert checks["short"].over_limit
    assert not checks["matrix_room"].over_limit

    storage.set_channel_enabled("short", False)
    assert [check.name for check in publisher.channel_checks(announcement)] == ["matrix_room"]
