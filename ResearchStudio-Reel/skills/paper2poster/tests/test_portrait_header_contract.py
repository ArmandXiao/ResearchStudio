"""Regression tests for Portrait header resource ownership and final logo fitting.

The five Portrait headers deliberately vary their visual order and grid areas.  These
tests therefore pin only the shared semantic DOM contract that downstream logo fitting
depends on, plus the orientation-specific style catalogs.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from lxml import html as lxml_html


SKILL = Path(__file__).resolve().parent.parent
REFERENCES = SKILL / "references"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(REFERENCES))
sys.path.insert(0, str(SCRIPTS))

import compose_poster  # noqa: E402
import fit_logos  # noqa: E402
import render_poster  # noqa: E402


PORTRAIT_HEADERS = tuple(f"pv{i}" for i in range(1, 6))
LANDSCAPE_STYLES = {
    "solid", "framed", "simple", "left-bar", "elevated", "neo-brutal",
    "tag", "underline", "tinted", "double-rule", "legend-frame",
}
PORTRAIT_EXCLUDED_STYLES = {"underline", "double-rule"}
PORTRAIT_STYLES = LANDSCAPE_STYLES - PORTRAIT_EXCLUDED_STYLES
PIXEL_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _has_class(name: str) -> str:
    return (
        'contains(concat(" ", normalize-space(@class), " "), '
        f'" {name} ")'
    )


def _source_titlebar(header: str):
    source = SKILL / "assets" / "headers_portrait" / f"{header}.html"
    wrapper = lxml_html.fromstring(
        f"<div>{source.read_text(encoding='utf-8')}</div>"
    )
    titlebars = wrapper.xpath(f"./header[{_has_class('titlebar')}]")
    assert len(titlebars) == 1, f"{header} must define exactly one titlebar"
    return titlebars[0]


def _composition(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="paper2poster-composition" type="application/json">(.*?)</script>',
        text,
        flags=re.S,
    )
    assert match, f"missing composition manifest in {path}"
    return json.loads(match.group(1))


def _build_renderable_portrait_header(
    output: Path, *, layout: str, header: str,
) -> None:
    """Compose a real A0 header and fill only the fields needed for geometry."""
    with contextlib.redirect_stdout(io.StringIO()):
        compose_poster.compose(
            layout, "solid", header, output,
            orientation="portrait", theme="blue", seed=f"align-{layout}-{header}",
        )
    replacements = {
        "TITLE": "A Deliberately Long Research Paper Title",
        "AUTHORS": "Ada Researcher, Grace Scientist",
        "AUTHOR_LEGEND": "1 Example University  2 Research Institute",
        "CONTACT": "contact@example.org",
        "VENUE_LINK": "https://example.org/paper",
        "VENUE_NAME": "PREPRINT",
        "VENUE_YEAR": "2026",
        "VENUE_TAG": "POSTER",
        "QR_PAPER": PIXEL_DATA_URI,
        "QR_CODE": PIXEL_DATA_URI,
        "LOGO_1": PIXEL_DATA_URI,
        "LOGO_2": PIXEL_DATA_URI,
        "LOGO_3": "",
        "LOGO_4": "",
    }
    source = output.read_text(encoding="utf-8")
    for key, value in replacements.items():
        source = source.replace(f"{{{{{key}}}}}", value)
    source = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", source)
    output.write_text(source, encoding="utf-8")


@pytest.mark.parametrize("header", PORTRAIT_HEADERS)
def test_portrait_header_resource_ownership_contract(header):
    """Layout variants may reorder rails, but must not mix QR and logo ownership."""
    titlebar = _source_titlebar(header)

    meta_rails = titlebar.xpath(f"./div[{_has_class('meta-rail')}]")
    title_blocks = titlebar.xpath(f"./div[{_has_class('title-block')}]")
    institution_rails = titlebar.xpath(
        f"./div[{_has_class('logo-block')} and {_has_class('institution-rail')}]"
    )
    assert len(meta_rails) == 1
    assert len(title_blocks) == 1
    assert len(institution_rails) == 1

    meta = meta_rails[0]
    assert len(meta.xpath(f"./div[{_has_class('venue-badge')}]")) == 1
    assert len(meta.xpath(f"./div[{_has_class('qr-block')}]")) == 1
    assert not meta.xpath(f".//*[{_has_class('logo-block')}]")

    # There is one owner for each resource, and the institution rail stays a direct
    # titlebar child so fit_logos can measure it independently from Venue + QR.
    assert len(titlebar.xpath(f".//*[{_has_class('venue-badge')}]")) == 1
    assert len(titlebar.xpath(f".//*[{_has_class('qr-block')}]")) == 1
    assert institution_rails[0].getparent() is titlebar
    assert institution_rails[0].xpath(f".//img[{_has_class('logo')}]")
    assert not titlebar.xpath(f".//*[{_has_class('right-zone')}]")


@pytest.mark.parametrize(
    ("header", "grid_areas"),
    (
        ("pv2", 'grid-template-areas: "tt tt" "mr ir"'),
        ("pv3", 'grid-template-areas: "tt ir mr"'),
        ("pv4", 'grid-template-areas: "ir tt mr"'),
        ("pv5", 'grid-template-areas: "mr ir tt"'),
    ),
)
def test_portrait_header_variants_declare_distinct_grid_topologies(
    header, grid_areas,
):
    """The catalog must vary structure, not only title alignment and size."""
    source = (
        SKILL / "assets" / "headers_portrait" / f"{header}.html"
    ).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", source)
    assert grid_areas in normalized
    assert "grid-template-rows" not in source


def test_pv4_is_the_center_aligned_mirror_of_pv1():
    source = (
        SKILL / "assets" / "headers_portrait" / "pv4.html"
    ).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", source)
    assert 'grid-template-areas: "ir tt mr"' in normalized
    assert "text-align: center" in source
    assert "text-align: right" not in source


@pytest.mark.parametrize(
    ("header", "alignment", "divider", "logo_padding"),
    (
        ("pv3", "left", "border-left: 2px", "padding-right: 28pt"),
        ("pv5", "right", "border-right: 2px", "padding-left: 28pt"),
    ),
)
def test_portrait_editorial_headers_pin_copy_and_divide_navigation(
    header, alignment, divider, logo_padding,
):
    source = (
        SKILL / "assets" / "headers_portrait" / f"{header}.html"
    ).read_text(encoding="utf-8")
    assert "align-self: start" in source
    assert f"text-align: {alignment}" in source
    assert divider in source
    assert logo_padding in source


@pytest.mark.parametrize("layout", ("full", "half"))
def test_portrait_base_header_uses_compact_opposing_rails(layout):
    source = (
        SKILL / "assets" / "layouts_portrait" / f"{layout}.html"
    ).read_text(encoding="utf-8")
    assert "--portrait-meta-track: 292pt" in source
    assert "--portrait-inst-track: 570pt" in source
    assert 'grid-template-areas: "mr tt ir"' in source
    assert "width: 132pt; height: 132pt" in source


def test_portrait_meta_rail_and_each_venue_line_are_visually_centered(tmp_path):
    """Center against divider-to-edge space, not only the inner grid track."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")

    posters = []
    for layout in ("full", "half"):
        for header in PORTRAIT_HEADERS:
            poster = tmp_path / layout / header / "poster.html"
            _build_renderable_portrait_header(
                poster, layout=layout, header=header,
            )
            posters.append((layout, header, poster))

    geometries = {}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available ({exc})")
        try:
            page = browser.new_page(viewport={"width": 3178, "height": 4493})
            for layout, header, poster in posters:
                page.goto(poster.as_uri(), wait_until="load", timeout=15_000)
                geometry = page.evaluate(r"""() => {
                  const box = (node) => {
                    const r = node.getBoundingClientRect();
                    return {
                      left:r.left, right:r.right, top:r.top, bottom:r.bottom,
                      width:r.width, height:r.height,
                      cx:r.left + r.width / 2, cy:r.top + r.height / 2,
                    };
                  };
                  const textBox = (node) => {
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    const r = range.getBoundingClientRect();
                    return {left:r.left, right:r.right, cx:r.left + r.width / 2};
                  };
                  const titlebar = document.querySelector('.titlebar');
                  const title = document.querySelector('.title-block');
                  const meta = document.querySelector('.meta-rail');
                  const institutions = document.querySelector('.institution-rail');
                  const qr = document.querySelector('.qr-block');
                  const venue = document.querySelector('.venue-badge');
                  const hr = box(titlebar), mr = box(meta), qrBox = box(qr);
                  const venueBox = box(venue);
                  const horizontalUtilities = titlebar.classList.contains('pv2');
                  const utilityGroup = {
                    left: Math.min(venueBox.left, qrBox.left),
                    right: Math.max(venueBox.right, qrBox.right),
                  };
                  utilityGroup.cx = (utilityGroup.left + utilityGroup.right) / 2;
                  const right = mr.cx > box(document.querySelector('.title-block')).cx;
                  const touchesLeft = Math.abs(mr.left - hr.left) <= 5;
                  const touchesRight = Math.abs(mr.right - hr.right) <= 5;
                  const compartment = touchesLeft
                    ? {left:hr.left, right:mr.right}
                    : touchesRight
                      ? {left:mr.left, right:hr.right}
                      : {left:mr.left, right:mr.right};
                  compartment.cx = (compartment.left + compartment.right) / 2;
                  return {
                    right,
                    edgeBound: touchesLeft || touchesRight,
                    horizontalUtilities,
                    header:hr,
                    meta:mr,
                    compartment,
                    qr:qrBox,
                    venue:venueBox,
                    utilityGroup,
                    titleAlignment:getComputedStyle(title).textAlign,
                    resources:{
                      'paper-info':box(title),
                      'institutions':box(institutions),
                      'venue-qr':box(meta),
                    },
                    resourceOrder:[
                      {name:'paper-info', ...box(title)},
                      {name:'institutions', ...box(institutions)},
                      {name:'venue-qr', ...box(meta)},
                    ].sort((a, b) => a.cx - b.cx).map((item) => item.name),
                    venueLines:[...document.querySelectorAll(
                      '.vb-venue, .vb-year, .vb-tag'
                    )].filter((node) => node.textContent.trim()).map((node) => ({
                      selector:'.' + node.className,
                      ...textBox(node),
                    })),
                  };
                }""")
                context = f"{layout}/{header}"
                geometries[(layout, header)] = geometry
                if header != "pv2":
                    assert geometry["edgeBound"], context
                if geometry["edgeBound"]:
                    outer_delta = min(
                        abs(geometry["meta"]["left"] - geometry["header"]["left"]),
                        abs(geometry["meta"]["right"] - geometry["header"]["right"]),
                    )
                    # Framed headers can contribute a 3-4px outer border; the rail
                    # still reaches the usable visual edge inside that frame.
                    assert outer_delta <= 5, context
                if geometry["horizontalUtilities"]:
                    assert abs(
                        geometry["utilityGroup"]["cx"] -
                        geometry["compartment"]["cx"]
                    ) <= 3, context
                else:
                    assert abs(
                        geometry["qr"]["cx"] - geometry["compartment"]["cx"]
                    ) <= 3, context
                for line in geometry["venueLines"]:
                    expected_center = (
                        geometry["venue"]["cx"]
                        if geometry["horizontalUtilities"] else
                        geometry["compartment"]["cx"]
                    )
                    assert abs(
                        line["cx"] - expected_center
                    ) <= 3, f'{context} {line["selector"]}'
                    assert line["left"] >= geometry["meta"]["left"] - 1
                    assert line["right"] <= geometry["meta"]["right"] + 1
                if header == "pv1":
                    assert geometry["resourceOrder"] == [
                        "venue-qr", "paper-info", "institutions",
                    ], context
                elif header == "pv3":
                    assert geometry["resourceOrder"] == [
                        "paper-info", "institutions", "venue-qr",
                    ], context
                elif header == "pv4":
                    assert geometry["resourceOrder"] == [
                        "institutions", "paper-info", "venue-qr",
                    ], context
                elif header == "pv5":
                    assert geometry["resourceOrder"] == [
                        "venue-qr", "institutions", "paper-info",
                    ], context

            for layout in ("full", "half"):
                pv1 = geometries[(layout, "pv1")]
                pv4 = geometries[(layout, "pv4")]
                context = f"{layout}/pv1-pv4"
                assert pv1["titleAlignment"] == "center", context
                assert pv4["titleAlignment"] == "center", context
                mirror_axis_twice = pv1["header"]["left"] + pv1["header"]["right"]
                assert abs(
                    mirror_axis_twice -
                    (pv4["header"]["left"] + pv4["header"]["right"])
                ) <= 1, context
                for resource in ("paper-info", "institutions", "venue-qr"):
                    left = pv1["resources"][resource]
                    right = pv4["resources"][resource]
                    assert abs(left["width"] - right["width"]) <= 1, (
                        context, resource, "width",
                    )
                    assert abs(left["height"] - right["height"]) <= 1, (
                        context, resource, "height",
                    )
                    assert abs(left["top"] - right["top"]) <= 1, (
                        context, resource, "top",
                    )
                    assert abs(
                        left["cx"] + right["cx"] - mirror_axis_twice
                    ) <= 1, (context, resource, "mirror-center")
        finally:
            browser.close()


