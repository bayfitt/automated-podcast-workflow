"""Publish prepared episodes in order, keeping private recovery checkpoints."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SUPPORTED_AUDIO = {".mp3", ".m4a", ".wav"}


@dataclass(frozen=True)
class Episode:
    language: str
    show_id: str
    title: str
    audio: Path
    description: Path
    artwork: Path | None
    resume_draft: str | None
    key: str


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a path")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return path


def load_manifest(path: Path) -> list[Episode]:
    path = path.expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("episodes"), list):
        raise TypeError("manifest must contain an episodes array")
    if not raw["episodes"]:
        raise ValueError("manifest has no episodes")
    episodes: list[Episode] = []
    seen: set[str] = set()
    for item in raw["episodes"]:
        if not isinstance(item, dict):
            raise TypeError("each episode must be an object")
        language, show_id, title = (item.get(x) for x in ("language", "show_id", "title"))
        if not isinstance(language, str) or not language.strip():
            raise ValueError("each episode needs a language")
        if not isinstance(show_id, str) or not ID_PATTERN.fullmatch(show_id):
            raise ValueError("each episode needs a valid show_id")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("each episode needs a title of at most 200 characters")
        audio = _file(item.get("audio"), path.parent, "audio")
        if audio.suffix.lower() not in SUPPORTED_AUDIO:
            raise ValueError("audio must be MP3, M4A, or WAV")
        description = _file(item.get("description"), path.parent, "description")
        if len(description.read_text(encoding="utf-8")) > 4000:
            raise ValueError("description exceeds 4000 characters")
        artwork = _file(item["artwork"], path.parent, "artwork") if item.get("artwork") else None
        draft = item.get("resume_draft")
        if draft is not None and (not isinstance(draft, str) or not ID_PATTERN.fullmatch(draft)):
            raise ValueError("resume_draft must be an episode ID")
        key = hashlib.sha256(f"{show_id}:{_digest(audio)}".encode()).hexdigest()
        if key in seen:
            raise ValueError("manifest repeats the same audio for the same show")
        seen.add(key)
        episodes.append(Episode(language, show_id, title, audio, description, artwork, draft, key))
    return episodes


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "meta" and data.get("property") in {"og:title", "og:url"}:
            self.values[str(data["property"])] = data.get("content") or ""


def public_page_matches(episode_id: str, title: str) -> tuple[bool, str]:
    if not ID_PATTERN.fullmatch(episode_id):
        return False, "invalid episode ID"
    url = f"https://open.spotify.com/episode/{episode_id}"
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=20) as response:
            final = urlparse(response.geturl())
            if response.status != 200 or final.hostname != "open.spotify.com":
                return False, "unexpected public response"
            parser = _Meta()
            parser.feed(response.read(2_000_000).decode("utf-8", errors="replace"))
    except HTTPError as exc:
        return False, f"public page returned HTTP {exc.code}"
    except (URLError, OSError, TimeoutError) as exc:
        return False, f"public page unavailable ({type(exc).__name__})"
    actual = " ".join(parser.values.get("og:title", "").split())
    canonical = urlparse(parser.values.get("og:url", ""))
    if actual != " ".join(title.split()):
        return False, "public title does not match"
    if canonical.hostname != "open.spotify.com" or canonical.path != f"/episode/{episode_id}":
        return False, "public episode URL does not match"
    return True, url


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".podcast-workflow-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _command(episode: Episode, uploader: str) -> list[str]:
    command = [
        uploader, "upload", str(episode.audio), "--title", episode.title,
        "--description-file", str(episode.description), "--show-id", episode.show_id,
    ]
    if episode.artwork:
        command += ["--thumbnail", str(episode.artwork)]
    if episode.resume_draft:
        command += ["--resume-draft", episode.resume_draft]
    return command


def publish(episodes: list[Episode], state_path: Path, uploader: str) -> bool:
    state_path = state_path.expanduser().resolve()
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another publishing run holds the state lock") from exc
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict) or not isinstance(state.setdefault("episodes", {}), dict):
            raise TypeError("invalid state file")
        entries = state["episodes"]
        for episode in episodes:
            entry = entries.get(episode.key)
            if entry is not None and entry.get("title") != episode.title:
                raise ValueError("the same show and audio already have a different recorded title")
            if entry is None:
                # Record intent before making a remote mutation. An interrupted run
                # needs human inspection instead of silently creating another draft.
                entry = {"title": episode.title, "show_id": episode.show_id, "status": "submitting"}
                entries[episode.key] = entry
                _save_state(state_path, state)
                try:
                    result = subprocess.run(
                        _command(episode, uploader), capture_output=True, text=True, timeout=3600,
                        check=False,
                    )
                    payload = json.loads(result.stdout)
                    if not isinstance(payload, dict):
                        raise TypeError("uploader returned an invalid result")
                    if result.returncode or payload.get("status") not in {
                        "published", "processing", "already_published"
                    } or payload.get("show_id") != episode.show_id:
                        raise ValueError("uploader did not confirm the requested show")
                    episode_id = payload.get("episode_id")
                    if not isinstance(episode_id, str) or not ID_PATTERN.fullmatch(episode_id):
                        raise ValueError("uploader returned no usable episode ID")
                    entry.update(status="submitted", episode_id=episode_id)
                    _save_state(state_path, state)
                except (OSError, subprocess.TimeoutExpired, TypeError, ValueError) as exc:
                    entry.update(status="needs_review", reason=type(exc).__name__)
                    _save_state(state_path, state)
                    print(f"{episode.language}: upload outcome needs review", file=sys.stderr)
                    return False
            if entry.get("status") in {"submitting", "needs_review"}:
                print(f"{episode.language}: inspect Spotify before retrying", file=sys.stderr)
                return False
            ok, detail = public_page_matches(str(entry.get("episode_id", "")), episode.title)
            entry.update(status="public_page_verified" if ok else "pending_public_page", detail=detail)
            _save_state(state_path, state)
            print(f"{episode.language}: {detail}")
            if not ok:
                return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "publish"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--state", type=Path, help="private checkpoint file")
    parser.add_argument("--uploader", default="spotify-uploader", help="uploader command path")
    args = parser.parse_args(argv)
    try:
        episodes = load_manifest(args.manifest)
        if args.action == "plan":
            for episode in episodes:
                print(f"{episode.language}: {episode.title} -> show {episode.show_id}")
            return 0
        state = args.state or args.manifest.with_name(".podcast-workflow-state.json")
        return 0 if publish(episodes, state, args.uploader) else 1
    except (OSError, TypeError, ValueError) as exc:
        print(f"podcast-workflow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
