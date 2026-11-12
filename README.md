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
