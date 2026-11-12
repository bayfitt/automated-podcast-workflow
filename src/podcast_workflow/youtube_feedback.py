"""Collect a private, bounded sample of public YouTube source-video comments."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from podcast_workflow.feedback import save_private

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
COMMENT_FAILURE = re.compile(
    r"(?im)^(?=.*(?:warning|error))(?=.*comment)(?=.*(?:failed|unable|error|incomplete|disabled|unavailable)).*$"
)


def normalize_comments(items: object, limit: int) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise TypeError("YouTube comments were not returned")
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("YouTube returned an invalid comment")
        comment_id, text = item.get("id"), item.get("text")
        if not isinstance(comment_id, str) or not comment_id:
            raise ValueError("YouTube comment identifier was missing")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("YouTube comment text was missing")
        if comment_id in seen:
            continue
        seen.add(comment_id)
        comments.append({
            "id": comment_id,
            "text": text,
            "parent_id": item.get("parent"),
            "created_at": item.get("timestamp"),
            "likes": item.get("like_count"),
            "author_is_uploader": item.get("author_is_uploader"),
        })
        if len(comments) == limit:
            break
    return comments


def collect_video(video_id: str, limit: int, executable: str = "yt-dlp") -> dict[str, Any]:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("expected an 11-character YouTube video ID")
    if not 1 <= limit <= 200:
        raise ValueError("comment limit must be from 1 to 200")
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Ignore local config, playlists and media downloads. The generated URL is
    # constrained to YouTube and passed as one argument, never through a shell.
    command = [
        executable, "--ignore-config", "--no-playlist", "--skip-download", "--write-comments",
        "--socket-timeout", "20", "--retries", "1", "--extractor-retries", "1",
        "--extractor-args", f"youtube:max_comments={limit},{limit},{limit},5,2",
        "--dump-single-json", "--", url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    if result.returncode != 0 or COMMENT_FAILURE.search(result.stderr):
        raise RuntimeError("YouTube comments could not be retrieved")
    info = json.loads(result.stdout)
    if not isinstance(info, dict) or info.get("id") != video_id:
        raise ValueError("YouTube returned an unexpected video")
    if not isinstance(info.get("comments"), list):
        raise TypeError("YouTube did not provide a comments list")
    comments = normalize_comments(info["comments"], limit)
    count = info.get("comment_count")
    if not comments and count != 0:
        raise ValueError("YouTube did not provide a verifiable empty comments list")
    reported_count = count if type(count) is int and count >= 0 else None
    truncated = len(info["comments"]) > limit or len(comments) >= limit
    if reported_count is not None and reported_count > len(comments):
        truncated = True
    return {
        "platform": "youtube", "video_id": video_id, "source_url": url,
        "status": "ok", "comment_count_reported": reported_count,
        "sample_limit": limit, "truncated": truncated, "comments": comments,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_ids", nargs="+", help="IDs of selected source videos")
    parser.add_argument("--output", type=Path, required=True, help="private JSON snapshot")
    parser.add_argument("--max-comments", type=int, default=100, help="maximum per video (1–200)")
    parser.add_argument("--yt-dlp", default="yt-dlp", help="yt-dlp executable")
    args = parser.parse_args(argv)
    if not 1 <= args.max_comments <= 200 or len(args.video_ids) > 10:
        parser.error("choose 1–200 comments and no more than 10 source videos")
    if len(set(args.video_ids)) != len(args.video_ids) or any(
        not VIDEO_ID.fullmatch(video_id) for video_id in args.video_ids
    ):
        parser.error("provide distinct 11-character YouTube video IDs")
    sources: list[dict[str, Any]] = []
    for video_id in args.video_ids:
        try:
            sources.append(collect_video(video_id, args.max_comments, args.yt_dlp))
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            # Never log raw comments, uploader metadata, or subprocess output.
            sources.append({"platform": "youtube", "video_id": video_id,
                            "status": "error", "error_type": type(exc).__name__})
    snapshot = {"format_version": 1, "collected_at": datetime.now(UTC).isoformat(),
                "sources": sources}
    try:
        save_private(args.output.expanduser().resolve(), snapshot)
    except OSError as exc:
        print(f"podcast-source-feedback: {type(exc).__name__}; snapshot unavailable", file=sys.stderr)
        return 2
    for source in sources:
        if source["status"] == "ok":
            suffix = "; sample may be incomplete" if source["truncated"] else ""
            print(f"{source['video_id']}: {len(source['comments'])} comments{suffix}")
        else:
            print(f"{source['video_id']}: comments unavailable ({source['error_type']})", file=sys.stderr)
    return 0 if all(source["status"] == "ok" for source in sources) else 1


if __name__ == "__main__":
    raise SystemExit(main())
