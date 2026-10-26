# Dayshelf

A digital bookshelf for the days you want to keep. Create journals, fill their
pages with photos, writing and drawings, and browse them like a personal memory book.

## Features

- Journal covers with editable names, dates and colors.
- Multiple pages per journal with photos, text and drawings.
- Drawing eraser and session-based undo/redo.
- Sideways page swipes and recoverable ripped pages.
- Protected text and drawing editors: page gestures and ripping are disabled while editing.
- Browser-based photo upload with JPEG, PNG and HEIC support.
- JSON backup download and validated imports that add copies without overwriting journals.

## Run locally

Requires Python 3.12. The pinned versions match the tested development environment.

```sh
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

Then install and run:

```sh
python -m pip install -r requirements.txt
python memory_album.py
```

Open **http://localhost:8550**. Keep the terminal running while using the app.
Use `--port 8560` to select another port or `--debug` for diagnostic logs.

### Test on a phone

```sh
python memory_album.py --lan
```

Connect the computer and phone to the same Wi-Fi and open the network address
printed in the terminal. Your firewall may require allowing local-network access.
`--lan` listens on all network interfaces; use it only on a trusted network.

## Using the journal

1. Create a journal from the bookshelf and open its cover.
2. Add a blank page or a photo page.
3. Choose Photos, Description or Drawing on a page.
4. In reading view, swipe sideways to turn pages. Swipe vertically on the margins
   to request ripping a page, then confirm. Restore ripped pages from Recently deleted.
5. In either editor, write or draw without page gestures interfering. Select
   Drawing eraser to remove parts of a drawing. Undo and redo remain available
   until the browser session ends or reloads.
6. Photo upload opens a separate browser tab. Choose a photo, upload it, then
   return to the original tab and select Keep photos.

## Storage and backups

Journals are stored in that browser on that device. There are no user accounts,
automatic device sync, or offline server support. Clearing site data or using a
different browser/address can make previous memories unavailable.

Photos are reduced to JPEG images with a maximum dimension of 700 pixels before
being saved. Exported backups therefore contain the reduced images, not the
original full-resolution photos. Browser storage has limits; export regularly.

Use Backups on the bookshelf to download a JSON file. Imports are validated and
require confirmation in the album tab before journals are added as separate copies.
Recently deleted pages are included in backups and retained until restored.
Keep backup files private: they contain your photos, descriptions and drawings.

Photo uploads are processed by the running Python server. Preview images and
upload/backup links are held temporarily in server memory and use opaque URLs;
they are not account-authenticated. Restarting the server invalidates these links.
This is a local prototype, not a production internet hosting configuration.

Existing legacy albums are migrated into a My memories journal, with a browser
storage recovery copy saved before migration. Storage keys and the backup file
format retain the original `memory-album` identifier for compatibility.

## Checks

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests cover backup validation and delivery, image routes, migration, drawing erasure
and undo/redo. Browser flows have been checked on desktop and at phone widths;
this does not replace testing on real iOS and Android devices. If an old tab stalls
after a server restart, reopen the app in a new tab.

## Project files

- `memory_album.py`: Flet interface, data operations and local HTTP routes.
- `requirements.txt`: pinned runtime dependencies.
- `requirements-dev.txt`: additional test dependencies.
- `tests/`: regression checks using Python's standard unittest framework.

The development process used OpenAI Codex for coding, debugging and testing
assistance. The app uses Flet, FastAPI, Uvicorn, Pillow and pillow-heif; their
respective licenses and trademarks remain with their owners.
