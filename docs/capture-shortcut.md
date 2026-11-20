# Capturing a moment from an iPhone

One press on the iPhone marks what you are hearing and puts it in your bank as a **moment** deposit: the track,
where in it you were, and an optional note. This page is how to build the Shortcut by hand (a shortcut can't be
installed from the repository), and the request format the capture address takes.

> **Status: draft.** Nothing here has been built or tried on a phone yet. It
> is written from knowledge of Shortcuts, so expect to adjust action names and steps while testing. The steps
> marked *(check)* are the ones most likely to need it.

## What the shortcut does

1. **Starts from one press**: the Action Button, Back Tap, a Lock Screen control or Siri.
2. **Works out what is playing**, stopping at the first that answers:
   1. **Spotify**: the shortcut asks Tastr, and Tastr asks Spotify what is playing and how far in. This works
      only once Tastr has been signed in to Spotify; until then this step always comes back empty and is skipped.
   2. **Shazam**: the built-in **Recognize Music** action listens through the microphone. It gives the title and
      artist, not how far into the track you are, so the shortcut then records a short clip (10 seconds by
      default) that Tastr can later line up against the track to find the spot.
   3. **Recording**: if nothing recognised it, **Record Audio** keeps 30 seconds of the room for Tastr to
      identify later.
3. **Offers a note.** A text box opens; type, or tap the keyboard's microphone and speak, or tap Done to skip.
4. **Sends it** to Tastr's capture address and shows a notification with what was saved.

## Before you start

