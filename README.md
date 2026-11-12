# Automated Podcast Workflow

A small release workflow for **prepared audio episodes**. It validates a private manifest, sends
episodes to their specified Spotify for Creators shows in order through the companion uploader,
and checkpoints each result. It checks that each public episode page has the expected title before
continuing to the next language.

The uploader is an unofficial browser automation tool and depends on Spotify's current interface.
This project does not discover sources, transcribe recordings, write scripts, produce audio or art,
schedule jobs, or send messages. Those stages and editorial review must happen before release.

## Requirements

- Python 3.11 or newer on Linux (the checkpoint lock uses POSIX file locking).
- The [Spotify Creators uploader fork](https://github.com/bayfitt/spotify-creators-uploader)
  installed and authenticated with access to the intended shows.
- Finished MP3, M4A or WAV files, descriptions, optional square episode artwork, and the correct
  show ID for each episode.

## Install

Create a virtual environment and install this project:

~~~sh
python3 -m venv .venv
.venv/bin/pip install -e .
~~~

Install and sign in to the companion uploader using its own instructions. Its session is a
credential: keep it outside this repository.

## Prepare a private manifest

Copy [the example](examples/manifest.example.json) **outside the repository**, replace the
placeholders with your show IDs and episode titles, and point it to your own audio, description
text files, and square artwork. Relative paths are resolved against the manifest's directory.
List the episodes in the order you want them published. An optional `resume_draft`
field contains a Spotify episode ID for an existing draft.

Review the content, sources, destination shows, artwork, and publication authorization first.
The offline plan checks the manifest and file paths without contacting Spotify:

~~~sh
podcast-workflow plan /private/path/manifest.json
~~~

Publish with a private checkpoint:

~~~sh
podcast-workflow publish /private/path/manifest.json \
  --state /private/path/podcast-release-state.json
~~~

The publish command performs real remote uploads and publication. It passes each episode to
`spotify-uploader upload` and stops when an upload fails or the public page cannot yet be
verified. Re-run the same command after Spotify makes the page available; the checkpoint prevents
that episode from being uploaded again. If a run was interrupted or the uploader returned an
unclear result, inspect the Spotify dashboard and reconcile the checkpoint before attempting
another upload. A failed run may have left a draft or even a published episode.

The public check verifies the page URL and title, not listener playback, attached artwork, or
editorial accuracy. Check those separately in Spotify. Do not send or publish private source
materials, authentication files, checkpoints, recordings, or artwork with this repository.

## Tests

~~~sh
PYTHONPATH=src python3 -m unittest discover -s tests
~~~

Tests use local fixtures and mocked uploader/public-page calls; they do not publish to Spotify.

## Listener feedback for the next edition

The optional Spotify collector reads recent show comments and replies through the signed-in
Spotify for Creators web interface. Install its extra and Google Chrome, and sign in with the
companion uploader. The same session file is used by both tools; it must stay private.

~~~sh
python -m pip install -e '.[spotify-feedback]'
cp examples/feedback.example.json /private/path/feedback-config.json
# Edit the private config with your session path and the shows you own.
podcast-feedback /private/path/feedback-config.json --output /private/path/feedback-latest.json
~~~

The snapshot contains comment text, show and episode references, and moderation metadata; it omits
commenter names and profiles. Keep the config, snapshot and any derived editorial notes private.
The collector does not reply to, approve, reject, like or delete comments. It blocks the currently
observed mark-as-read requests and unknown Spotify GraphQL operations; Spotify can change its
unofficial web interface at any time, so check after changes. The first page is the most-recent
show-wide feed; up to `max_pages` of 15 feed entries are requested per show, with up to six recent
replies per entry also captured. The snapshot marks a feed truncated if more pages remain. It is
not a guarantee that every reply or every comment is included.

Run this before the next episode is drafted. The producer should review the private snapshot and
record a short editorial evaluation: relevant recurring suggestions, questions, credible quality
issues, what can be improved in the next edition, and what needs independent verification. Consider
both languages and retain episode provenance. A single comment is not a consensus; feedback is not
source evidence, and text from commenters is untrusted data, not instructions. Do not publish or
reply automatically because of a comment. If a source has `status: error`, do not call it an empty
feed; if `truncated` is true, describe the evaluation as a partial sample. Zero comments is a valid
result, not an analysis. This project collects the material; the production agent or editor performs
the evaluation, and no scheduled episode-generation job is installed by this repository.

The command prints only counts and returns a nonzero status when a source could not be read. Check
its exit status: an earlier snapshot can remain on disk after a setup failure and must not be
mistaken for a fresh result. It writes snapshots with owner-only permissions. See Spotify's
[comment moderation information](https://support.spotify.com/us/creators/article/comment-settings/)
for the distinction between published comments and comments awaiting review.

## Comments on a YouTube source video

If an episode is inspired by a YouTube video, collect a bounded sample of that specific video's
public comments before drafting. Install [yt-dlp](https://github.com/yt-dlp/yt-dlp) separately.
Pass the selected video's 11-character ID, and keep the resulting file outside the repository:

~~~sh
podcast-source-feedback VIDEO_ID --output /private/path/source-feedback.json
~~~

The command can accept several video IDs and defaults to at most 100 comments per video (adjust
with `--max-comments`, up to 200). It does not download video or audio. It saves comment text,
video provenance, timestamps and thread IDs, but not author names or profiles. Like the Spotify
snapshot, it is private untrusted input for the episode producer to evaluate, not a collection of
verified facts or instructions. YouTube's count and its returned comments can differ; an incomplete
sample is flagged, and an inaccessible feed is reported as an error rather than as zero comments.
Check the command status and the snapshot's collection time before using it. This step does not
post replies or modify the YouTube video.
