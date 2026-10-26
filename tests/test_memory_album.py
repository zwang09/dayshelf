import base64
import io
import time
import unittest
from copy import deepcopy

from PIL import Image
from starlette.testclient import TestClient

import memory_album as app


def sample_backup():
    page = {
        "id": "page-1", "date": ["26", "10", "2026"], "text": "A memory",
        "photos": [], "drawing": [[0, 0, 50, 0]],
    }
    book = {
        "id": "journal-1", "date": ["26", "10", "2026"], "text": "",
        "title": "Sample", "photos": [], "pages": [page], "deleted_pages": [],
    }
    return {"format": "memory-album", "version": 1, "journals": [book]}


class JournalTests(unittest.TestCase):
    def test_backup_preserves_recoverable_pages_without_aliasing(self):
        payload = sample_backup()
        book = payload["journals"][0]
        book["deleted_pages"].append({"index": 0, "page": book["pages"].pop()})
        restored = app.validate_backup(payload)
        self.assertEqual(restored, payload["journals"])
        restored[0]["deleted_pages"][0]["page"]["text"] = "Changed"
        self.assertEqual(book["deleted_pages"][0]["page"]["text"], "A memory")

    def test_backup_rejects_invalid_images_and_drawings(self):
        for field, value in [("photos", ["not an image"]), ("drawing", [[0, 0, float("nan"), 1]])]:
            with self.subTest(field=field):
                payload = sample_backup()
                payload["journals"][0]["pages"][0][field] = value
                with self.assertRaises(Exception):
                    app.validate_backup(payload)

    def test_migration_keeps_legacy_pages_and_is_idempotent(self):
        page = sample_backup()["journals"][0]["pages"][0]
        books = app.migrate_journals([page])
        self.assertEqual(books[0]["pages"], [page])
        self.assertEqual(app.migrate_journals(books), books)

    def test_erasing_can_be_undone_and_new_edits_clear_redo(self):
        page = sample_backup()["journals"][0]["pages"][0]
        original = deepcopy(page)
        history = app.PageHistory(page)
        page["drawing"] = [s for s in page["drawing"] if not app.near_segment((25, 5), s)]
        history.record(page)
        self.assertEqual(page["drawing"], [])
        history.move(page, -1)
        self.assertEqual(page, original)
        page["text"] = "New text"
        history.record(page)
        history.move(page, 1)
        self.assertEqual(page["drawing"], original["drawing"])
        self.assertEqual(page["text"], "New text")

    def test_photo_conversion_preserves_aspect_ratio(self):
        raw = io.BytesIO()
        Image.new("RGB", (1400, 900), "green").save(raw, "PNG")
        image = Image.open(io.BytesIO(base64.b64decode(app.shrink(raw.getvalue()))))
        self.assertEqual(image.size, (700, 450))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.backup = sample_backup()
        self.imports = []

        async def receive(books):
            self.imports.append(books)

        app.backup_sessions["test-backup"] = (time.monotonic() + 60, lambda: self.backup, receive)
        self.client = TestClient(app.build_web_app())

    def tearDown(self):
        self.client.close()
        app.backup_sessions.pop("test-backup", None)
        app.browser_imports.pop("test-photo", None)
        app.photo_previews.clear()

    def test_export_download_and_import_callback(self):
        response = self.client.get("/backup/test-backup?download=1")
        self.assertEqual(response.json(), self.backup)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(self.client.post("/backup/test-backup", json=self.backup).status_code, 200)
        self.assertEqual(self.imports, [self.backup["journals"]])

    def test_invalid_or_cross_origin_import_does_not_deliver(self):
        self.assertEqual(self.client.post("/backup/test-backup", json={}).status_code, 400)
        response = self.client.post("/backup/test-backup", json=self.backup, headers={"Origin": "https://unrelated.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.imports, [])

    def test_missing_and_expired_links(self):
        self.assertEqual(self.client.get("/backup/missing").status_code, 410)
        app.backup_sessions["test-backup"] = (0, lambda: self.backup, None)
        self.assertEqual(self.client.get("/backup/test-backup").status_code, 410)
        self.assertEqual(self.client.get("/photo-import/missing").status_code, 410)
        self.assertEqual(self.client.get("/photo-image/missing").status_code, 404)

    def test_photo_upload_and_jpeg_delivery(self):
        received = []

        async def receive(encoded):
            received.append(encoded)

        app.browser_imports["test-photo"] = (time.monotonic() + 60, receive)
        raw = io.BytesIO()
        Image.new("RGB", (80, 60), "green").save(raw, "JPEG")
        response = self.client.post("/photo-import/test-photo", content=raw.getvalue())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(received), 1)
        image_bytes = base64.b64decode(received[0])
        url = app.photo_source(image_bytes, "ws://testserver")
        self.assertTrue(url.startswith("http://testserver/"))
        response = self.client.get(url)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.content, image_bytes)
        self.assertEqual(self.client.post("/photo-import/test-photo", content=b"bad image").status_code, 422)
        self.assertEqual(len(received), 1)


if __name__ == "__main__":
    unittest.main()