- iOS 17 or later, with the Shortcuts app.
- The **capture address** and your **capture key** ([capture.md](capture.md#the-key)). The key can only add
  deposits to your bank: it can't read, change or delete anything. Keep it out of chats,
  screenshots and shared shortcuts. If it leaks, rotate it (capture.md says how) and paste the new one into the shortcut.
- Microphone permission for Shortcuts (asked for on the first run).

## Build it

Open **Shortcuts**, tap **+**, and name the shortcut **Tastr capture**. Add the actions below in order. Text in
`code` is what you type into a field; **bold** names an action (search for it in the action list) or a variable.
To use a variable, long-press a field (or tap it and pick from the bar above the keyboard) and choose it under
*Variables*. Each **Set Variable** step names the output of the action before it.

### 1. Settings

| # | Action | Fields |
|---|---|---|
| 1 | **Text** | `https://tastr.pocketknows.com/pub/capture` |
| 2 | **Set Variable** | Name `Address` |
| 3 | **Text** | your capture key |
| 4 | **Set Variable** | Name `Key` |
| 5 | **Current Date** | |
| 6 | **Format Date** | Date format `ISO 8601`, Include time on *(check)* |
| 7 | **Set Variable** | Name `Marked` |

`Marked` is the moment you pressed, so a note that takes a minute to type doesn't move the mark.

### 2. Ask Spotify (through Tastr)

| # | Action | Fields |
|---|---|---|
| 8 | **Text** | **Address** then `/now` (no space), so it reads `…/pub/capture/now` |
| 9 | **Get Contents of URL** | URL: the Text above. Method `GET`. Headers: `Authorization` = `Bearer ` followed by **Key** |
| 10 | **Get Dictionary from Input** | |
| 11 | **Set Variable** | Name `Now` |
| 12 | **Get Dictionary Value** | Value for `playing` in **Now** |
| 13 | **If** | *Get Dictionary Value* is `true` *(check: the condition may read "is" with a text value; if so, compare to `true` as text, or switch to `has any value` on `title`)* |
| 14 | **Get Dictionary Value** ×4, each followed by **Set Variable** | `title` → `Title`, `artist` → `Artist`, `position_s` → `Position`, `uri` → `Link`; then a **Text** `spotify` → `HeardOn` |
| 15 | **Otherwise** | (step 3 goes here) |
| 16 | **End If** | (after step 3) |

If Spotify isn't set up, or nothing is playing, `playing` is false or missing and the shortcut moves on.

### 3. Shazam, then record (inside *Otherwise*)

| # | Action | Fields |
|---|---|---|
| 17 | **Recognize Music** | |
| 18 | **If** | *Recognize Music* `has any value` |
| 19 | **Get Details of Shazam Media** ×3 with **Set Variable** | `Title` → `Title`, `Artist` → `Artist`, `Apple Music URL` → `Link` |
| 20 | **Text** → **Set Variable** | `shazam` → `HeardOn` |
| 21 | **Record Audio** | Audio Quality `Normal`, Start Recording `Immediately`, Finish Recording `After Time`, `10` seconds |
| 22 | **Set Variable** | Name `Audio` |
| 23 | **Otherwise** | |
| 24 | **Record Audio** | as above, `30` seconds |
| 25 | **Set Variable** | Name `Audio` |
| 26 | **Text** → **Set Variable** | `recording` → `HeardOn` |
| 27 | **End If** | |

To skip the short clip after a Shazam match, delete actions 21 and 22. The deposit then has the track but not the
spot in it.

*(check)* If **Recognize Music** stops the whole shortcut when it hears nothing it knows, instead of returning
nothing, the recording fallback never runs. If that happens, move **Record Audio** (30 seconds) before **Recognize
Music** in the *Otherwise* branch, so a clip is always kept, and tell the thread so this page can be changed.

### 4. Note and send

| # | Action | Fields |
|---|---|---|
| 28 | **Ask for Input** | Input type `Text`, Prompt `Note (optional)`, Default answer empty |
| 29 | **Set Variable** | Name `Note` |
| 30 | **Get Contents of URL** | URL **Address**. Method `POST`. Headers: `Authorization` = `Bearer ` + **Key**. Request Body `Form`, with the fields in the table below |
| 31 | **Get Dictionary Value** | Value for `title` in *Contents of URL* |
| 32 | **Show Notification** | `Saved to Tastr: ` + *Dictionary Value* |

Form fields for action 30. Add them all: on a path where a variable was never set, Shortcuts sends the field empty,
and Tastr treats an empty field as missing.

| Field | Type | Value |
|---|---|---|
| `marked_at` | Text | **Marked** |
| `heard_on` | Text | **HeardOn** |
| `title` | Text | **Title** |
| `artist` | Text | **Artist** |
| `position_s` | Text | **Position** |
| `link` | Text | **Link** |
| `note` | Text | **Note** |
| `audio` | File | **Audio** |
| `client` | Text | `ios-shortcut/1` |

Don't add a `Content-Type` header: Shortcuts sets it, with the boundary, for a Form body.

### 5. One press

- **Action Button** (iPhone 15 Pro and later): Settings › Action Button › Shortcut › **Tastr capture**.
- **Back Tap**: Settings › Accessibility › Touch › Back Tap › Double Tap › **Tastr capture**.
- **Lock Screen**: edit the Lock Screen, tap a control slot, add **Shortcut** and pick it.
- **Siri**: say "Tastr capture".

*(check)* A locked phone may ask to unlock before **Record Audio** or **Ask for Input** runs.

## Test checklist

Run each and look for the deposit in the bank (`tastr deposit list`, or `/api/deposits` until the bank has a page).

- [ ] **Spotify on the phone**, mid-track: the deposit has the title, the artist and a position within a few
      seconds of where it was, and `heard_on` is `spotify`. Skips Shazam (no listening pause).
- [ ] **Spotify not set up, music from a speaker**: Shazam names it, a 10 second clip is kept, `heard_on` is
      `shazam`.
- [ ] **Something Shazam won't know** (humming, a live set blend): a 30 second clip is kept, `heard_on` is
      `recording`, and the deposit is titled "Recording, <date and time>".
- [ ] **Note typed**, **note dictated**, **note skipped** with Done: each saves; the skipped one has no note.
- [ ] **From the Lock Screen** and **from the Action Button** with the phone locked.
- [ ] **A wrong key** (change one character): nothing is saved and the shortcut shows an error.
- [ ] **No signal** (Airplane Mode): the shortcut shows an error. The moment is lost; note how often this
      happens, since a queue for later is possible.
- [ ] The notification names the track.

## Request format

The full format, the key and its limits are in [capture.md](capture.md). In short, the shortcut makes two
requests, both with `Authorization: Bearer <key>`:

- `GET /pub/capture/now` answers `{"playing": true, "title", "artist", "position_s", "uri", "heard_on"}`, or
  `{"playing": false}`. Until Spotify is connected it always answers `{"playing": false}`.
- `POST /pub/capture` takes the form fields above and saves one moment. A match is titled "Artist - Title"; a
  recording with no match is titled "Recording, 20 Nov 2026, 14:03" and is ready to work on, since the recording is
  the source. With `position_s` the moment runs from 20 seconds before to 10 seconds after. The answer is `201`
  with `{"id", "title", "status"}`; otherwise `401` (wrong key), `409` (the bank is full), `413` (recording too
  large or long: 60 seconds and 5 MB by default), `422` (a field Tastr can't read) or `429` (the day's cap), each
  with `{"detail": "<a sentence to show>"}`.
