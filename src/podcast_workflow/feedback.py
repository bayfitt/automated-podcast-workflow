"""Collect a private, bounded Spotify listener-feedback snapshot.

Spotify for Creators has no public comments API in this project. This adapter
observes its logged-in web application and may need maintenance as it changes.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ID = re.compile(r"^[A-Za-z0-9_-]+$")
GRAPH_HOST = "creators-graph.spotify.com"
QUERY = "getCommentsAcrossEpisodes"
# Fail closed for unknown GraphQL operations, including mark-as-read mutations.
READ_OPERATIONS = frozenset({
    "WebGetUserAndCurrentShow", "WebGetShowsForUser", "CheckSuspension",
    "WebGetExternalPartnerId", "WebGetUser", "getShowData", "getEpisodesForShow",
    "WebGetOrgForShowUri", "WebGetNavigationContextForUser", "WebGetShowTypeByUri",
    "WebGetMembershipMarketEligibility", "WebGetUserRegistration",
    "WebGetShowCommentNotifications", QUERY, "GetCurrentMembership",
    "WebGetMonetizationLifecycleState",
})


def _items(payload: object) -> list[object]:
    return payload if isinstance(payload, list) else [payload]


def allowed_graph_request(payload: object) -> bool:
    items = _items(payload)
    return bool(items) and all(
        isinstance(item, dict) and item.get("operationName") in READ_OPERATIONS
        for item in items
    )


def _unfiltered_query(payload: object, show_id: str) -> tuple[int, dict] | None:
    for index, item in enumerate(_items(payload)):
        if not isinstance(item, dict) or item.get("operationName") != QUERY:
            continue
        v = item.get("variables")
        if (
            isinstance(v, dict) and v.get("showUri") == f"spotify:show:{show_id}"
            and v.get("primaryFilters") == [] and v.get("secondaryFilters") == []
            and v.get("commentTypeFilters") == [] and v.get("pageToken") == ""
            and set(v.get("repliesFilter", [])) == {
                "LIST_COMMENT_PRIMARY_FILTER_PUBLISHED", "LIST_COMMENT_PRIMARY_FILTER_NEEDS_REVIEW",
            }
        ):
            return index, item
    return None


def _feed(payload: object) -> tuple[list[dict], str]:
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("Spotify returned an invalid feedback response")
    try:
        feed = payload["data"]["showByShowUri"]["showComments"]
        entries, token = feed["decoratedCommentOrReply"], feed["nextPageToken"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Spotify feedback response was incomplete") from exc
    if not isinstance(entries, list) or not isinstance(token, str):
        raise TypeError("Spotify feedback response had an unexpected format")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("Spotify returned an invalid comment")
    return entries, token


def normalize(entries: list[dict]) -> list[dict[str, Any]]:
    """Keep comment text and provenance; do not retain names or profiles."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(wrapper: dict, episode_uri: object) -> None:
        item = wrapper.get("oneOfCommentOrReply")
        if not isinstance(item, dict):
            raise TypeError("Spotify comment was missing its content")
        author = item.get("oneOfAuthorDisplayMetadata") or {}
        if isinstance(author, dict) and author.get("__typename") == "ShowAuthorDisplayMetadata":
            return
        comment_id = item.get("commentUri") or wrapper.get("commentUri")
        text = item.get("commentStr") if item.get("commentStr") is not None else item.get("replyStr")
        if not isinstance(comment_id, str) or not comment_id:
            raise ValueError("Spotify comment was missing its identifier")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Spotify comment was missing its text")
        if comment_id in seen:
            return
        seen.add(comment_id)
        result.append({
            "id": comment_id,
            "episode_uri": episode_uri if isinstance(episode_uri, str) else None,
            "kind": "reply" if "replyStr" in item else "comment",
            "text": text,
            "language": item.get("language"),
            "created_at": item.get("createDate"),
            "moderation_state": item.get("initialModerationState"),
            "blocked": wrapper.get("isBlocked"),
        })

    for entry in entries:
        episode_uri = entry.get("episodeUri")
        add(entry, episode_uri)
        replies = entry.get("mostRecentReplies")
        if replies is not None:
            if not isinstance(replies, dict) or not isinstance(replies.get("commentsOrReplies"), list):
                raise ValueError("Spotify replies had an unexpected format")
            for reply in replies["commentsOrReplies"]:
                if not isinstance(reply, dict):
                    raise TypeError("Spotify returned an invalid reply")
                add(reply, episode_uri)
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("session"), str):
        raise TypeError("configuration needs a session path")
    if not Path(config["session"]).expanduser().is_file():
        raise ValueError("Spotify session file is missing")
    shows = config.get("shows")
    if not isinstance(shows, list) or not shows:
        raise ValueError("configuration needs at least one show")
    ids: set[str] = set()
    for show in shows:
        if not isinstance(show, dict):
            raise TypeError("each show must be an object")
        show_id, language = show.get("show_id"), show.get("language")
        if not isinstance(show_id, str) or not ID.fullmatch(show_id) or show_id in ids:
            raise ValueError("each show needs a unique valid show_id")
        if not isinstance(language, str) or not language.strip():
            raise ValueError("each show needs a language")
        ids.add(show_id)
    max_pages = config.get("max_pages", 5)
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("max_pages must be an integer from 1 to 10")
    config["max_pages"] = max_pages
    return config


