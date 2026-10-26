"""Dayshelf: a browser-based photo journal. See README.md for setup."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import secrets
import tempfile
import time
from copy import deepcopy
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import flet as ft
import flet.canvas as cv
from PIL import Image as PILImage, ImageOps

logger = logging.getLogger(__name__)

APP_BUILD = "Dayshelf 0.1.0"
browser_imports = {}
backup_sessions = {}
photo_previews = {}


def validate_backup(payload):
    if not isinstance(payload, dict) or payload.get("format") != "memory-album" or payload.get("version") != 1:
        raise ValueError("Choose a Memory Album backup exported by this app.")
    books = payload.get("journals")
    if not isinstance(books, list) or len(books) > 1000:
        raise ValueError("Invalid journal list.")
    for book in books:
        if not isinstance(book, dict) or not isinstance(book.get("pages"), list):
            raise ValueError("Invalid journal.")
        pages = list(book["pages"])
        deleted = book.get("deleted_pages", [])
        if not isinstance(deleted, list) or any(not isinstance(item, dict) or "page" not in item or not isinstance(item.get("index"), int) for item in deleted):
            raise ValueError("Invalid recovery data.")
        pages.extend(item["page"] for item in deleted)
        for item in [book, *pages]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
                raise ValueError("Invalid page text or identifier.")
            date = item.get("date")
            if not isinstance(date, list) or len(date) != 3 or any(not isinstance(part, str) for part in date):
                raise ValueError("Invalid page date.")
            datetime.strptime("/".join(date), "%d/%m/%Y")
            if not isinstance(item.get("title", ""), str) or item.get("cover_color", "#425B4B") not in ["#425B4B", "#8E5144", "#485B73", "#73586E", "#967342", "#496F70"]:
                raise ValueError("Invalid page details.")
            photos = item.get("photos", [])
            if not isinstance(photos, list):
                raise ValueError("Invalid photos.")
            for photo in photos:
                with PILImage.open(io.BytesIO(base64.b64decode(photo, validate=True))) as image:
                    image.verify()
            drawing = item.get("drawing", [])
            if not isinstance(drawing, list) or any(not isinstance(s, list) or len(s) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in s) for s in drawing):
                raise ValueError("Invalid drawing.")
            if type(item.get("font_size", 18)) not in (int, float) or not 10 <= item.get("font_size", 18) <= 40:
                raise ValueError("Invalid text size.")
    return deepcopy(books)


def near_segment(point, segment, radius=18):
    x, y = point
    a, b, c, d = segment
    length = (c-a)**2 + (d-b)**2
    t = max(0, min(1, ((x-a)*(c-a)+(y-b)*(d-b))/length)) if length else 0
    return (x-a-t*(c-a))**2 + (y-b-t*(d-b))**2 <= radius**2


class PageHistory:
    """Bounded edit history, shared across visits to a page in this session."""
    def __init__(self, entry):
        self.states = [self.snapshot(entry)]
        self.position = 0

    @staticmethod
    def snapshot(entry):
        return {"text": entry.get("text", ""), "drawing": deepcopy(entry.get("drawing", []))}

    def record(self, entry):
        state = self.snapshot(entry)
        if state == self.states[self.position]:
            return
        self.states = self.states[:self.position + 1] + [state]
        self.states = self.states[-60:]
        self.position = len(self.states) - 1

    def move(self, entry, delta):
        target = self.position + delta
        if 0 <= target < len(self.states):
            self.position = target
            entry.update(deepcopy(self.states[target]))


def migrate_journals(entries):
    legacy = [entry for entry in entries if "pages" not in entry]
    journals = [entry for entry in entries if "pages" in entry]
    if legacy:
        journals.insert(0, {"id": uuid4().hex, "title": "My memories", "date": list(legacy[0]["date"]),
            "text": "", "photos": [], "pages": legacy, "cover_color": legacy[0].get("cover_color", "#425B4B")})
    return journals


def photo_source(raw, base_url):
    # Opaque URLs let the browser fetch JPEGs without passing image bytes
    # through the Flet control protocol. Saved albums still contain the image.
    digest = hashlib.sha256(raw).hexdigest()
    now = time.monotonic()
    for key, item in list(photo_previews.items()):
        if now - item[2] > 86400:
            photo_previews.pop(key, None)
    item = next((item for item in photo_previews.values() if item[0] == digest), None)
    if item is None:
        token = secrets.token_urlsafe(32)
    else:
        token = item[3]
    photo_previews[token] = (digest, raw, now, token)
    origin = urlsplit(str(base_url))
    scheme = {"ws": "http", "wss": "https"}.get(origin.scheme, origin.scheme)
    return f"{scheme}://{origin.netloc}/photo-image/{token}"


def build_web_app():
    """Serve a standard HTML file input alongside the Flet interface."""
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    async def backup_route(request):
        item = backup_sessions.get(request.path_params["token"])
        if not item or time.monotonic() > item[0]:
            return HTMLResponse("Link expired. Reopen Backups from the bookshelf.", status_code=410)
        if request.method == "POST":
            if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
                return JSONResponse({"error": "Open Backups from your album."}, status_code=403)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 100 * 1024 * 1024:
                    return JSONResponse({"error": "Backup exceeds 100 MB."}, status_code=413)
            try:
                books = await asyncio.to_thread(validate_backup, json.loads(raw))
                await item[2](books)
            except Exception as exc:
                return JSONResponse({"error": str(exc) or "This backup could not be read."}, status_code=400)
            return JSONResponse({"ok": True, "count": len(books)})
        if "download" in request.query_params:
            return Response(json.dumps(item[1](), ensure_ascii=False).encode("utf-8"), media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="memory-album-backup.json"', "Cache-Control": "no-store"})
        return HTMLResponse('''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Memory Album backups</title>
<style>body{font:18px system-ui;max-width:480px;margin:auto;padding:24px;background:#ede5d8;color:#34473b}button,a{display:inline-block;padding:12px;margin:12px 0;background:#425b4b;color:white;border:0;border-radius:8px;font:inherit}input{max-width:100%;font:inherit}p{line-height:1.5}</style>
<h1>Keep a copy of your memories</h1><p>Backups include photos, text, drawings, and recently deleted pages. Store the file somewhere safe; it contains your private memories.</p>
<a href="?download=1" download>Download backup</a><h2>Import a backup</h2><p>Imported journals are added as copies. Existing journals stay unchanged.</p><input id="file" type="file" accept=".json,application/json"><button id="send">Check backup</button><p id="status" role="status"></p>
<script>const button=document.getElementById('send'),status=document.getElementById('status');button.onclick=async()=>{const file=document.getElementById('file').files[0];if(!file){status.textContent='Choose a backup file first.';return;}button.disabled=true;status.textContent='Checking backup…';try{const r=await fetch(location.pathname,{method:'POST',body:file,headers:{'Content-Type':'application/json'}});const data=await r.json();if(!r.ok)throw Error(data.error);status.textContent='Checked '+data.count+' journal(s). Return to the album tab to confirm importing.';}catch(e){status.textContent=e.message;}finally{button.disabled=false;}};</script>''', headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def importer(request):
        token = request.path_params["token"]
        item = browser_imports.get(token)
        if not item or time.monotonic() > item[0]:
            return HTMLResponse("This upload link expired. Return to the album and tap Add photos again.", status_code=410)
        if request.method == "POST":
            if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
                return JSONResponse({"error": "Open the upload page from your album."}, status_code=403)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 40 * 1024 * 1024:
                    return JSONResponse({"error": "Choose a photo smaller than 40 MB."}, status_code=413)
            try:
                encoded = await asyncio.to_thread(shrink, bytes(raw))
                await item[1](encoded)
            except Exception as exc:
                logger.debug(f'Browser photo import: {type(exc).__name__}: {exc}')
                return JSONResponse({"error": "Could not read this photo. Try a JPG, PNG, or HEIC photo."}, status_code=422)
            return JSONResponse({"ok": True, "preview": "data:image/jpeg;base64," + encoded})
        return HTMLResponse('''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add a memory photo</title><style>body{font:18px system-ui;background:#ede5d8;color:#34473b;padding:24px;max-width:480px;margin:auto}button,input{font:inherit;margin:16px 0;max-width:100%}button{background:#425b4b;color:white;padding:14px;border:0;border-radius:8px}p{line-height:1.5}</style></head><body>
<h1>Add a memory photo</h1><p>Choose one photo, then upload it. Your album stays open in the previous tab.</p>
<input id="photo" type="file" accept="image/*,.heic,.heif"><br><button id="send">Upload photo</button><p id="status" role="status"></p><img id="preview" alt="Uploaded photo preview" hidden style="max-width:100%;max-height:400px">
<script>const file=document.getElementById('photo'),button=document.getElementById('send'),status=document.getElementById('status');
button.onclick=async()=>{const photo=file.files[0];if(!photo){status.textContent='Choose a photo first.';return;}if(photo.size>40*1024*1024){status.textContent='Choose a photo smaller than 40 MB.';return;}button.disabled=true;status.textContent='Uploading photo… Keep this tab open.';
try{const response=await fetch(location.pathname,{method:'POST',body:photo,headers:{'Content-Type':'application/octet-stream'}});const result=await response.json();if(!response.ok)throw Error(result.error||'Upload failed.');const preview=document.getElementById('preview');preview.src=result.preview;preview.hidden=false;status.textContent='Photo ready! Return to your album tab and tap Keep photos.';file.value='';}catch(error){status.textContent=error.message+' You can retry.';}finally{button.disabled=false;}};</script></body></html>''', headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def image_response(request):
        item = photo_previews.get(request.path_params["token"])
        if item is None or time.monotonic() - item[2] > 86400:
            return Response(status_code=404)
        return Response(item[1], media_type="image/jpeg", headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    web = ft.run(main, export_asgi_app=True, no_cdn=True, upload_dir=str(UPLOAD_DIR))
    web.router.routes.insert(0, Route("/photo-import/{token}", importer, methods=["GET", "POST"]))
    web.router.routes.insert(0, Route("/photo-image/{token}", image_response, methods=["GET"]))
    web.router.routes.insert(0, Route("/backup/{token}", backup_route, methods=["GET", "POST"]))
    return web
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # JPG/PNG still work; HEIC requires pillow-heif.


# Uploaded originals are temporary and are never served as public assets.
_upload_temp = tempfile.TemporaryDirectory(prefix="memory-album-")
UPLOAD_DIR = Path(_upload_temp.name)
os.environ.setdefault("FLET_SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("FLET_MAX_UPLOAD_SIZE", str(40 * 1024 * 1024))

def shrink(raw: bytes) -> str:
    """Make the photo small (max 700px) and return it as text so it fits in the browser's storage."""
    im = ImageOps.exif_transpose(PILImage.open(io.BytesIO(raw))).convert("RGB")
    im.thumbnail((700, 700))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=70)
    return base64.b64encode(out.getvalue()).decode()


