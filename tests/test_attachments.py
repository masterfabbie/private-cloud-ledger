import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.services import attachments as att_service
from tests.conftest import login


def _photo(size=(3000, 2000), orientation=6, gps=True) -> bytes:
    """A JPEG like a phone camera makes: EXIF orientation (rotated) and GPS position."""
    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation: rotate 90° CW to display
    exif[0x010F] = "PhoneMaker"
    if gps:
        exif[0x8825] = {1: "N", 2: (52.0, 31.0, 12.0), 3: "E", 4: (13.0, 24.0, 18.0)}
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _tx(c: TestClient, desc="Baumarkt") -> dict:
    acc = c.get("/api/accounts").json()[0]
    return c.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-05-01",
                                             "amount_cents": -4599, "description": desc}).json()


def _upload(c, tx_id, *files):
    return c.post(f"/api/transactions/{tx_id}/attachments", files=[("files", f) for f in files])


def _files_on_disk() -> set[str]:
    return {p.name for p in att_service.storage_dir().glob("*/*")}


def test_photo_is_rotated_shrunk_and_stripped(admin, client):
    tx = _tx(admin)
    r = _upload(admin, tx["id"], ("IMG_0001.HEIC.png", _photo(), "image/jpeg"))
    assert r.status_code == 201, r.text
    a = r.json()[0]
    assert a["content_type"] == "image/jpeg" and a["filename"] == "IMG_0001.HEIC.jpg" and a["thumb_url"]

    full = admin.get(a["url"])
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    assert "sandbox" in full.headers["content-security-policy"]
    img = Image.open(io.BytesIO(full.content))
    assert img.size == (1600, 2400)  # rotated to portrait and limited to 2400 px
    assert not img.getexif()  # no orientation, maker or GPS data left
    thumb = Image.open(io.BytesIO(admin.get(a["thumb_url"]).content))
    assert max(thumb.size) == 360

    listed = admin.get("/api/transactions").json()["items"][0]["attachments"]
    assert [x["id"] for x in listed] == [a["id"]]

    # Other users cannot see it.
    admin.post("/api/admin/users", json={"username": "mallory", "password": "password123"})
    other = login(TestClient(client.app), "mallory", "password123")
    assert other.get(a["url"]).status_code == 404
    assert _upload(other, tx["id"], ("x.pdf", PDF, "application/pdf")).status_code == 404
    assert other.delete(a["url"]).status_code == 404


def test_pdf_and_rejected_files(admin, monkeypatch):
    tx = _tx(admin)
    r = _upload(admin, tx["id"], ("Rechnung.pdf", PDF, "application/pdf"))
    assert r.status_code == 201 and r.json()[0]["thumb_url"] is None
    assert admin.get(r.json()[0]["url"]).content == PDF

    bad = _upload(admin, tx["id"], ("evil.html", b"<script>alert(1)</script>", "image/jpeg"))
    assert bad.status_code == 400 and "only photos" in bad.json()["detail"]
    heic = _upload(admin, tx["id"], ("a.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 50, "image/heic"))
    assert heic.status_code == 400 and "HEIC" in heic.json()["detail"]
    broken = _upload(admin, tx["id"], ("b.jpg", b"\xff\xd8\xff" + b"garbage" * 10, "image/jpeg"))
    assert broken.status_code == 400

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "max_attachment_mb", 0)
    assert _upload(admin, tx["id"], ("c.pdf", PDF, "application/pdf")).status_code == 400
    # Nothing from the failed uploads stayed behind; only the first PDF exists.
    assert len(admin.get("/api/transactions").json()["items"][0]["attachments"]) == 1


def test_files_are_removed_with_their_transactions(admin):
    before = _files_on_disk()
    t1, t2, t3 = _tx(admin, "A"), _tx(admin, "B"), _tx(admin, "C")
    a1 = _upload(admin, t1["id"], ("a.jpg", _photo(gps=False), "image/jpeg")).json()[0]
    _upload(admin, t2["id"], ("b.pdf", PDF, "application/pdf"))
    _upload(admin, t3["id"], ("c.pdf", PDF, "application/pdf"))
    assert len(_files_on_disk() - before) == 4  # photo + thumbnail + 2 PDFs

    assert admin.delete(a1["url"]).status_code == 204
    assert len(_files_on_disk() - before) == 2
    admin.delete(f"/api/transactions/{t2['id']}")
    assert len(_files_on_disk() - before) == 1

    backup = admin.get("/api/export/json").content
    dry = admin.post("/api/export/restore", params={"dry_run": True}, files={"file": ("b.json", backup, "application/json")}).json()
    assert dry["current"]["attachments"] == 1
    admin.post("/api/export/restore", files={"file": ("b.json", backup, "application/json")})
    assert _files_on_disk() == before  # receipts of replaced transactions are removed

    t4 = _tx(admin, "D")
    _upload(admin, t4["id"], ("d.pdf", PDF, "application/pdf"))
    admin.delete("/api/transactions", params={"confirm": "DELETE"})
    assert _files_on_disk() == before


def test_upload_needs_csrf(admin):
    tx = _tx(admin)
    token = admin.headers.pop("X-CSRF-Token")
    assert _upload(admin, tx["id"], ("x.pdf", PDF, "application/pdf")).status_code == 403
    admin.headers["X-CSRF-Token"] = token


def test_orphan_cleanup(db, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "attachments_dir", str(tmp_path))
    root = att_service.storage_dir() / "999"
    root.mkdir(parents=True, exist_ok=True)
    stray = root / "deadbeef"
    stray.write_bytes(b"x")
    assert att_service.cleanup_orphans(db, min_age_seconds=3600) == 0  # too new, maybe an upload in progress
    assert att_service.cleanup_orphans(db, min_age_seconds=0) == 1
    assert not Path(stray).exists()