async def collect_show(context: Any, show: dict, max_pages: int) -> dict[str, Any]:
    """Observe the first unfiltered show feed and request bounded subsequent pages."""
    show_id = show["show_id"]
    page = await context.new_page()
    first = asyncio.get_running_loop().create_future()
    pending: set[asyncio.Task] = set()

    async def guard(route: Any) -> None:
        try:
            valid = allowed_graph_request(route.request.post_data_json)
        except (TypeError, ValueError):
            valid = False
        if valid:
            await route.continue_()
        else:
            await route.abort()

    async def inspect(response: Any) -> None:
        if first.done():
            return
        url = urlparse(response.url)
        if url.hostname != GRAPH_HOST or url.path != "/v2/graph-pq":
            return
        try:
            request_data = response.request.post_data_json
            selected = _unfiltered_query(request_data, show_id)
            if selected is None:
                return
            if response.status != 200:
                raise ValueError("Spotify feedback request failed")
            index, operation = selected
            response_data = await response.json()
            if isinstance(request_data, list):
                if not isinstance(response_data, list) or index >= len(response_data):
                    raise ValueError("Spotify returned an incomplete batch")
                response_data = response_data[index]
            if not first.done():
                first.set_result((response.request, operation, isinstance(request_data, list), response_data))
        except Exception as exc:  # noqa: BLE001 - settle the asynchronous capture safely
            if not first.done():
                first.set_exception(exc)

    def received(response: Any) -> None:
        task = asyncio.create_task(inspect(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        await page.route("**/v2/graph-pq*", guard)
        page.on("response", received)
        await page.goto(
            f"https://creators.spotify.com/interactivity/show/{show_id}/comments",
            wait_until="domcontentloaded", timeout=45000,
        )
        request, operation, batched, initial = await asyncio.wait_for(first, timeout=35)
        entries, token = _feed(initial)
        all_entries = list(entries)
        pages = 1
        tokens: set[str] = set()
        while token and pages < max_pages:
            if token in tokens:
                raise ValueError("Spotify repeated a feedback page token")
            tokens.add(token)
            body = copy.deepcopy(operation)
            body["variables"]["pageToken"] = token
            response = await context.request.fetch(
                request, data=[body] if batched else body, timeout=20000,
            )
            if response.status != 200:
                raise ValueError("Spotify feedback pagination failed")
            payload = await response.json()
            if batched:
                if not isinstance(payload, list) or len(payload) != 1:
                    raise ValueError("Spotify feedback pagination was incomplete")
                payload = payload[0]
            entries, token = _feed(payload)
            all_entries.extend(entries)
            pages += 1
        return {
            "platform": "spotify", "show_id": show_id, "language": show["language"],
            "status": "ok", "pages": pages, "truncated": bool(token),
            "comments": normalize(all_entries),
        }
    finally:
        await page.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def collect(config: dict[str, Any]) -> dict[str, Any]:
    # Import lazily: the release workflow itself has no Playwright dependency.
    from playwright.async_api import async_playwright

    sources: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(
                storage_state=str(Path(config["session"]).expanduser()), locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            try:
                for show in config["shows"]:
                    try:
                        sources.append(await collect_show(context, show, config["max_pages"]))
                    except Exception as exc:  # noqa: BLE001 - isolate errors per show
                        # No exception messages, URLs, headers, or comment text in logs.
                        sources.append({"platform": "spotify", "show_id": show["show_id"],
                                        "language": show["language"], "status": "error",
                                        "error_type": type(exc).__name__})
            finally:
                await context.close()
        finally:
            await browser.close()
    return {"format_version": 1, "collected_at": datetime.now(UTC).isoformat(),
            "sources": sources}


def save_private(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".podcast-feedback-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="private feedback configuration")
    parser.add_argument("--output", type=Path, required=True, help="private JSON snapshot")
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().resolve()
        config = load_config(config_path)
        output = args.output.expanduser().resolve()
        if output in {config_path, Path(config["session"]).expanduser().resolve()}:
            raise ValueError("snapshot cannot replace the configuration or session")
        snapshot = asyncio.run(collect(config))
        save_private(output, snapshot)
        for source in snapshot["sources"]:
            if source["status"] == "ok":
                suffix = "; sample truncated" if source["truncated"] else ""
                print(f"{source['language']}: {len(source['comments'])} comments{suffix}")
            else:
                print(f"{source['language']}: feedback unavailable ({source['error_type']})", file=sys.stderr)
        return 0 if all(source["status"] == "ok" for source in snapshot["sources"]) else 1
    except Exception as exc:  # noqa: BLE001 - do not leak session data from browser errors
        print(f"podcast-feedback: {type(exc).__name__}; feedback unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
