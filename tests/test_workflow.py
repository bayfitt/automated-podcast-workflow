from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from podcast_workflow.cli import load_manifest, public_page_matches, publish


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("en.mp3", "he.mp3", "description.txt", "art.png"):
            (self.root / name).write_text(name, encoding="utf-8")
        self.path = self.root / "manifest.json"
        self.path.write_text(json.dumps({"episodes": [
            {"language": "en", "show_id": "EnglishShow", "title": "English episode",
             "audio": "en.mp3", "description": "description.txt", "artwork": "art.png"},
            {"language": "he", "show_id": "HebrewShow", "title": "Hebrew episode",
             "audio": "he.mp3", "description": "description.txt", "artwork": "art.png"},
        ]}), encoding="utf-8")
        self.episodes = load_manifest(self.path)
        self.state = self.root / "private" / "state.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def result(command: list[str], **_: object) -> SimpleNamespace:
        show = command[command.index("--show-id") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": "published", "show_id": show, "episode_id": show + "Episode",
        }))

    @patch("podcast_workflow.cli.public_page_matches", return_value=(True, "public URL"))
    @patch("podcast_workflow.cli.subprocess.run", side_effect=result)
    def test_publishes_in_order_and_reuses_checkpoint(self, run, verify) -> None:
        self.assertTrue(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 2)
        self.assertIn("EnglishShow", run.call_args_list[0].args[0])
        self.assertIn("HebrewShow", run.call_args_list[1].args[0])
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
        self.assertTrue(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(verify.call_count, 4)

    @patch("podcast_workflow.cli.public_page_matches", return_value=(False, "HTTP 403"))
    @patch("podcast_workflow.cli.subprocess.run", side_effect=result)
    def test_pending_public_page_stops_and_does_not_reupload(self, run, verify) -> None:
        self.assertFalse(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 1)
        self.assertFalse(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(verify.call_count, 2)

    @patch("podcast_workflow.cli.subprocess.run")
    def test_failed_upload_stays_for_review(self, run) -> None:
        run.return_value = SimpleNamespace(returncode=1, stdout="{}")
        self.assertFalse(publish(self.episodes, self.state, "uploader"))
        self.assertFalse(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 1)

    def test_duplicate_audio_for_same_show_is_rejected(self) -> None:
        data = json.loads(self.path.read_text())
        data["episodes"].append(data["episodes"][0])
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "repeats"):
            load_manifest(self.path)

    @patch("podcast_workflow.cli.public_page_matches")
    @patch("podcast_workflow.cli.subprocess.run", side_effect=result)
    def test_pending_episode_can_verify_later_before_next_upload(self, run, verify) -> None:
        verify.side_effect = [(False, "HTTP 403"), (True, "English URL"), (True, "Hebrew URL")]
        self.assertFalse(publish(self.episodes, self.state, "uploader"))
        self.assertTrue(publish(self.episodes, self.state, "uploader"))
        self.assertEqual(run.call_count, 2)

    @patch("podcast_workflow.cli.urlopen")
    def test_public_page_requires_the_expected_title_and_episode_url(self, open_url) -> None:
        class Response(BytesIO):
            status = 200

            def geturl(self) -> str:
                return "https://open.spotify.com/episode/example"

        html = (
            '<meta property="og:title" content="Expected title">'
            '<meta property="og:url" content="https://open.spotify.com/episode/example">'
        )
        open_url.side_effect = lambda *args, **kwargs: Response(html.encode())
        self.assertTrue(public_page_matches("example", "Expected title")[0])
        self.assertFalse(public_page_matches("example", "A different title")[0])


if __name__ == "__main__":
    unittest.main()
