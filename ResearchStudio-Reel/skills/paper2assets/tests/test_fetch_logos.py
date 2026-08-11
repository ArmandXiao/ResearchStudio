from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

import fetch_logos
from utils.logo_quality import inspect_logo_bytes


def _mark_bytes(
    *, size: tuple[int, int] = (320, 120), format_name: str = "PNG",
) -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((width * 0.08, height * 0.18, width * 0.38, height * 0.82), fill="#145da0")
    draw.ellipse((width * 0.54, height * 0.18, width * 0.86, height * 0.82), fill="#e64833")
    buffer = io.BytesIO()
    image.save(buffer, format=format_name, quality=92)
    return buffer.getvalue()


def _alternate_mark_bytes() -> bytes:
    image = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(((25, 95), (92, 18), (160, 95)), fill="#522a83")
    draw.rounded_rectangle((190, 28, 295, 92), radius=20, fill="#2a9d68")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_entry(
    logos_dir: Path, name: str, filename: str, source: str,
    *, institution_key: str | None = None, data: bytes | None = None,
) -> dict:
    payload = data or _mark_bytes()
    path = logos_dir / filename
    path.write_bytes(payload)
    inspection = inspect_logo_bytes(payload, source=source)
    assert inspection.accepted
    key = institution_key or fetch_logos.canonical_institution_key(name)
    return {
        "name": name,
        "institution_key": key,
        "institution_keys": [key],
        "slug": path.stem,
        "path": f"assets/logos/{filename}",
        "source": source,
        "approved": True,
        "decision": "approved",
        "fingerprint": inspection.fingerprint,
        "quality": inspection.to_dict(),
    }


def test_microsoft_research_aliases_share_canonical_key() -> None:
    assert fetch_logos.canonical_institution_key("MSRA") == "microsoft"
    assert fetch_logos.canonical_institution_key("Microsoft Research") == "microsoft"
    assert fetch_logos.canonical_institution_key("Microsoft Research Asia") == "microsoft"


def test_approved_entry_keeps_requested_and_resolved_identity(tmp_path) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    payload = _mark_bytes()
    path = logos_dir / "university-of-trento.png"
    path.write_bytes(payload)
    inspection = inspect_logo_bytes(payload)

    entry = fetch_logos._approved_entry(
        "Department of Information Engineering, University of Trento",
        "university-of-trento",
        path,
        "https://example.test/trento.png",
        inspection,
        resolved_title="University of Trento",
    )

    assert entry["institution_key"] == "university-of-trento"
    assert "department-of-information-engineering-university-of-trento" in entry["institution_keys"]
    assert "university-of-trento" in entry["institution_keys"]


def test_fetch_logo_continues_after_rejected_candidate(monkeypatch) -> None:
    valid = _mark_bytes()
    rejected: list[dict] = []

    monkeypatch.setattr(fetch_logos, "wikidata_logo_urls", lambda _qid: [
        "https://example.test/not-image.xml",
        "https://example.test/official.png",
    ])
    monkeypatch.setattr(fetch_logos, "find_logo_url", lambda _html: None)

    def fake_fetch(url: str, timeout: float = 15.0) -> bytes:
        del timeout
        if "wikipedia.org/wiki" in url:
            return b"<html data-qid='Q1'></html>"
        if url.endswith("not-image.xml"):
            return b"<?xml version='1.0'?><error>not an image</error>"
        return valid

    monkeypatch.setattr(fetch_logos, "fetch", fake_fetch)

    result = fetch_logos.fetch_logo_for("MSRA", rejected=rejected)

    assert result is not None
    assert result["source"].endswith("official.png")
    assert result["inspection"].accepted is True
    assert len(rejected) == 1
    assert rejected[0]["decision"] == "rejected"
    assert rejected[0]["source"].endswith("not-image.xml")


def test_dedupe_deletes_correct_manifest_path_and_preserves_orphan(tmp_path) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    first = _write_entry(logos_dir, "Institute A", "a.png", "https://same.test/logo")
    second = _write_entry(logos_dir, "Institute B", "b.png", "https://same.test/logo")
    orphan = logos_dir / "user-orphan.png"
    orphan.write_bytes(b"do not delete")

    kept = fetch_logos._dedupe_by_source([first, second], logos_dir)

    assert len(kept) == 1
    assert (logos_dir / "a.png").exists()
    assert not (logos_dir / "b.png").exists()
    assert not (tmp_path / "assets" / "assets" / "logos" / "b.png").exists()
    assert orphan.read_bytes() == b"do not delete"


def test_dedupe_never_deletes_keeper_when_paths_are_identical(tmp_path) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    first = _write_entry(logos_dir, "Institute A", "shared.png", "https://same.test/logo")
    second = dict(first, name="Institute B", institution_key="institute-b", institution_keys=["institute-b"])

    kept = fetch_logos._dedupe_by_source([first, second], logos_dir)

    assert len(kept) == 1
    assert (logos_dir / "shared.png").exists()
    assert set(kept[0]["institution_keys"]) == {"institute-a", "institute-b"}


