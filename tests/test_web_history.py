#!/usr/bin/env python3
"""Retention and privacy contracts for local CardPulse message history."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT_DIR / "lib"))

import cardpulse_web  # noqa: E402


class WebHistoryTest(unittest.TestCase):
    def test_load_retains_recent_record_and_drops_legacy_expired_and_malformed_rows(self):
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "state" / "messages.jsonl"
            history_path.parent.mkdir()
            records = [
                {
                    "id": "recent",
                    "direction": "inbound",
                    "body": "keep",
                    "first_seen_at": (now - timedelta(days=89)).isoformat(),
                },
                {
                    "id": "expired",
                    "direction": "inbound",
                    "body": "remove",
                    "first_seen_at": (now - timedelta(days=90)).isoformat(),
                },
                {
                    "id": "future",
                    "direction": "inbound",
                    "body": "remove",
                    "first_seen_at": (now + timedelta(minutes=1)).isoformat(),
                },
                {"id": "legacy", "direction": "inbound", "body": "remove"},
            ]
            history_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\nnot json\n",
                encoding="utf-8",
            )

            history = cardpulse_web.MessageHistory(history_path, now=lambda: now)

            self.assertEqual([item["id"] for item in history.list()], ["recent"])
            rewritten = history_path.read_text(encoding="utf-8")
            self.assertIn('"recent"', rewritten)
            self.assertNotIn('"expired"', rewritten)
            self.assertNotIn('"future"', rewritten)
            self.assertNotIn('"legacy"', rewritten)
            self.assertNotIn("not json", rewritten)

    def test_add_sets_first_seen_at_once_and_clear_removes_all_records(self):
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "private" / "messages.jsonl"
            history = cardpulse_web.MessageHistory(history_path, now=lambda: now)

            created = history.add(
                {"id": "message-1", "direction": "inbound", "body": "original"}
            )
            updated = history.add(
                {
                    "id": "message-1",
                    "direction": "inbound",
                    "body": "full body after read",
                }
            )
            cleared_count = history.clear()

            self.assertEqual(created["first_seen_at"], now.isoformat())
            self.assertEqual(updated["first_seen_at"], now.isoformat())
            self.assertEqual(cleared_count, 1)
            self.assertEqual(history.list(), [])
            self.assertEqual(history_path.read_text(encoding="utf-8"), "")

    def test_history_and_state_files_correct_private_permissions_when_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "private" / "messages.jsonl"
            history = cardpulse_web.MessageHistory(history_path)
            history.add({"id": "message-1", "direction": "inbound", "body": "body"})
            state_path = Path(tmpdir) / "state" / "web-state.json"
            state = cardpulse_web.OpsState(state_path)
            state.note_failure("failed")

            if os.name != "nt":
                self.assertEqual(history_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(history_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(state_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_private_file_helpers_request_0600_on_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            private_dir = Path(tmpdir) / "private"
            append_path = private_dir / "audit.jsonl"
            rewrite_path = private_dir / "state.json"

            with mock.patch.object(cardpulse_web.os, "open", wraps=os.open) as private_open:
                cardpulse_web.append_private_text(append_path, "{\"ok\":true}\n")

            open_calls = [
                call
                for call in private_open.call_args_list
                if Path(call.args[0]) == append_path
            ]
            self.assertEqual(len(open_calls), 1)
            self.assertEqual(open_calls[0].args[2], 0o600)

            cardpulse_web.atomic_write_private_text(rewrite_path, "{\"state\":true}\n")

            self.assertEqual(append_path.read_text(encoding="utf-8"), "{\"ok\":true}\n")
            self.assertEqual(rewrite_path.read_text(encoding="utf-8"), "{\"state\":true}\n")
            if os.name != "nt":
                self.assertEqual(append_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(rewrite_path.stat().st_mode & 0o777, 0o600)

    def test_ops_state_file_does_not_persist_message_body_phone_or_preview(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state" / "web-state.json"
            state = cardpulse_web.OpsState(state_path)
            state.note_current_messages(
                [
                    {
                        "id": "message-1",
                        "direction": "inbound",
                        "indexes": ["2"],
                        "status": "REC UNREAD",
                        "phone": "+8613800138000",
                        "preview": "secret preview",
                        "body": "secret body",
                    }
                ]
            )

            raw = state_path.read_text(encoding="utf-8")

            self.assertNotIn("+8613800138000", raw)
            self.assertNotIn("secret preview", raw)
            self.assertNotIn("secret body", raw)
            self.assertIn("message-1", raw)


if __name__ == "__main__":
    unittest.main()
