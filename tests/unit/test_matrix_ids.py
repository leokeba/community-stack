import pytest
from community_stack.matrix_ids import ROOM_ID


@pytest.mark.parametrize(
    "room_id",
    [
        "!rYmCpUwG1PT34NH83HMrUJVPHjXSJttm7DV2jm7Cjjw",  # room version 12: no server part
        "!-hihwVuJpzWue_wt0oYoVcOWufYw_mIP13yqHzDxJ70",
        "!GmnucseOFJpvhryzGU:matrix.example.org",
        "!abc:localhost:8448",
    ],
)
def test_room_ids_of_every_room_version_are_accepted(room_id):
    assert ROOM_ID.fullmatch(room_id)


@pytest.mark.parametrize(
    "room_id",
    ["", "!", "abc:example.org", "#alias:example.org", "!a b:example.org", "!abc:", "!abc/def"],
)
def test_malformed_room_ids_are_refused(room_id):
    assert not ROOM_ID.fullmatch(room_id)