def test_landscape_style_catalog_remains_all_eleven(tmp_path):
    installed = {
        path.stem for path in (SKILL / "assets" / "styles").glob("*.css")
    }
    assert installed == LANDSCAPE_STYLES

    output = tmp_path / "landscape" / "poster.html"
    with contextlib.redirect_stdout(io.StringIO()):
        compose_poster.compose(
            "half", "random", "v1", output,
            orientation="landscape", theme="blue", seed="landscape-catalog",
        )
    manifest = _composition(output)
    assert set(manifest["catalog"]["styles"]) == LANDSCAPE_STYLES


def test_portrait_random_catalog_excludes_two_incompatible_styles(tmp_path):
    resolved = set()
    for index in range(len(PORTRAIT_STYLES)):
        output = tmp_path / "portrait" / str(index) / "poster.html"
        with contextlib.redirect_stdout(io.StringIO()):
            compose_poster.compose(
                "half", "random", "pv1", output,
                orientation="portrait", theme="blue",
                variant_index=index, variant_seed="portrait-style-contract",
            )
        manifest = _composition(output)
        assert set(manifest["catalog"]["styles"]) == PORTRAIT_STYLES
        resolved.add(manifest["resolved"]["style"])
    assert resolved == PORTRAIT_STYLES

    for excluded in PORTRAIT_EXCLUDED_STYLES:
        with pytest.raises(SystemExit, match="Landscape-only"):
            compose_poster.compose(
                "half", excluded, "pv1",
                tmp_path / "rejected" / excluded / "poster.html",
                orientation="portrait", theme="blue",
            )


