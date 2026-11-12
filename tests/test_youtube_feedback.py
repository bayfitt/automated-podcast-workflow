from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from podcast_workflow.youtube_feedback import collect_video, normalize_comments


class YouTubeFeedbackTests(unittest.TestCase):
    def test_normalization_omits_personal_metadata_and_limits_results(self) -> None:
        items = [
            {"id": "a", "text": "Please include more context", "author": "Private Name",
             "author_id": "private user", "author_url": "https://example.invalid/user",
             "parent": "root", "timestamp": 1},
            {"id": "b", "text": "תודה רבה", "author": "Another Name"},
        ]
        comments = normalize_comments(items, 1)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["text"], "Please include more context")
        self.assertNotIn("Private Name", json.dumps(comments))
        self.assertNotIn("author_id", json.dumps(comments))

    @patch("podcast_workflow.youtube_feedback.subprocess.run")
    def test_retrieves_selected_video_without_media_or_shell(self, run) -> None:
        run.return_value = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "id": "abcdefghijk", "comment_count": 2,
            "comments": [{"id": "a", "text": "Question"}],
        }))
        result = collect_video("abcdefghijk", 100)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["truncated"])
        command = run.call_args.args[0]
        self.assertIn("--skip-download", command)
        self.assertIn("--no-playlist", command)
        self.assertIn("--ignore-config", command)
        self.assertEqual(command[-1], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("podcast_workflow.youtube_feedback.subprocess.run")
    def test_failure_or_unknown_empty_is_not_no_comments(self, run) -> None:
        run.return_value = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "id": "abcdefghijk", "comment_count": None, "comments": [],
        }))
        with self.assertRaises(ValueError):
            collect_video("abcdefghijk", 100)
        run.return_value = SimpleNamespace(returncode=0, stderr="WARNING: comment retrieval failed",
                                           stdout="{}")
        with self.assertRaises(RuntimeError):
            collect_video("abcdefghijk", 100)

    @patch("podcast_workflow.youtube_feedback.subprocess.run")
    def test_verified_empty_count(self, run) -> None:
        run.return_value = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "id": "abcdefghijk", "comment_count": 0, "comments": [],
        }))
        result = collect_video("abcdefghijk", 100)
        self.assertEqual(result["comments"], [])
        self.assertFalse(result["truncated"])

    def test_invalid_video_id_and_unreadable_content_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            collect_video("invalid video", 100)
        with self.assertRaises(ValueError):
            normalize_comments([{"id": "one", "text": ""}], 100)


if __name__ == "__main__":
    unittest.main()