def pic(e, **kw):
    base_url = kw.pop("base_url", None)
    fallback = ft.Container(padding=12, bgcolor="#F3E7CF",
        content=ft.Text("This photo could not be displayed. Try adding it again.", color="#703B2D"))
    try:
        raw = base64.b64decode(e["photo"], validate=True)
    except (ValueError, TypeError, KeyError):
        return fallback
    return ft.Image(src=photo_source(raw, base_url) if base_url else raw,
                    error_content=fallback, **kw)


def span(t, p, seps, include_end=False):
    """Grow outward from position p until we hit a separator."""
    p = max(0, min(p, len(t)))
    a = b = p
    while a > 0 and t[a - 1] not in seps:
        a -= 1
    while b < len(t) and t[b] not in seps:
        b += 1
    if include_end and b < len(t):
        b += 1
    while a < b and t[a].isspace():
        a += 1
    return a, b


# Selector tap counts: word, clause, paragraph, sentence.
RULES = {1: (" \n.,!?;:", False), 2: (",.!?;\n", False), 3: ("\n", False), 4: (".!?\n", True)}


async def app(page: ft.Page):
    image_control = globals()["pic"]
    def pic(entry, **kwargs):
        return image_control(entry, base_url=page.url, **kwargs)
    page.title = "Dayshelf"
    page.padding = 8
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = "#EDE5D8"
    page.theme = ft.Theme(color_scheme_seed="#526B58")
    # Everything is saved inside each person's own browser, so every tester gets a private album.
    # Install callbacks BEFORE Flet automatically mounts the services.
    # Stable dispatchers keep the client subscriptions alive across page changes.
    result_handlers, upload_handlers = {}, {}

    async def new_result(event):
        logger.debug('Photo diagnostic: new-photo result received')
        handler = result_handlers.get("new")
        if handler:
            await handler(event)

    async def existing_result(event):
        logger.debug('Photo diagnostic: existing-page photo result received')
        handler = result_handlers.get("existing")
        if handler:
            await handler(event)

    async def new_upload(event):
        handler = upload_handlers.get("new")
        if handler:
            await handler(event)

    async def existing_upload(event):
        handler = upload_handlers.get("existing")
        if handler:
            await handler(event)

    prefs = ft.SharedPreferences()
    picker = ft.FilePicker(on_result=new_result, on_upload=new_upload)
    more_picker = ft.FilePicker(on_result=existing_result, on_upload=existing_upload)
    # Flet 1.0.5 automatically registers each Service in page._services.
    # Do not also add it to page.services (the separate root-view service list).
    retry_load = asyncio.Event()

    def retry_storage(_):
        retry_load.set()

    while True:
        retry_load.clear()
        page.controls.clear()
        page.add(ft.Text("Loading your saved album…"), ft.Text(APP_BUILD, size=12))
        page.update()
        try:
            raw_album = await asyncio.wait_for(prefs.get("album"), timeout=30)
            # Only a confirmed missing key represents a new album.
            entries = [] if raw_album is None else json.loads(raw_album)
            if not isinstance(entries, list):
                raise ValueError("Saved album is not a list. Existing data was not changed.")
            for entry in entries:
                if not isinstance(entry, dict) or not all(key in entry for key in ("id", "date", "text")):
                    raise ValueError("A saved entry has an unexpected format. Existing data was not changed.")
            break
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                detail = "Browser storage did not respond within 30 seconds."
            else:
                detail = f"{type(exc).__name__}: {exc}"
            logger.warning(f'Album load failed: {detail}')
            page.controls.clear()
            page.add(ft.Column([
                ft.Text("Your saved album could not be opened", size=22),
                ft.Text("No memories have been changed. Keep using this browser and address."),
                ft.Text(detail, selectable=True),
                ft.FilledButton("Retry loading", on_click=retry_storage),
                ft.Text(f"{APP_BUILD} · If Retry fails, send the error shown above.", size=12),
            ], tight=True))
            await retry_load.wait()

    for entry in entries:
        if "pages" in entry:
            if not isinstance(entry["pages"], list) or any(
                not isinstance(p, dict) or not all(k in p for k in ("id", "date", "text", "photos"))
                for p in entry["pages"]):
                raise ValueError("A journal has invalid pages. Saved data was not changed.")
        if "photos" not in entry:
            entry["photos"] = [entry.pop("photo")] if entry.get("photo") else []

    if any("pages" not in entry for entry in entries):
        # Keep a recovery copy before changing the browser's saved structure.
        if await prefs.get("album_before_journals") is None:
            if await prefs.set("album_before_journals", raw_album) is False:
                raise RuntimeError("Could not back up existing memories before migration")
        entries = migrate_journals(entries)
        if await prefs.set("album", json.dumps(entries)) is False:
            raise RuntimeError("Could not save the journal migration")
    histories = {}

    def owner(memory):
        return next(book for book in entries if any(p is memory for p in book["pages"]))

    save_status = ft.Text("Stored on this device", size=11, color="#817867")
    save_lock = asyncio.Lock()

    async def persist():
        async with save_lock:
            try:
                result = await prefs.set("album", json.dumps(entries))
                if result is False:
                    raise RuntimeError("Storage rejected the save")
                save_status.value = "All changes saved on this device"
                save_status.color = "#817867"
                page.update()
            except Exception:
                save_status.value = "Changes could not be saved"
                save_status.color = "#9C4337"
                page.show_dialog(ft.SnackBar(ft.Text("Could not save. Your browser storage may be full. Keep this window open.")))

    def save():
        save_status.value = "Saving…"
        page.run_task(persist)

    def ask(content, yes, no=None):
        def close(fn):
            def h(e):
                page.pop_dialog()
                fn and fn()
            return h
        page.show_dialog(ft.AlertDialog(
            content=ft.Text(content) if isinstance(content, str) else content,
            actions=[ft.TextButton("Yes", on_click=close(yes)), ft.TextButton("No", on_click=close(no))]))

    photo_status = ft.Text(f"{APP_BUILD} · Ready to select a photo", size=12, color="#59402B")

    def photo_stage(message):
        photo_status.value = f"{APP_BUILD} · {message}"
        logger.debug(f'Photo diagnostic: {message}')
        page.update()


    def photo_diagnostics(_):
        versions = []
        for name in ("flet", "flet-web", "pillow", "pillow-heif"):
            try:
                versions.append(f"{name}: {version(name)}")
            except PackageNotFoundError:
                versions.append(f"{name}: not installed")
        page.show_dialog(ft.AlertDialog(title=ft.Text("Photo troubleshooting"),
            content=ft.Text(photo_status.value + "\n\n" + "\n".join(versions), selectable=True),
            actions=[ft.TextButton("Close", on_click=lambda _: page.pop_dialog())]))

    # A bounded root prevents the scrollable photo/journal area collapsing.
    screen = ft.Container(bgcolor="#EDE5D8", padding=0)
    journal_paper = {"frame": None}

    def size_screen(_=None):
        screen.width = max(240, (page.width or 400) - 16)
        screen.height = max(240, (page.height or 700) - 16)
        if journal_paper["frame"] is not None:
            journal_paper["frame"].margin = ft.Margin.symmetric(
                horizontal=max(0, ((page.width or 400) - 940) / 2))
        if screen in page.controls:
            page.update()

    page.on_resize = size_screen

    def show(*controls):
        size_screen()
        screen.content = ft.Column([
            *controls,
            ft.Row([save_status, ft.IconButton(ft.Icons.INFO_OUTLINE, icon_size=16,
                icon_color="#817867", tooltip="App details and photo help", on_click=photo_diagnostics)],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN, height=30)
        ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        if not any(control is screen for control in page.controls):
            page.controls.clear()
            page.add(screen)
        else:
            page.update()

    def label(e):
        return "/".join(e["date"])

    upload_lock = asyncio.Lock()

    async def receive_photos(files, service, keep):
        if not files:
            photo_stage("The chooser returned no files. Selection was cancelled or lost.")
            logger.debug('Photo import: chooser returned no files.')
            return
        photo_stage(f"Selection received: {len(files)} photo(s).")
        # Safari can stall while the web picker constructs a read stream.
        # On iOS request bytes instead, and bypass the separate HTTP upload.
        if all(getattr(file, "bytes", None) is not None for file in files):
            async with upload_lock:
                photo_stage("Preparing your selected photo…")
                await choose_photos(files, keep)
            return
        if upload_lock.locked():
            return
        async with upload_lock:
            status = ft.Text("Starting photo upload…")
            progress = ft.ProgressBar(value=0)
            dialog = ft.AlertDialog(modal=True, title=ft.Text("Adding your photos"),
                content=ft.Column([status, progress,
                    ft.Text("Keep this page open while your photos upload.", size=12)], tight=True, width=300))
            page.show_dialog(dialog)
            received, errors = [], []
            pending = {"future": None, "name": None}

            async def on_upload(event):
                future = pending["future"]
                if future is None or future.done() or event.file_name != pending["name"]:
                    return
                if event.error:
                    future.set_exception(RuntimeError(event.error))
                elif event.progress is not None:
                    progress.value = event.progress
                    page.update()
                    if event.progress >= 1:
                        future.set_result(None)

            upload_key = "new" if service is picker else "existing"
            upload_handlers[upload_key] = on_upload
            page.update()  # Register the progress handler before starting uploads.
            try:
                for index, file in enumerate(files):
                    photo_stage(f"Uploading photo {index + 1} of {len(files)}…")
                    status.value = f"Uploading photo {index + 1} of {len(files)}…"
                    progress.value = 0
                    page.update()
                    destination = UPLOAD_DIR / (uuid4().hex + ".photo")
                    future = asyncio.get_running_loop().create_future()
                    pending.update(future=future, name=file.name)
                    try:
                        if file.size > 40 * 1024 * 1024:
                            raise ValueError("Photo exceeds the 40 MB upload limit.")
                        url = page.get_upload_url(destination.name, 180)
                        request = ft.FilePickerUploadFile(name=file.name, upload_url=url, id=file.id)
                        await asyncio.wait_for(service.upload([request]), timeout=90)
                        await asyncio.wait_for(future, timeout=90)
                        status.value = "Preparing photo…"
                        page.update()
                        raw = await asyncio.to_thread(destination.read_bytes)
                        # Compress immediately so large originals do not accumulate in memory.
                        encoded = await asyncio.to_thread(shrink, raw)
                        received.append(SimpleNamespace(bytes=base64.b64decode(encoded)))
                        logger.debug(f'Photo import: HTTP upload {index + 1} completed.')
                    except Exception as exc:
                        message = str(exc) or "Upload timed out. Check the phone's connection to the computer."
                        errors.append(message)
                        logger.debug(f'Photo import: {type(exc).__name__}: {message}')
                    finally:
                        if not future.done():
                            future.cancel()
                        elif not future.cancelled():
                            future.exception()  # Retrieve a reported upload exception.
                        pending["future"] = None
                        destination.unlink(missing_ok=True)
            finally:
                upload_handlers.pop(upload_key, None)
                page.pop_dialog()
            if received:
                await choose_photos(received, keep, len(errors))
            else:
                photo_stage("Upload failed. See the error dialog.")
                message = "No photos were added. " + (errors[0] if errors else "No image data arrived.")
                message += " If this is an HEIC image, install pillow-heif on the computer, then restart."
                page.show_dialog(ft.AlertDialog(title=ft.Text("Photo upload failed"),
                    content=ft.Text(message), actions=[ft.TextButton("OK", on_click=lambda _: page.pop_dialog())]))

    async def choose_photos(files, keep, upload_failures=0):
        def notice(message):
            page.show_dialog(ft.AlertDialog(title=ft.Text("Photo import"),
                content=ft.Text(message), actions=[ft.TextButton("OK", on_click=lambda _: page.pop_dialog())]))
        if not files:
            logger.debug('Photo import: picker returned no files (cancelled or unavailable).')
            return
        logger.debug(f'Photo import: received {len(files)} file(s).')
        photos, failed = [], upload_failures
        for file in files:
            raw = file.bytes
            if not raw:
                failed += 1
                logger.debug('Photo import: a selected file contained no bytes.')
                continue
            try:
                photos.append(await asyncio.to_thread(shrink, raw))
            except Exception as exc:
                failed += 1
                logger.debug(f'Photo import: could not decode {len(raw)} bytes: {type(exc).__name__}: {exc}')
        if not photos:
            notice("The selected photo could not be read. For iPhone HEIC photos, install pillow-heif on your computer and restart the app. Try one JPG or PNG screenshot to check the upload. Try another image if the problem continues.")
            return
        logger.debug(f'Photo import: prepared {len(photos)} photo(s); waiting for Keep photos.')
        photo_stage("Preview ready. Tap Keep photos to add them.")
        def accept(_):
            photo_stage("Photos accepted.")
            page.pop_dialog()
            keep(photos)
        page.show_dialog(ft.AlertDialog(title=ft.Text("Keep your photos?"),
            content=ft.Column([
                ft.Row([pic({"photo": photo}, width=100, height=100, fit=ft.BoxFit.COVER)
                        for photo in photos], wrap=True),
                ft.Text(f"{len(photos)} photo(s) ready to add."),
                ft.Text(f"{failed} photo(s) could not be read. You can retry those separately.")
                    if failed else ft.Container(),
            ], tight=True, scroll=ft.ScrollMode.AUTO, width=320),
            actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                     ft.FilledButton("Keep photos", on_click=accept)]))

    def new_entry(photos=None, commit=True, book=None):
        d = time.localtime()
        entry = {"id": uuid4().hex,
                 "date": [f"{d.tm_mday:02d}", f"{d.tm_mon:02d}", str(d.tm_year)],
                 "photos": photos or [], "text": ""}
        if commit:
            if book is None:
                book = {"id": uuid4().hex, "title": "New journal", "date": list(entry["date"]),
                        "text": "", "photos": [], "pages": []}
                entries.append(book)
            book["pages"].append(entry)
            save()
        return entry

    def blank_journal(_):
        entry = new_entry(commit=False)
        entry["pages"] = []
        def start():
            entries.append(entry)
            save()
            journal_pages(entry)
        edit_details(entry, after=start)

    async def add(event):
        def keep(photos):
            open_page(new_entry(photos))
        await receive_photos(event.files, picker, keep)

    result_handlers["new"] = add
    def browser_photo_action(service):
        key = "new" if service is picker else "existing"
        handler = result_handlers.get(key)
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        for old_token, item in list(browser_imports.items()):
            if now > item[0]:
                browser_imports.pop(old_token, None)
        async def deliver(encoded):
            if not handler:
                raise RuntimeError("Album page is no longer available")
            await handler(SimpleNamespace(files=[SimpleNamespace(bytes=base64.b64decode(encoded))]))
        browser_imports[token] = (now + 24 * 60 * 60, deliver)
        return ft.OpenUrl(f"/photo-import/{token}", target=ft.UrlTarget.BLANK)

    def photo_action(service):
        # One consistent browser importer also works with iPhone camera photos.
        return browser_photo_action(service)

    def shelf():
        groups = {}
        for entry in entries:
            entry["photos"] = next((p["photos"][:1] for p in entry["pages"] if p["photos"]), [])
            entry["text"] = f"{len(entry['pages'])} pages"
            groups.setdefault(label(entry), []).append(entry)

        def date_key(item):
            try:
                return datetime.strptime(item[0], "%d/%m/%Y")
            except ValueError:
                return datetime.min

        expanded = {"cover": None, "collapse": None}

        def book(entry, number, index):
            colors = ["#425B4B", "#8E5144", "#485B73", "#73586E", "#967342", "#496F70"]
            color = entry.get("cover_color", colors[index % len(colors)])
            title = entry.get("title") or f"Memory {number}"
            height = [246, 232, 256, 240][index % 4]
            pinned = False
            spine_width = 52
            spine = ft.Column([
                ft.Container(height=3, bgcolor="#C9AC73"),
                ft.Text(entry["date"][2], size=10, color="#E7D5AB"),
                ft.Container(expand=True, alignment=ft.Alignment.CENTER,
                    content=ft.RotatedBox(quarter_turns=1,
                        content=ft.Text(title, size=15, font_family="Georgia", weight=ft.FontWeight.BOLD,
                            color="#FFF4DA", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))),
                ft.Icon(ft.Icons.MENU_BOOK, color="#D8C198", size=18),
                ft.Container(height=3, bgcolor="#C9AC73"),
            ], spacing=5, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
            detail = ft.Column([
                ft.Row([ft.Text(title, size=19, color="#FFF4DA", expand=True,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.IconButton(ft.Icons.CLOSE, icon_color="#FFF4DA", tooltip="Close book",
                                      on_click=lambda _: collapse())]),
                ft.Text(label(entry), size=12, color="#E7D5AB"),
                ft.Container(height=65, content=pic({"photo": entry["photos"][0]}, height=65,
                    fit=ft.BoxFit.CONTAIN)) if entry["photos"] else ft.Text("Your story starts here", color="#E7D5AB"),
                ft.Text((entry.get("text") or "No journal written yet")[:90], size=12,
                        color="#FFF4DA", max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row([ft.TextButton("Open journal", on_click=lambda _: journal_pages(entry), style=ft.ButtonStyle(color="#FFF4DA")),
                        ft.TextButton("Edit cover", on_click=lambda _: edit_details(entry), style=ft.ButtonStyle(color="#FFF4DA"))], spacing=0, wrap=True),
            ], spacing=4)
            cover = ft.Container(width=spine_width, height=height, bgcolor=color,
                animate_size=ft.Animation(250, ft.AnimationCurve.EASE_OUT),
                border_radius=5, padding=8,
                border=ft.Border.only(left=ft.BorderSide(6, "#33000000"), right=ft.BorderSide(3, "#D6C5A4")),
                tooltip=f"{title} · {label(entry)} · hover to preview, click to keep open", content=spine)

            def collapse():
                nonlocal pinned
                pinned = False
                cover.width = spine_width
                cover.height = height
                cover.content = spine
                if expanded["cover"] is cover:
                    expanded.update(cover=None, collapse=None)
                page.update()

            def expand(_):
                if expanded["cover"] is cover:
                    return
                if expanded["collapse"]:
                    expanded["collapse"]()
                cover.width = min(320, max(230, (page.width or 400) - 100))
                cover.height = 300
                cover.content = detail
                expanded.update(cover=cover, collapse=collapse)
                page.update()

            def click(event):
                nonlocal pinned
                pinned = True
                expand(event)

            def hover(event):
                if str(event.data).lower() == "true":
                    expand(event)
                elif not pinned and expanded["cover"] is cover:
                    collapse()

            cover.on_click = click
            cover.on_hover = hover
            return cover

        def wooden_shelf(date, pages, offset):
            books = [book(entry, i + 1, offset + i) for i, entry in enumerate(pages)]
            contents = ft.Row(books, spacing=4, vertical_alignment=ft.CrossAxisAlignment.END,
                              scroll=ft.ScrollMode.AUTO, height=312)
            if not pages:
                contents = ft.Container(height=170, alignment=ft.Alignment.CENTER,
                    content=ft.Text("A little room for your next memory", color="#E1C8A6", italic=True))
            return ft.Column([
                ft.Container(padding=ft.Padding.only(left=16, right=16, top=12),
                    content=ft.Row([
                        ft.Container(bgcolor="#E9D9B8", padding=ft.Padding.symmetric(horizontal=12, vertical=5),
                            border_radius=3, content=ft.Text(date, size=12, color="#59402B", weight=ft.FontWeight.BOLD)),
                        ft.Text(f"{len(pages)} journal{'s' if len(pages) != 1 else ''}" if pages else "", size=11, color="#D9BC96")
                    ])),
                ft.Container(padding=ft.Padding.only(left=16, right=16), content=contents),
                ft.Container(height=7, bgcolor="#C49560"),
                ft.Container(height=17, bgcolor="#946139", border=ft.Border.only(bottom=ft.BorderSide(3, "#533822"))),
                ft.Container(height=7, bgcolor="#513722"),
            ], spacing=0, tight=True)

        rows = []
        offset = 0
        for date, pages in sorted(groups.items(), key=date_key, reverse=True):
            rows.append(wooden_shelf(date, pages, offset))
            offset += len(pages)
        if not rows:
            rows.append(wooden_shelf("YOUR FIRST CHAPTER", [], 0))
        rows.append(wooden_shelf("MORE MEMORIES TO COME", [], offset))
        cabinet = ft.Container(bgcolor="#6D4B32", border_radius=8,
            border=ft.Border.all(12, "#825735"),
            content=ft.Column(rows, spacing=0, tight=True))
        show(ft.Row([
                 ft.Column([ft.Text("The memory bookshelf", size=28, font_family="Georgia",
                                   weight=ft.FontWeight.BOLD, color="#3E483A"),
                            ft.Text("Your days, bound in little journals.", size=13, color="#796E5C")], spacing=3, tight=True),
                 ft.Row([
                     ft.FilledButton("New journal", icon=ft.Icons.MENU_BOOK, on_click=blank_journal),
                     ft.TextButton("Backups", icon=ft.Icons.SAVE_ALT, action=backup_action()),
                     ft.TextButton("Recently deleted", icon=ft.Icons.RESTORE, on_click=lambda _: recently_deleted()),
                 ], wrap=True)
             ], wrap=True, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
             ft.Text("Hover over a spine to see its cover. Click or tap to keep it open and edit your journal.", size=12, color="#796E5C"),
             ft.ListView([cabinet], expand=True, padding=8),
             ft.Text("A place for the days you want to keep.", size=12, color="#796E5C", italic=True))

    def edit_details(entry, after=None):
        title = ft.TextField(label="Journal title", value=entry.get("title", ""), max_length=60)
        date = ft.TextField(label="Date (DD/MM/YYYY)", value=label(entry))
        colors = ["#425B4B", "#8E5144", "#485B73", "#73586E", "#967342", "#496F70"]
        selected = {"color": entry.get("cover_color", colors[0])}
        swatches = []
        def choose_color(color):
            selected["color"] = color
            for swatch in swatches:
                swatch.border = ft.Border.all(3, "#E3BA54" if swatch.bgcolor == color else "#FFFFFF")
            page.update()
        for color in colors:
            swatches.append(ft.Container(width=36, height=36, bgcolor=color, border_radius=18,
                border=ft.Border.all(3, "#E3BA54" if color == selected["color"] else "#FFFFFF"),
                tooltip=color, on_click=lambda _, c=color: choose_color(c)))
        def commit(_):
            try:
                parsed = datetime.strptime(date.value.strip(), "%d/%m/%Y")
            except ValueError:
                date.error_text = "Enter a valid date, such as 21/10/2026."
                page.update()
                return
            entry.update(title=title.value.strip(), date=parsed.strftime("%d/%m/%Y").split("/"),
                         cover_color=selected["color"])
            save()
            page.pop_dialog()
            (after or shelf)()
        page.show_dialog(ft.AlertDialog(title=ft.Text("Make this journal yours"),
            content=ft.Column([title, date, ft.Text("Cover color"), ft.Row(swatches, wrap=True),
                ft.Text("Changing the date moves this book to that day's shelf. Your first photo is its cover preview.", size=12)],
                tight=True, width=340),
            actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                     ft.FilledButton("Save changes", on_click=commit)]))

    def delete(e):
        book = owner(e)
        index = next(i for i, p in enumerate(book["pages"]) if p is e)
        book.setdefault("deleted_pages", []).append({"page": e, "index": index,
            "deleted_at": datetime.now().isoformat()})
        book["pages"].remove(e)
        save()
        journal_pages(book)

    def backup_action():
        token = secrets.token_urlsafe(32)
        for old, item in list(backup_sessions.items()):
            if time.monotonic() > item[0]:
                backup_sessions.pop(old, None)
        async def confirm_import(books):
            def commit():
                for book in books:
                    book["id"] = uuid4().hex
                    for memory in book["pages"] + [item["page"] for item in book.get("deleted_pages", [])]:
                        memory["id"] = uuid4().hex
                    book["title"] = (book.get("title") or "My journal") + " (imported)"
                entries.extend(books)
                save()
                shelf()
            ask(f"Add {len(books)} journal(s) from this backup as copies? Your current journals will stay unchanged.", commit)
        backup_sessions[token] = (time.monotonic()+86400,
            lambda: {"format": "memory-album", "version": 1, "journals": deepcopy(entries)}, confirm_import)
        return ft.OpenUrl(f"/backup/{token}", target=ft.UrlTarget.BLANK)

    def recently_deleted():
        rows = []
        for book in entries:
            for item in book.get("deleted_pages", []):
                memory = item["page"]
                def restore(book=book, item=item):
                    book["pages"].insert(min(max(0, item["index"]), len(book["pages"])), item["page"])
                    book["deleted_pages"].remove(item)
                    save()
                    recently_deleted()
                rows.append(ft.Container(bgcolor="#FFF9ED", padding=14, border_radius=6,
                    content=ft.Row([ft.Column([
                        ft.Text(memory.get("title") or "Untitled page", weight=ft.FontWeight.BOLD),
                        ft.Text(f"{book.get('title') or 'My journal'} · {label(memory)}", size=12),
                    ], expand=True), ft.TextButton("Restore", icon=ft.Icons.RESTORE, on_click=lambda _, fn=restore: fn())])))
        show(ft.TextButton("Bookshelf", icon=ft.Icons.ARROW_BACK, on_click=lambda _: shelf()),
             ft.Text("Recently deleted", size=28, font_family="Georgia"),
             ft.Text("Ripped pages stay here until you restore them. Photos, text and drawings are kept.", size=13, color="#796E5C"),
             ft.ListView(rows or [ft.Text("No ripped pages. Your memories are all in their journals.")], expand=True, spacing=10))

    def journal_pages(book):
        async def add_to_book(event):
            await receive_photos(event.files, picker,
                lambda photos: page_menu(new_entry(photos, book=book)))
        result_handlers["new"] = add_to_book
        cards = []
        for index, memory in enumerate(book["pages"]):
            preview = pic({"photo": memory["photos"][0]}, width=90, height=80, fit=ft.BoxFit.COVER) if memory["photos"] else ft.Icon(ft.Icons.DESCRIPTION, size=36)
            cards.append(ft.Container(bgcolor="#FFF9ED", padding=16, border_radius=6,
                on_click=lambda _, memory=memory: page_menu(memory),
                content=ft.Row([preview, ft.Column([
                    ft.Text(memory.get("title") or f"Page {index + 1}", size=20, weight=ft.FontWeight.BOLD),
                    ft.Text(label(memory), size=12),
                    ft.Text(memory["text"][:80] or "Photos, words, and little drawings", max_lines=2),
                ], expand=True)])))
        show(ft.Row([ft.TextButton("Bookshelf", icon=ft.Icons.ARROW_BACK, on_click=lambda _: shelf()),
                     ft.TextButton("Edit journal cover", on_click=lambda _: edit_details(book, after=lambda: journal_pages(book)))], wrap=True),
             ft.Text(book.get("title") or "My journal", size=28, font_family="Georgia"),
             ft.Text(f"{len(book['pages'])} page{'s' if len(book['pages']) != 1 else ''} · Choose a memory", size=13, color="#796E5C"),
             ft.ListView(cards or [ft.Text("Your journal is ready for its first page.")], expand=True, spacing=10),
             ft.Row([ft.FilledButton("New page", icon=ft.Icons.ADD, on_click=lambda _: page_menu(new_entry(book=book))),
                     ft.TextButton("Add a photo page", icon=ft.Icons.ADD_A_PHOTO, action=photo_action(picker))], wrap=True))

    def page_menu(memory):
        book = owner(memory)
        number = next(i for i, p in enumerate(book["pages"]) if p is memory) + 1
        show(ft.TextButton("Back to journal", icon=ft.Icons.ARROW_BACK, on_click=lambda _: journal_pages(book)),
             journal_frame(memory, ft.ListView([
                 ft.Text(memory.get("title") or f"Page {number}", size=28, font_family="Georgia", color="#425B4B"),
                 ft.Text(label(memory), size=12, color="#796E5C"),
                 ft.Row([
                     ft.TextButton("Photos", icon=ft.Icons.PHOTO, on_click=lambda _: open_page(memory)),
                     ft.TextButton("Description", icon=ft.Icons.DESCRIPTION, on_click=lambda _: journal(memory)),
                     ft.TextButton("Drawing", icon=ft.Icons.EDIT, on_click=lambda _: journal(memory, drawing=True)),
                     ft.IconButton(ft.Icons.MORE_HORIZ, tooltip="Page title and date", on_click=lambda _: edit_details(memory, after=lambda: page_menu(memory))),
                 ], wrap=True),
                 *[pic({"photo": photo}, height=220, fit=ft.BoxFit.CONTAIN) for photo in memory["photos"]],
                 ft.Text(memory["text"] or "A fresh page for your memories…", size=18, font_family="Georgia"),
                 ft.Container(height=220, content=cv.Canvas([
                     cv.Line(*segment, paint=ft.Paint(color="#56504A", stroke_width=3))
                     for segment in memory.get("drawing", [])])) if memory.get("drawing") else ft.Container(),
             ], expand=True, spacing=16, padding=16), page_menu))

    def journal_frame(memory, content, turn, editing=False):
        book = owner(memory)
        pages = book["pages"]
        index = next(i for i, p in enumerate(pages) if p is memory)

        turning = {"busy": False}
        async def animate_turn(delta):
            try:
                frame.opacity = 0.25
                frame.offset = ft.Offset(-0.04 if delta > 0 else 0.04, 0)
                page.update()
                await asyncio.sleep(0.14)
                turn(pages[index+delta])
                incoming = journal_paper["frame"]
                incoming.opacity = 0.4
                incoming.offset = ft.Offset(0.04 if delta > 0 else -0.04, 0)
                page.update()
                await asyncio.sleep(0.03)
                incoming.opacity = 1
                incoming.offset = ft.Offset(0, 0)
                page.update()
            finally:
                turning["busy"] = False

        def navigate(delta):
            if turning["busy"]:
                return
            target = index + delta
            if 0 <= target < len(pages):
                turning["busy"] = True
                page.run_task(animate_turn, delta)
            else:
                page.show_dialog(ft.SnackBar(ft.Text("This is the first page." if delta < 0 else "This is the last page.")))

        def rip():
            if editing:
                return
            ask("Rip out this page? You can restore its photos, description and drawing from Recently deleted.", lambda: delete(memory))

        def gestures(child):
            # Do not register parent drag recognizers around the editor.
            # They compete with canvas strokes, text selection and scrolling.
            if editing:
                return child
            distance = {"x": 0, "y": 0}
            def start(axis):
                distance[axis] = 0
            def move(axis, event):
                distance[axis] += event.primary_delta or 0
            def end(axis, event):
                value = distance[axis]
                velocity = event.primary_velocity or 0
                if abs(value) < 65 and abs(velocity) < 500:
                    return
                if axis == "x":
                    navigate(1 if (value if abs(value) >= 65 else velocity) < 0 else -1)
                else:
                    rip()
            return ft.GestureDetector(content=child,
                on_horizontal_drag_start=lambda _: start("x"),
                on_horizontal_drag_update=lambda event: move("x", event),
                on_horizontal_drag_end=lambda event: end("x", event),
                on_vertical_drag_start=lambda _: start("y"),
                on_vertical_drag_update=lambda event: move("y", event),
                on_vertical_drag_end=lambda event: end("y", event))

        # Page gestures belong to reading mode only; editors own their drags.
        margin_hint = "Editing: page swipes are off" if editing else "Swipe to turn or rip this page"
        frame = ft.Container(expand=True, bgcolor="#825735", padding=5, border_radius=8,
            animate_opacity=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
            animate_offset=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
            margin=ft.Margin.symmetric(horizontal=max(0, ((page.width or 400) - 940) / 2)),
            content=ft.Container(bgcolor="#FFF9ED", border_radius=4,
                border=ft.Border.only(left=ft.BorderSide(7, "#D8CBB5"), right=ft.BorderSide(3, "#D8CBB5")),
                content=ft.Column([
                    ft.Container(padding=8, content=ft.Text(book.get("title") or "My journal", font_family="Georgia", color="#796E5C")),
                    ft.Row([
                        gestures(ft.Container(width=24, bgcolor="#EEE3CD", tooltip=margin_hint)),
                        ft.Container(expand=True, content=gestures(content)),
                        gestures(ft.Container(width=24, bgcolor="#EEE3CD", tooltip=margin_hint)),
                    ], expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                    ft.Row([
                        ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Previous page", disabled=index == 0, on_click=lambda _: navigate(-1)),
                        ft.Text(f"{index + 1} / {len(pages)}", size=12),
                        ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Next page", disabled=index == len(pages)-1, on_click=lambda _: navigate(1)),
                        ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Return to page view to rip" if editing else "Rip out this page",
                            disabled=editing, icon_color="#817867", on_click=lambda _: rip()),
                    ], alignment=ft.MainAxisAlignment.CENTER, wrap=True),
                    ft.Container(padding=6, alignment=ft.Alignment.CENTER,
                        content=ft.Text("Page swipes and ripping are off while editing. Use Back to page when finished."
                            if editing else "Swipe the margins sideways to turn; up or down to rip", size=10, color="#948976")),
                ], spacing=0)))
        journal_paper["frame"] = frame
        return frame

    def open_page(e):
        book = owner(e)
        day_pages = book["pages"]
        index = next(i for i, item in enumerate(day_pages) if item is e)

        async def more(event):
            def keep(photos):
                e["photos"].extend(photos)
                save()
                open_page(e)
            await receive_photos(event.files, more_picker, keep)

        result_handlers["existing"] = more

        def remove_photo(photo_index):
            e["photos"].pop(photo_index)
            save()
            open_page(e)

        def set_cover(photo_index):
            photo = e["photos"].pop(photo_index)
            e["photos"].insert(0, photo)
            save()
            open_page(e)

        def photo_card(photo, photo_index):
            return ft.Container(padding=10, bgcolor="white", border_radius=3,
                content=ft.Column([
                    pic({"photo": photo}, width=min(760, max(200, (page.width or 400) - 100)), height=260, fit=ft.BoxFit.CONTAIN),
                    ft.TextButton("Use as cover", on_click=lambda _: set_cover(photo_index)),
                    ft.TextButton("Remove photo", on_click=lambda _: ask(
                        "Remove this photo from the page?", lambda: remove_photo(photo_index)))
                ], tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER))


        paper = ft.Container(bgcolor="#FFF9ED", padding=20, border_radius=8,
            border=ft.Border.only(left=ft.BorderSide(8, "#D8CBB5")),
            content=ft.Column([
                ft.Text(e.get("title") or label(e), size=26, weight=ft.FontWeight.BOLD, color="#34473B"),
                ft.Text("PHOTOGRAPHS · double-click to open description", size=11),
                *[photo_card(photo, i) for i, photo in enumerate(e["photos"])],
                ft.Text("Add photos to this page.") if not e["photos"] else ft.Container(),
            ], tight=True))
        show(ft.Row([
                 ft.TextButton("Back to page", icon=ft.Icons.ARROW_BACK, on_click=lambda _: page_menu(e)),
                 ft.Text(f"Page {index + 1} of {len(day_pages)}")], wrap=True),
             journal_frame(e, ft.ListView([ft.GestureDetector(content=paper,
                 on_double_tap=lambda _: journal(e))], expand=True), open_page),
             ft.Row([
                 ft.FilledButton("Add photos", icon=ft.Icons.ADD_A_PHOTO, action=photo_action(more_picker)),
                 ft.TextButton("Write about this page", on_click=lambda _: journal(e)),
             ], wrap=True, alignment=ft.MainAxisAlignment.CENTER))

    def journal(e, drawing=False):
        history = histories.setdefault(e["id"], PageHistory(e))
        st = {"mode": "move", "pos": 0, "taps": 0, "sel": (0, 0), "last": None}
        tf = ft.TextField(value=e["text"], multiline=True, expand=True, border=ft.NoInputBorder(),
                          hint_text="Write about this day...")

        def changed(_):
            e["text"] = tf.value
            history.record(e)
            update_history_buttons()
            save()

        def selchange(ev):
            s = ev.selection
            if s.base_offset < 0 or s.extent_offset < 0:
                return
            st["pos"] = s.extent_offset
            st["sel"] = (min(s.base_offset, s.extent_offset), max(s.base_offset, s.extent_offset))

        tf.on_change, tf.on_selection_change = changed, selchange

        def put_selection(a, b):
            tf.selection = ft.TextSelection(base_offset=a, extent_offset=b)
            st["sel"] = (a, b)
            tf.update()

        # --- detection zone: taps select, sliding stretches ---
        async def zone_tap(_):
            if st["mode"] != "select":
                return
            st["taps"] += 1
            n = st["taps"]
            await asyncio.sleep(0.35)
            if st["taps"] == n:
                seps, inc = RULES[min(n, 4)]
                put_selection(*span(tf.value or "", st["pos"], seps, inc))
                st["taps"] = 0

        def zone_slide_start(_):
            st["anchor"], st["acc"] = st["pos"], 0.0

        def zone_slide(ev):
            if st["mode"] != "select":
                return
            st["acc"] += ev.primary_delta or 0
            end = max(0, min(len(tf.value or ""), st["anchor"] + int(st["acc"] / 8)))
            put_selection(min(st["anchor"], end), max(st["anchor"], end))

        zone = ft.Container(
            left=0, bottom=0, width=min(220, (page.width or 400) * 0.5), height=min(200, (page.height or 700) * 0.25), visible=False, border_radius=ft.BorderRadius.only(top_right=16),
            bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.ORANGE),
            border=ft.Border.all(2, ft.Colors.ORANGE),
            content=ft.GestureDetector(on_tap=zone_tap, on_horizontal_drag_start=zone_slide_start,
                                       on_horizontal_drag_update=zone_slide,
                                       content=ft.Container(content=ft.Text("Detection zone\nPlace cursor in text, then select tool.\nTap to select · slide to extend", size=11),
                                                            alignment=ft.Alignment.BOTTOM_LEFT, padding=6)))

        # --- drawing overlay ---
        e.setdefault("drawing", [])
        def line_shape(segment):
            return cv.Line(*segment, paint=ft.Paint(color="#56504A", stroke_width=3))
        canvas = cv.Canvas([line_shape(segment) for segment in e["drawing"]], expand=True)

        def update_history_buttons():
            undo_button.disabled = history.position == 0
            redo_button.disabled = history.position == len(history.states) - 1
            page.update()

        def restore(delta):
            history.move(e, delta)
            tf.value = e["text"]
            st.update(last=None, sel=(0, 0), pos=0)
            tf.selection = ft.TextSelection(base_offset=0, extent_offset=0)
            canvas.shapes = [line_shape(segment) for segment in e.get("drawing", [])]
            save()
            update_history_buttons()

        undo_button = ft.IconButton(ft.Icons.UNDO, tooltip="Undo text or drawing",
            disabled=history.position == 0, on_click=lambda _: restore(-1))
        redo_button = ft.IconButton(ft.Icons.REDO, tooltip="Redo text or drawing",
            disabled=history.position == len(history.states) - 1, on_click=lambda _: restore(1))

        def draw(ev):
            p = (ev.local_position.x, ev.local_position.y)
            if st["mode"] == "erase_draw":
                e["drawing"] = [segment for segment in e["drawing"] if not near_segment(p, segment)]
                canvas.shapes = [line_shape(segment) for segment in e["drawing"]]
                canvas.update()
                return
            if st["last"]:
                segment = [*st["last"], *p]
                e["drawing"].append(segment)
                canvas.shapes.append(line_shape(segment))
                canvas.update()
            st["last"] = p

        def finish_draw(_):
            st["last"] = None
            history.record(e)
            update_history_buttons()
            save()

        draw_layer = ft.Container(left=0, top=0, right=0, bottom=0, visible=False,
                                  content=ft.GestureDetector(on_pan_update=draw,
                                                             on_pan_end=finish_draw,
                                                             content=canvas))

        # --- tools ---
        def set_mode(m):
            st["mode"] = m if st["mode"] != m else "move"
            zone.visible = st["mode"] == "select"
            typing_area.bottom = min(200, (page.height or 700) * 0.25) if zone.visible else 0
            zone.opacity = 1 if st["mode"] == "select" else 0.5
            draw_layer.visible = st["mode"] in ("draw", "erase_draw")
            mode_label.value = {"move": "Hand · type and navigate", "draw": "Pencil · doodle on the page", "erase_draw": "Eraser · rub over drawing to remove it", "select": "Selector · use the detection zone"}[st["mode"]]
            for k, b in btns.items():
                if isinstance(b, ft.IconButton):
                    b.selected = st["mode"] == k
            page.update()

        def erase(_):
            a, b = st["sel"]
            if b > a:
                tf.value = tf.value[:a] + tf.value[b:]
                changed(None)
                put_selection(a, a)
            else:
                page.show_dialog(ft.AlertDialog(title=ft.Text("Delete selected text"),
                    content=ft.Text("Press and hold a word on your phone, or drag across text with your mouse. Adjust the selection handles, then tap Delete selected text.\n\nTo start over completely, use Clear writing. Photos are kept, and Undo restores cleared text and drawings."),
                    actions=[ft.TextButton("Got it", on_click=lambda _: page.pop_dialog())]))

        def clear_all():
            tf.value = ""
            canvas.shapes.clear()
            e["drawing"] = []
            changed(None)
            page.update()

        btns = {
            "move": ft.IconButton(ft.Icons.PAN_TOOL, selected_icon=ft.Icons.PAN_TOOL, on_click=lambda _: set_mode("move"), tooltip="Normal"),
            "draw": ft.IconButton(ft.Icons.EDIT, on_click=lambda _: set_mode("draw"), tooltip="Pencil: draw / doodle"),
            "select": ft.IconButton(ft.Icons.NEAR_ME, on_click=lambda _: set_mode("select"), tooltip="Selector"),
            "erase_draw": ft.TextButton("Drawing eraser", icon=ft.Icons.CLEANING_SERVICES_OUTLINED,
                on_click=lambda _: set_mode("erase_draw")),
        }
        eraser = ft.TextButton("Delete selected text", icon=ft.Icons.BACKSPACE_OUTLINED,
                              on_click=erase, tooltip="Delete only the highlighted text")
        clear_writing = ft.TextButton("Clear writing", icon=ft.Icons.RESTART_ALT,
            tooltip="Clear text and drawing; keep photos",
            on_click=lambda _: ask("Clear all text and drawing on this page? Photos will stay. You can restore the writing with Undo.", clear_all))


        mode_label = ft.Text("Hand · type and navigate", size=11, color="#766D60")

        def settings(_):
            def font_changed(ev):
                tf.text_size = int(ev.control.value)
                e["font_size"] = tf.text_size
                save()
                page.update()
            def clear_drawing():
                canvas.shapes.clear()
                e["drawing"] = []
                history.record(e)
                update_history_buttons()
                save()
                page.update()
            page.show_dialog(ft.AlertDialog(title=ft.Text("Journal settings"),
                content=ft.Column([
                    ft.Text("Text size"),
                    ft.Slider(min=14, max=26, divisions=6, value=e.get("font_size", 18), on_change=font_changed),
                    ft.Text("Selector: place the cursor in your journal, then use the lower-left zone.\n"
                            "1 tap: word · 2 taps: clause\n3 taps: paragraph · 4 taps: sentence\n"
                            "Drag sideways in the zone to extend the selection.\n"
                            "Delete selected text removes highlighted words.\n"
                            "Clear writing clears text and drawing after confirmation; photos stay. Undo restores it.", size=13),
                    ft.TextButton("Clear doodles", on_click=lambda _: (page.pop_dialog(), ask("Clear all doodles?", clear_drawing))),
                ], tight=True), actions=[ft.TextButton("Done", on_click=lambda _: page.pop_dialog())]))

        tf.text_size = e.get("font_size", 18)
        bar = ft.Container(bgcolor="#E8DFCE", padding=8, content=ft.Row([
            btns["move"], btns["draw"], btns["select"], undo_button, redo_button, btns["erase_draw"],
            eraser, clear_writing
        ], wrap=True, spacing=3, run_spacing=0))
        # Reserve room for the detection pad only while selecting text.
        typing_area = ft.Container(left=0, top=0, right=0, bottom=0,
                                   padding=18, content=tf)
        area = ft.Stack([
            ft.Container(left=0, top=0, right=0, bottom=0, bgcolor="#FFF9ED"),
            typing_area,
            ft.Container(left=0, top=0, right=0, bottom=0, ignore_interactions=True, content=canvas),
            zone,
            ft.Container(right=4, bottom=4, content=ft.IconButton(ft.Icons.SETTINGS, tooltip="Settings", on_click=settings)),
        ], expand=True)
        # A transparent drawing surface captures strokes only in pencil mode.
        draw_layer.content.content = ft.Container(expand=True, bgcolor=ft.Colors.TRANSPARENT)
        area.controls.insert(3, draw_layer)
        show(ft.Row([ft.TextButton("Back to page", icon=ft.Icons.ARROW_BACK, on_click=lambda _: page_menu(e)),
                     ft.Text(label(e), size=12, color="#817867"), mode_label], wrap=True),
             journal_frame(e, ft.Column([bar, area], expand=True, spacing=0),
                           lambda memory: journal(memory, drawing=drawing), editing=True))
        if drawing:
            set_mode("draw")

    shelf()


async def main(page: ft.Page):
    try:
        await app(page)
    except Exception:
        import traceback
        err = traceback.format_exc()
        logger.error(err)
        page.controls.clear()
        page.add(ft.Text("Something went wrong:", weight=ft.FontWeight.BOLD), ft.Text("Please reopen the app. Your saved memories have not been intentionally cleared."))
        page.update()


def lan_ip():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as k:
            k.connect(("8.8.8.8", 80))
            return k.getsockname()[0]
    except Exception:
        return "YOUR-COMPUTER-IP"


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run Dayshelf locally.")
    parser.add_argument("--port", type=int, default=8550)
    parser.add_argument("--lan", action="store_true", help="Allow devices on your local network to connect.")
    parser.add_argument("--debug", action="store_true", help="Enable diagnostic logs.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    print(f"Dayshelf: http://localhost:{args.port}")
    if args.lan:
        print(f"Same-Wi-Fi devices: http://{lan_ip()}:{args.port}")
    uvicorn.run(build_web_app(), host=host, port=args.port, access_log=False)
