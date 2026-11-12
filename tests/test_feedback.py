from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from podcast_workflow.feedback import (
    _feed,
    _unfiltered_query,
    allowed_graph_request,
    collect_show,
    load_config,
    main,
    normalize,
    save_private,
)


def comment(uri: str, text: str, *, reply: bool = False, creator: bool = False) -> dict:
    return {
        "episodeUri": "spotify:episode:example",
        "commentUri": uri,
        "oneOfCommentOrReply": {
            "commentUri": uri,
            "replyStr" if reply else "commentStr": text,
            "language": "he" if reply else "en",
            "oneOfAuthorDisplayMetadata": {
                "__typename": "ShowAuthorDisplayMetadata" if creator else "UserAuthorDisplayMetadata",
                "username": "private name",
                "userFullName": "private full name",
            },
        },
    }


def feed(entries: list[dict], token: str = "") -> dict:
    return {"data": {"showByShowUri": {"showComments": {
        "decoratedCommentOrReply": entries, "nextPageToken": token,
    }}}}


def operation(show_id: str = "Show123") -> dict:
    return {"operationName": "getCommentsAcrossEpisodes", "variables": {
        "showUri": f"spotify:show:{show_id}", "primaryFilters": [], "secondaryFilters": [],
        "commentTypeFilters": [], "pageToken": "", "pageSize": 15,
        "repliesFilter": ["LIST_COMMENT_PRIMARY_FILTER_PUBLISHED",
                          "LIST_COMMENT_PRIMARY_FILTER_NEEDS_REVIEW"],
    }, "extensions": {"persistedQuery": {"sha256Hash": "example"}}}


class FeedbackUnitTests(unittest.TestCase):
    def test_normalizes_comments_and_replies_without_author_metadata(self) -> None:
        first = comment("comment-1", "More context, please")
        nested = comment("reply-1", "תודה רבה", reply=True)
        first["mostRecentReplies"] = {"commentsOrReplies": [nested]}
        comments = normalize([first, nested, comment("creator", "Thanks!", creator=True)])
        self.assertEqual([item["id"] for item in comments], ["comment-1", "reply-1"])
        self.assertEqual(comments[1]["text"], "תודה רבה")
        self.assertEqual(comments[1]["episode_uri"], "spotify:episode:example")
        self.assertNotIn("private name", json.dumps(comments))
        self.assertNotIn("userFullName", json.dumps(comments))

    def test_unreadable_comment_and_incomplete_feed_are_not_empty(self) -> None:
        broken = comment("comment-1", "")
        with self.assertRaises(ValueError):
            normalize([broken])
        with self.assertRaises(ValueError):
            _feed({"data": {}})
        self.assertEqual(_feed(feed([])), ([], ""))
        with self.assertRaises(ValueError):
            _feed({"errors": [{"message": "failure"}]})

    def test_request_guard_blocks_mutations_unknowns_and_mixed_batches(self) -> None:
        self.assertTrue(allowed_graph_request([operation(), {"operationName": "getShowData"}]))
        for bad in [None, [], {"operationName": "markShowCommentsAsRead"},
                    {"operationName": "WebMarkShowCommentsAsRead"},
                    [operation(), {"operationName": "unknownNewOperation"}]]:
            self.assertFalse(allowed_graph_request(bad))

    def test_only_unfiltered_request_for_requested_show_is_selected(self) -> None:
        self.assertIsNone(_unfiltered_query(operation("Different"), "Show123"))
        selected = operation()
        selected["variables"]["primaryFilters"] = ["PUBLISHED"]
        self.assertIsNone(_unfiltered_query(selected, "Show123"))
        self.assertEqual(_unfiltered_query([{"operationName": "getShowData"}, operation()],
                                           "Show123")[0], 1)

    def test_private_snapshot_and_config_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text("{}")
            path = root / "config.json"
            path.write_text(json.dumps({"session": str(session), "shows": [
                {"show_id": "Show123", "language": "en"},
            ]}))
            self.assertEqual(load_config(path)["max_pages"], 5)
            save_private(root / "snapshots" / "latest.json", {"comments": ["private"]})
            self.assertEqual(os.stat(root / "snapshots" / "latest.json").st_mode & 0o777, 0o600)
            bad = {"session": str(session), "shows": [{"show_id": "../bad", "language": "en"}]}
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                load_config(path)


    def test_snapshot_cannot_overwrite_the_session_or_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.json"
            session.write_text("private session")
            config = root / "config.json"
            config.write_text(json.dumps({"session": str(session), "shows": [
                {"show_id": "Show123", "language": "en"},
            ]}))
            original = config.read_text()
            self.assertEqual(main([str(config), "--output", str(session)]), 2)
            self.assertEqual(session.read_text(), "private session")
            self.assertEqual(main([str(config), "--output", str(config)]), 2)
            self.assertEqual(config.read_text(), original)


class FakeRequest:
    def __init__(self):
        self.post_data_json = [operation()]


class FakeResponse:
    url = "https://creators-graph.spotify.com/v2/graph-pq"
    status = 200
    def __init__(self, data: object):
        self.request = FakeRequest()
        self.data = data

    async def json(self):
        return self.data


class FakePage:
    def __init__(self, first: object):
        self.first = first
        self.callback = None

    async def route(self, pattern, handler):
        self.handler = handler

    def on(self, event, callback):
        self.callback = callback

    async def goto(self, url, **kwargs):
        self.callback(FakeResponse([self.first]))

    async def close(self):
        pass


class FakeAPI:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.requests = []

    async def fetch(self, request, *, data, timeout):
        self.requests.append(data)
        return FakeResponse([self.pages.pop(0)])


class FakeContext:
    def __init__(self, first: dict, following: list[dict]):
        self.page = FakePage(first)
        self.request = FakeAPI(following)

    async def new_page(self):
        return self.page


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_pagination_and_distinct_comments(self) -> None:
        context = FakeContext(feed([comment("one", "First")], "next"),
                              [feed([comment("two", "Second")], "more")])
        result = await collect_show(context, {"show_id": "Show123", "language": "en"}, 2)
        self.assertEqual([item["id"] for item in result["comments"]], ["one", "two"])
        self.assertTrue(result["truncated"])
        self.assertEqual(context.request.requests[0][0]["variables"]["pageToken"], "next")
        self.assertEqual(result["pages"], 2)

    async def test_repeated_page_token_is_an_error(self) -> None:
        context = FakeContext(feed([], "next"), [feed([], "next")])
        with self.assertRaisesRegex(ValueError, "repeated"):
            await collect_show(context, {"show_id": "Show123", "language": "en"}, 3)


if __name__ == "__main__":
    unittest.main()