@pytest.mark.parametrize("returncode", [0, 7])
def test_render_autopack_invokes_fit_logos_and_reports_soft_failure(
    tmp_path, capsys, returncode,
):
    poster = tmp_path / "poster.html"
    poster.write_text("<!doctype html><title>poster</title>", encoding="utf-8")
    completed = subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout="  baked .titlebar .logo-block: 4 logo(s), fill=42%\n"
               "fit_logos: done -> poster.html\n",
        stderr="synthetic fitter error" if returncode else "",
    )

    with mock.patch("subprocess.run", return_value=completed) as run:
        render_poster._autopack_header_logos(poster)

    expected_fitter = SKILL / "references" / "fit_logos.py"
    run.assert_called_once_with(
        [sys.executable, str(expected_fitter), "--poster", str(poster)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    captured = capsys.readouterr()
    if returncode:
        assert f"WARN: fit_logos auto-pack exited with status {returncode}" in captured.err
        assert "synthetic fitter error" in captured.err
    else:
        assert "WARN: fit_logos auto-pack" not in captured.err
        assert "fit_logos: done" in captured.out


def test_repeated_logo_bake_reuses_largest_measured_zone_height():
    """A baked logo strip may collapse; the second bake must retain its real budget."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available ({exc})")
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 600})
            page.set_content("""
              <div class="titlebar">
                <div class="logo-block institution-rail" style="width:700px;height:180px">
                  <img class="logo" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='80' height='40'></svg>">
                </div>
              </div>
            """)
            args = {"sels": [".titlebar .logo-block"], "includeEmpty": False}
            first = page.evaluate(fit_logos._ZONES_JS, args)
            assert len(first) == 1
            original_height = first[0]["H"]
            assert original_height == pytest.approx(180, abs=1)

            page.eval_on_selector(
                ".logo-block", "node => { node.style.height = '42px'; }"
            )
            second = page.evaluate(fit_logos._ZONES_JS, args)
            assert second[0]["H"] == pytest.approx(original_height, abs=1)

            # A later template can legitimately offer more room. max(current, cached)
            # must self-heal the cache instead of pinning the old smaller measurement.
            page.eval_on_selector(
                ".logo-block", "node => { node.style.height = '220px'; }"
            )
            third = page.evaluate(fit_logos._ZONES_JS, args)
            assert third[0]["H"] == pytest.approx(220, abs=1)
            assert page.get_attribute(".logo-block", "data-lf-h0") == "220"
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("zone_class", "selector", "css"),
    (
        (
            "logo-grid",
            ".titlebar .logo-grid",
            """
              .titlebar { width: 900px; height: 220px; }
              .logo-grid { width: 320px; height: 180px; }
              .logo-grid:not(:has(.logo[src]:not([src='']))) { display:none; }
            """,
        ),
        (
            "logo-block institution-rail",
            ".titlebar .logo-block",
            """
              .titlebar {
                --inst-track: 320px;
                width: 900px; height: 220px;
                display:grid; grid-template-columns: 180px 1fr var(--inst-track);
              }
              .titlebar:not(:has(.institution-rail img[src]:not([src='']))) {
                --inst-track: 0px;
              }
              .institution-rail { grid-column:3; height:180px; }
              .institution-rail:not(:has(.logo[src]:not([src='']))) { display:none; }
            """,
        ),
    ),
    ids=("landscape", "portrait"),
)
def test_disk_logo_preseed_reveals_hidden_zone_for_fitting(
    zone_class, selector, css,
):
    """A real disk mark must reveal a CSS-hidden logo zone before measurement."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")

    logo = (
        "data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' width='120' height='60'></svg>"
    )
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available ({exc})")
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 600})
            page.set_content(
                f"<style>{css}</style><header class='titlebar'>"
                f"<div class='{zone_class}'><img class='logo' src=''></div>"
                "</header>"
            )
            assert page.locator(selector).evaluate(
                "node => getComputedStyle(node).display"
            ) == "none"
            assert page.evaluate(
                fit_logos._ZONES_JS,
                {"sels": [selector], "includeEmpty": True},
            ) == []

            assert page.evaluate(
                fit_logos._PRESEED_EMPTY_ZONE_JS,
                {"sels": [selector], "src": logo},
            ) is True
            zones = page.evaluate(
                fit_logos._ZONES_JS,
                {"sels": [selector], "includeEmpty": True},
            )
            assert len(zones) == 1
            assert zones[0]["W"] >= 300
            assert zones[0]["H"] >= 170
            assert len(zones[0]["logos"]) == 1
        finally:
            browser.close()


