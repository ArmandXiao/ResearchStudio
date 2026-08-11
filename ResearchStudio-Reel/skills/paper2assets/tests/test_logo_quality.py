from __future__ import annotations

import io
import os

import numpy as np
from PIL import Image, ImageDraw

from utils.logo_quality import fingerprints_match, inspect_logo_bytes
from utils.logo_trim import autotrim


def _encode(image: Image.Image, format_name: str, **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=format_name, **save_kwargs)
    return buffer.getvalue()


def _mark(size: tuple[int, int], *, transparent: bool) -> Image.Image:
    mode = "RGBA" if transparent else "RGB"
    background = (255, 255, 255, 0) if transparent else "white"
    image = Image.new(mode, size, background)
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rounded_rectangle(
        (width * 0.12, height * 0.18, width * 0.42, height * 0.82),
        radius=max(2, height // 12),
        fill=(23, 84, 166, 255) if transparent else (23, 84, 166),
    )
    draw.polygon(
        (
            (width * 0.52, height * 0.18),
            (width * 0.88, height * 0.50),
            (width * 0.52, height * 0.82),
        ),
        fill=(232, 80, 43, 255) if transparent else (232, 80, 43),
    )
    return image


def test_non_image_html_and_xml_are_rejected() -> None:
    for payload in (
        b"<!doctype html><html><body>not an image</body></html>",
        b"<?xml version='1.0'?><response><error>denied</error></response>",
    ):
        result = inspect_logo_bytes(payload, source="https://example.test/logo")
        assert result.accepted is False
        assert result.reason.startswith("decode_failed:")


def test_transparent_white_mark_is_rejected_as_low_contrast() -> None:
    image = Image.new("RGBA", (240, 100), (255, 255, 255, 0))
    ImageDraw.Draw(image).rectangle((30, 24, 210, 76), fill=(255, 255, 255, 255))

    result = inspect_logo_bytes(_encode(image, "PNG"))

    assert result.accepted is False
    assert result.reason == "low_contrast_on_light_header"
    assert result.has_transparency is True


def test_opaque_photographic_raster_is_rejected() -> None:
    rng = np.random.default_rng(20260811)
    pixels = rng.integers(0, 256, size=(240, 360, 3), dtype=np.uint8)
    image = Image.fromarray(pixels, "RGB")

    result = inspect_logo_bytes(_encode(image, "JPEG", quality=92))

    assert result.accepted is False
    assert result.reason == "photographic_image"


def test_opaque_jpeg_crest_is_not_misclassified_as_a_photo() -> None:
    image = Image.new("RGB", (500, 640), "white")
    draw = ImageDraw.Draw(image)
    shield = ((28, 22), (472, 22), (438, 470), (250, 620), (62, 470))
    draw.polygon(shield, fill="#171744", outline="#050505", width=14)
    draw.polygon(
        ((82, 86), (418, 86), (395, 390), (250, 540), (105, 390)),
        fill="#b4202a", outline="#f3d77a", width=10,
    )
    for x in range(105, 420, 78):
        draw.ellipse((x, 42, x + 48, 90), fill="#f5dda0", outline="#d12f3b", width=4)
    for y in range(132, 350, 42):
        draw.line((112, y, 388, y), fill="#f5dda0", width=11)
        draw.line((250, y - 20, 250, y + 18), fill="#111111", width=5)
    draw.rectangle((155, 360, 345, 470), fill="#f7edc7", outline="#111111", width=8)
    draw.line((250, 365, 250, 466), fill="#111111", width=6)

    result = inspect_logo_bytes(_encode(image, "JPEG", quality=91))

    assert result.accepted is True
    assert result.foreground_fraction > 0.75
    assert result.edge_density > 0.30
    assert result.palette_concentration > 0.38
    assert result.flat_fraction > 0.28


def test_portrait_brand_cover_is_rejected() -> None:
    height, width = 640, 480
    yy, xx = np.mgrid[:height, :width]
    image = np.full((height, width, 3), (232, 241, 252), dtype=np.float32)
    ring = np.abs(np.hypot(xx - width / 2, yy - height / 2) - 145) < 42
    image[ring, 0] = 20 + 210 * (xx[ring] / width)
    image[ring, 1] = 80 + 150 * (yy[ring] / height)
    image[ring, 2] = 245
    corner = np.exp(-((xx - 25) ** 2 + (yy - 25) ** 2) / (2 * 95 ** 2))
    image[..., 0] += 55 * corner
    image[..., 2] -= 35 * corner
    card = Image.fromarray(np.clip(image, 0, 255).astype(np.uint8), "RGB")

    result = inspect_logo_bytes(_encode(card, "PNG"))

    assert result.accepted is False
    assert result.reason == "cover_or_page_image"


def test_visual_fingerprint_matches_encoding_and_resolution_variants() -> None:
    png = inspect_logo_bytes(_encode(_mark((480, 180), transparent=True), "PNG"))
    jpeg = inspect_logo_bytes(
        _encode(_mark((960, 360), transparent=False), "JPEG", quality=90)
    )

    assert png.accepted is True
    assert jpeg.accepted is True
    assert fingerprints_match(png.fingerprint, jpeg.fingerprint)


def test_autotrim_preserves_transparent_white_artwork(tmp_path) -> None:
    image = Image.new("RGBA", (240, 100), (255, 255, 255, 0))
    ImageDraw.Draw(image).rectangle((30, 24, 210, 76), fill=(255, 255, 255, 255))
    path = tmp_path / "white-mark.png"
    image.save(path)

    result = autotrim(path)
    trimmed = Image.open(result).convert("RGBA")

    assert result == path
    assert trimmed.width < 240
    assert trimmed.height < 100
    assert trimmed.getchannel("A").getextrema()[1] == 255


def test_autotrim_jpeg_is_atomic_and_nonempty(tmp_path) -> None:
    image = Image.new("RGB", (320, 160), "white")
    ImageDraw.Draw(image).rectangle((50, 35, 270, 125), fill=(25, 70, 145))
    path = tmp_path / "wordmark.jpg"
    image.save(path, format="JPEG", quality=92)
    if os.name != "nt":
        path.chmod(0o644)

    result = autotrim(path)
    trimmed = Image.open(result)

    assert result == path
    assert path.stat().st_size > 0
    assert trimmed.format == "JPEG"
    assert trimmed.width < 320
    assert trimmed.height < 160
    if os.name != "nt":
        assert path.stat().st_mode & 0o7777 == 0o644


def test_autotrim_encode_failure_preserves_original(tmp_path, monkeypatch) -> None:
    image = Image.new("RGB", (320, 160), "white")
    ImageDraw.Draw(image).rectangle((50, 35, 270, 125), fill=(25, 70, 145))
    path = tmp_path / "wordmark.jpg"
    image.save(path, format="JPEG", quality=92)
    original = path.read_bytes()

    def fail_save(*_args, **_kwargs) -> None:
        raise OSError("simulated encode failure")

    monkeypatch.setattr(Image.Image, "save", fail_save)

    assert autotrim(path) == path
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".wordmark-trim-*")) == []