def test_visual_fingerprint_dedupes_different_urls_and_encodings(tmp_path) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    png = _write_entry(
        logos_dir, "Institute A", "a.png", "https://one.test/logo.png",
        data=_mark_bytes(size=(320, 120), format_name="PNG"),
    )
    jpeg = _write_entry(
        logos_dir, "Institute B", "b.jpg", "https://two.test/logo.jpg",
        data=_mark_bytes(size=(640, 240), format_name="JPEG"),
    )

    kept = fetch_logos._dedupe_by_source([png, jpeg], logos_dir)

    assert len(kept) == 1
    assert set(kept[0]["institution_keys"]) == {"institute-a", "institute-b"}
    assert not (logos_dir / "b.jpg").exists()


def test_add_logo_merges_manifest_replaces_canonical_and_clears_missing(
    tmp_path, monkeypatch,
) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    unrelated = _write_entry(
        logos_dir, "Unrelated University", "unrelated.png", "https://old.test/unrelated",
        data=_alternate_mark_bytes(),
    )
    old_msra = _write_entry(
        logos_dir, "MSRA", "old-msra.png", "https://old.test/msra",
        institution_key="microsoft",
    )
    orphan = logos_dir / "campus-photo-user-orphan.jpg"
    orphan.write_bytes(b"preserve me")
    (logos_dir / "logos.json").write_text(json.dumps({
        "logos": [unrelated, old_msra],
        "missing": ["Microsoft Research Asia", "Still Missing Institute"],
        "rejected": [],
    }), encoding="utf-8")

    def fake_download(name, url, target_dir, rejected=None):
        del rejected
        return _write_entry(
            target_dir, name, "microsoft-new.png", url,
            institution_key="microsoft",
            data=_mark_bytes(size=(360, 135)),
        )

    monkeypatch.setattr(fetch_logos, "download_named_logo", fake_download)

    code = fetch_logos.main([
        "--outdir", str(tmp_path),
        "--add-logo", "Microsoft Research=https://new.test/microsoft.png",
    ])
    manifest = json.loads((logos_dir / "logos.json").read_text(encoding="utf-8"))

    assert code == 0
    assert manifest["version"] == 2
    assert [entry["institution_key"] for entry in manifest["logos"]] == [
        "unrelated-university", "microsoft",
    ]
    assert "Microsoft Research Asia" not in manifest["missing"]
    assert manifest["missing"] == ["Still Missing Institute"]
    assert not (logos_dir / "old-msra.png").exists()
    assert (logos_dir / "microsoft-new.png").exists()
    assert orphan.read_bytes() == b"preserve me"


def test_rejected_add_logo_is_recorded_outside_accepted_list(
    tmp_path, monkeypatch,
) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    (logos_dir / "logos.json").write_text(json.dumps({
        "logos": [], "missing": ["Bad Institute"], "rejected": [],
    }), encoding="utf-8")

    rejected_inspection = inspect_logo_bytes(b"<html>not an image</html>")

    def fake_download(name, url, target_dir, rejected=None):
        del target_dir
        assert rejected is not None
        rejected.append(fetch_logos._rejected_entry(
            name, url, rejected_inspection, stage="web-fallback",
        ))
        return None

    monkeypatch.setattr(fetch_logos, "download_named_logo", fake_download)

    code = fetch_logos.main([
        "--outdir", str(tmp_path),
        "--add-logo", "Bad Institute=https://bad.test/logo",
    ])
    manifest = json.loads((logos_dir / "logos.json").read_text(encoding="utf-8"))

    assert code == 1
    assert manifest["logos"] == []
    assert manifest["missing"] == ["Bad Institute"]
    assert len(manifest["rejected"]) == 1
    assert manifest["rejected"][0]["decision"] == "rejected"


def test_normal_pass_preserves_valid_current_fallback_and_leaves_old_files(
    tmp_path, monkeypatch,
) -> None:
    logos_dir = tmp_path / "assets" / "logos"
    logos_dir.mkdir(parents=True)
    fallback = _write_entry(
        logos_dir, "Current Institute", "current-fallback.png",
        "https://fallback.test/current.png",
    )
    old_other = _write_entry(
        logos_dir, "Previous Paper Institute", "previous-paper.png",
        "https://fallback.test/previous.png",
        data=_alternate_mark_bytes(),
    )
    orphan = logos_dir / "user-orphan.jpg"
    orphan.write_bytes(b"preserve me")
    (logos_dir / "logos.json").write_text(json.dumps({
        "logos": [fallback, old_other],
        "missing": [],
        "rejected": [],
    }), encoding="utf-8")
    monkeypatch.setattr(fetch_logos, "fetch_logo_for", lambda name, rejected=None: None)

    code = fetch_logos.main([
        "--outdir", str(tmp_path), "--names", "Current Institute",
    ])
    manifest = json.loads((logos_dir / "logos.json").read_text(encoding="utf-8"))

    assert code == 0
    assert [entry["institution_key"] for entry in manifest["logos"]] == [
        "current-institute",
    ]
    assert manifest["missing"] == []
    assert (logos_dir / "current-fallback.png").exists()
    assert (logos_dir / "previous-paper.png").exists()
    assert orphan.read_bytes() == b"preserve me"
