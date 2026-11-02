"""CLI commands for inspecting channel publications and recording manual outcomes."""

import re
from pathlib import Path

import yaml
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.cli import main

REPOSITORY = Path(__file__).resolve().parents[2]


def test_cli_lists_manual_publications_and_records_lifecycle_outcomes(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    config = yaml.safe_load((REPOSITORY / "config/example.yaml").read_text(encoding="utf-8"))
    config["community"] = {
        "name": "Example Community",
        "social": [{"platform": "instagram", "url": "https://www.instagram.com/example"}],
    }
    (tmp_path / "community-stack.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    assert main(["announce", "draft", "--title", "Food drive", "--body", "Bring supplies."]) == 0
    draft_output = capsys.readouterr().out
    announcement_id = re.search(r"ID: (ann_[A-Za-z0-9_]+)", draft_output).group(1)

    assert main(["announce", "publish", announcement_id]) == 0
    storage = AnnouncementStorage()
    delivery = next(
        item
        for item in storage.get_delivery_states(announcement_id)
        if item["adapter_name"] == "instagram"
    )
    assert delivery["status"] == "prepared"
    assert delivery["operation"] == "manual_publish"

    assert main(["announce", "deliveries", announcement_id]) == 0
    output = capsys.readouterr().out
    assert "PREPARED: manual publication required" in output
    assert "instagram: Prepared (manual publish;" in output
    assert (
        main(
            [
                "announce",
                "outcome",
                announcement_id,
                "instagram",
                "published",
                "--post-url",
                "http://instagram.example/p/abc/",
                "--actor",
                "olga",
            ]
        )
        == 1
    )
    delivery = next(
        item
        for item in storage.get_delivery_states(announcement_id)
        if item["adapter_name"] == "instagram"
    )
    assert delivery["status"] == "prepared"

    post_url = "https://www.instagram.com/p/abc123/"
    assert (
        main(
            [
                "announce",
                "outcome",
                announcement_id,
                "instagram",
                "published",
                "--post-url",
                post_url,
                "--actor",
                "olga",
            ]
        )
        == 0
    )
    assert main(["announce", "update", announcement_id, "--body", "Bring supplies tomorrow."]) == 0
    assert "manual follow-up" in capsys.readouterr().out
    assert (
        main(
            [
                "announce",
                "outcome",
                announcement_id,
                "instagram",
                "updated",
                "--post-url",
                post_url,
                "--actor",
                "olga",
            ]
        )
        == 0
    )
    assert main(["announce", "withdraw", announcement_id]) == 0
    assert "Manual withdrawal required" in capsys.readouterr().out
    assert (
        main(
            [
                "announce",
                "outcome",
                announcement_id,
                "instagram",
                "withdrawn",
                "--actor",
                "olga",
            ]
        )
        == 0
    )

    delivery = next(
        item
        for item in storage.get_delivery_states(announcement_id)
        if item["adapter_name"] == "instagram"
    )
    assert delivery["status"] == "withdrawn"
    assert delivery["operation"] == "manual_withdraw"
    assert delivery["recorded_by"] == "olga"
    assert delivery["post_url"] is None
    assert main(["announce", "deliveries", announcement_id]) == 0
    output = capsys.readouterr().out
    assert "instagram: Withdrawn (manual withdraw;" in output
    assert "ATTEMPT HISTORY:" in output
    assert "recorded by olga" in output
