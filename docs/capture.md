# The capture address

A small address that adds a moment to your bank without signing in, for an iPhone shortcut (or anything else
that can send a web request): you hear something, press the shortcut, and Tastr keeps what was playing, where
in the track you were, your note, and a short recording if there was no match.

The site sits behind Cloudflare Access, which a shortcut can't get through, so the address lives under `/pub/`
(which Access lets through, [hosting-map.md](hosting-map.md#how-security-works)) and checks a key of its own.

## The key

A capture key can **only add** moments to one person's bank. It can't read, change or delete anything, and it
opens nothing else on the site: every other address still needs the Access login. Tastr keeps only each key's
SHA-256 hash, compares it in constant time, and never logs it.

Keys live in one setting, `TASTR_CAPTURE_KEYS` (a Worker secret, [settings.md](settings.md#worker-secrets)):
entries `email:key`, separated by commas or new lines. The email is whose bank the key adds to. A key has to be
at least 32 characters; shorter ones are left out, with a warning in the log that names the entry by its place
in the list (never its email or key). Make one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

On your own machine (not hosted) a bare `key` with no email adds to your one library.

To **rotate** a key: add a second entry for the same email with the new key, put the new key in the shortcut,
then remove the old entry. Both work in between. To **revoke** one, remove its entry. With no entries the address
answers `401` to everything.

## Request format

Both requests send the key as `Authorization: Bearer <key>`. A missing or wrong key gets `401` with
`{"detail": "..."}` and nothing else happens.

### `GET /pub/capture/now`

What the key's owner is playing on Spotify, so the shortcut can skip Shazam:

```json
{"playing": true, "title": "Pili Pili", "artist": "Polo & Pan", "position_s": 95.4,
 "uri": "https://open.spotify.com/track/...", "heard_on": "spotify"}
```

Spotify isn't connected yet, so for now this always answers `200` with `{"playing": false}`. Anything that goes
wrong once it is connected (nothing playing, Spotify not answering) will answer the same, so the shortcut has one
thing to check.

### `POST /pub/capture`

`multipart/form-data` (what a Shortcuts **Get Contents of URL** sends with a Form body), or
`application/x-www-form-urlencoded` without `audio` (handy with curl). Every field is optional except `heard_on`;
empty fields count as missing.

| Field | Meaning |
| --- | --- |
| `heard_on` | `spotify`, `shazam` or `recording` |
| `marked_at` | When the button was pressed, ISO 8601 with an offset (`2026-11-20T14:03:00-08:00`). Defaults to when the request arrived; without an offset it is read as UTC |
| `title`, `artist` | The match, if there was one (up to 300 characters each) |
| `position_s` | Seconds into the track at `marked_at` (Spotify only) |
| `link` | The match's link, such as a Spotify or Apple Music track link (`http` or `https`) |
| `note` | Your note (up to 4000 characters) |
| `audio` | A recording: m4a/AAC (what **Record Audio** makes), MP3 or WAV, at most `TASTR_CAPTURE_SECONDS` (60) seconds and `TASTR_CAPTURE_MB` (5) MB |
| `client` | What sent it, for debugging (`ios-shortcut/1`) |

It saves one **moment** in the key owner's bank:

- `title` is "Artist - Title" when there was a match, else "Recording, 20 Nov 2026, 14:03" (or "Capture, ..."
  with no recording either), in `marked_at`'s own time.
- `status` is `needs-source` when the capture names a track (a title, artist or link) whose audio isn't here yet,
  or has nothing at all to go on. A recording with no match is `ready`: the recording is the source.
- `source_kind` is `link` with `source_uri` the link, else `recording` when there is one.
- With `position_s`, the moment runs from 20 seconds before it to 10 seconds after (`start_s`, `end_s`); without
  it the range stays empty.
- `context` uses the bank's names: `via` (`heard_on`), `title`, `artist`, `at_s` (`position_s`), `site` (the
  link's host), `recording` (the stored file, `captures/<hash>/<hash>.m4a`, kept in the audio bucket), and
  `capture`, the fields as they were sent (all but the note).

The answer is `201` with `{"id": 42, "title": "Polo & Pan - Pili Pili", "status": "needs-source"}`. Otherwise:

| Status | Meaning |
| --- | --- |
| `401` | The key is missing or not one Tastr knows |
| `409` | The bank is full (20,000 moments and collections) |
| `413` | The recording is larger or longer than the limit |
| `422` | A field Tastr can't read: `heard_on` missing or unknown, a time or link it can't parse, an audio type it doesn't take |
| `429` | The day's cap is reached (`TASTR_DAILY_CAPTURES`, 100 per key owner per UTC day) |
| `507` | The server is at its storage cap (`TASTR_STORAGE_GB`), so it can't keep a recording; the same capture without one still works |

Each comes with `{"detail": "<a sentence to show>"}`.

Try it with curl (the key from your password manager, never pasted into a chat):

```bash
curl -s https://your.host/pub/capture -H "Authorization: Bearer $TASTR_CAPTURE_KEY" \
  -F heard_on=shazam -F title="Pili Pili" -F artist="Polo & Pan" -F note="that drop"
```

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `TASTR_CAPTURE_KEYS` | none (the address is shut) | the keys, `email:key` entries; a secret |
| `TASTR_CAPTURE_MB` | 5 | the largest recording |
| `TASTR_CAPTURE_SECONDS` | 60 | the longest recording |
| `TASTR_DAILY_CAPTURES` | 100 | captures per key owner per UTC day |