def test_no_disk_logo_keeps_empty_logo_zone_hidden():
    """The preseed helper is a no-op when disk discovery found no source."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available ({exc})")
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 600})
            page.set_content("""
              <style>
                .logo-block { width:320px; height:180px; }
                .logo-block:not(:has(.logo[src]:not([src='']))) { display:none; }
              </style>
              <header class="titlebar">
                <div class="logo-block"><img class="logo" src=""></div>
              </header>
            """)
            assert page.evaluate(
                fit_logos._PRESEED_EMPTY_ZONE_JS,
                {"sels": [".titlebar .logo-block"], "src": ""},
            ) is False
            assert page.locator(".logo-block").evaluate(
                "node => getComputedStyle(node).display"
            ) == "none"
            assert page.evaluate(
                fit_logos._ZONES_JS,
                {"sels": [".titlebar .logo-block"], "includeEmpty": False},
            ) == []
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("canvas", "zone_class", "css"),
    (
        (
            "60in 36in",
            "logo-grid",
            """
              .titlebar { width:900px; height:220px; }
              .logo-grid { width:320px; height:180px; }
              .logo-grid:not(:has(.logo[src]:not([src='']))) { display:none; }
            """,
        ),
        (
            "33.1in 46.8in",
            "logo-block institution-rail",
            """
              .titlebar {
                --inst-track:320px;
                width:900px; height:220px;
                display:grid; grid-template-columns:180px 1fr var(--inst-track);
              }
              .titlebar:not(:has(.institution-rail img[src]:not([src='']))) {
                --inst-track:0px;
              }
              .institution-rail { grid-column:3; height:180px; }
              .institution-rail:not(:has(.logo[src]:not([src='']))) { display:none; }
            """,
        ),
    ),
    ids=("landscape", "portrait"),
)
def test_bake_autocompletes_css_hidden_zone_from_disk(
    tmp_path, canvas, zone_class, css,
):
    """The public bake path must discover and pack every on-disk institution mark."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available ({exc})")
        else:
            browser.close()

    logos = tmp_path / "assets" / "logos"
    logos.mkdir(parents=True)
    for name, width in (("inst-a.svg", 120), ("inst-b.svg", 180)):
        (logos / name).write_text(
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' "
            "height='60'><rect width='100%' height='100%' fill='navy'/></svg>",
            encoding="utf-8",
        )
    poster = tmp_path / "poster.html"
    poster.write_text(
        f"<!doctype html><style>@page {{ size:{canvas}; margin:0; }}{css}</style>"
        f"<header class='titlebar'><div class='{zone_class}'>"
        "<img class='logo' src=''></div></header>",
        encoding="utf-8",
    )

    baked = fit_logos.bake(poster)
    result = poster.read_text(encoding="utf-8")
    assert baked
    assert "assets/logos/inst-a.svg" in result
    assert "assets/logos/inst-b.svg" in result
    assert 'data-lf-fill="' in result
