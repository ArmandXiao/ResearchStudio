"""Regression tests for polish Gate H (content-pattern diversity)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "references"))

import fit_logos  # noqa: E402
from utils import polish as _polish  # noqa: E402
from utils import render as _render  # noqa: E402


@pytest.fixture
def page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page(viewport={"width": 1200, "height": 900})
            yield pg
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium not available ({exc})")


def _collect_widgets(page, cards: str) -> dict:
    page.set_content(
        "<style>.poster{width:1100px}.col{display:block}"
        ".section{display:block;min-height:50px}</style>"
        f'<div class="poster"><div class="col">{cards}</div></div>'
    )
    _render.inject_class_fallback_roles(page)
    return _polish.collect_polish_data(page)["data"]["widgetDiversity"]


def test_supported_families_are_exact_and_timeline_variants_count_once(page):
    widgets = _collect_widgets(page, """
      <div class="section" data-section="a"><div class="p-callout-primary">A</div></div>
      <div class="section" data-section="b"><div class="p-callout-soft">B</div></div>
      <div class="section" data-section="c"><div class="p-callout-bar">C</div></div>
      <div class="section" data-section="d"><div class="p-key-stat">D</div></div>
      <div class="section" data-section="e"><div class="p-stat-strip">E</div></div>
      <div class="section" data-section="f"><div class="p-vs">F</div></div>
      <div class="section" data-section="g"><div class="p-steps">G</div></div>
      <div class="section" data-section="h"><div class="p-chips">H</div></div>
      <div class="section" data-section="i"><table class="p-table"><tr><td>I</td></tr></table></div>
      <div class="section" data-section="j"><div class="p-eq">J</div></div>
      <div class="section" data-section="k">
        <div class="p-timeline-cards">E</div>
        <div class="p-timeline-pills">same semantic family</div>
      </div>
      <div class="section" data-section="l"><div class="p-banner">L</div></div>
    """)
    assert widgets["families"] == [
        "banner", "callout-bar", "callout-primary", "callout-soft", "chips",
        "equation", "highlight-table", "key-stat", "numbered-steps",
        "stat-strip", "timeline", "vs",
    ]
    assert widgets["widgetless_sections"] == []


def test_per_section_exemptions_are_narrow_and_chrome_is_not_a_widget(page):
    widgets = _collect_widgets(page, """
      <div class="section card callout stat" data-section="plain"><h2>Plain</h2><p>Only prose.</p></div>
      <div class="section" data-section="title"><h2>Title</h2></div>
      <div class="section method-text" data-section="method-text"><ul><li>Method copy</li></ul></div>
      <div class="section" data-section="headline-numbers"><div class="headline-hero">42</div></div>
      <div class="section" data-section="scan-to-read"><div>Scan me</div></div>
      <div class="section" data-section="figure"><h2>Figure</h2><figure><svg width="20" height="20"></svg><figcaption>Cap</figcaption></figure></div>
      <div class="section" data-section="headline-numbers"><p>42</p></div>
      <div class="section" data-section="figure-plus-prose"><figure><svg width="20" height="20"></svg></figure><p>Interpretation.</p></div>
    """)
    assert widgets["families"] == []
    assert [s["section"] for s in widgets["widgetless_sections"]] == [
        "plain", "headline-numbers", "figure-plus-prose",
    ]
    assert {s["reason"] for s in widgets["exempt_sections"]} == {
        "title", "method-text", "headline-numbers-hero", "scan",
        "figure-only",
    }


def test_hidden_or_zero_area_widgets_do_not_satisfy_gate_h(page):
    widgets = _collect_widgets(page, """
      <div class="section" data-section="hidden">
        <p>Visible prose only.</p>
        <div class="p-callout-primary" style="display:none">hidden</div>
        <div class="p-key-stat" style="visibility:hidden">hidden</div>
        <div class="p-chips" style="opacity:0">hidden</div>
        <div class="p-eq" style="width:0;height:0;overflow:hidden">hidden</div>
        <div class="p-banner" hidden>hidden</div>
        <div style="opacity:0"><div class="p-vs">ancestor opacity</div></div>
        <div style="content-visibility:hidden">
          <div class="p-steps">ancestor content visibility</div>
        </div>
      </div>
    """)
    assert widgets["families"] == []
    assert [s["section"] for s in widgets["widgetless_sections"]] == ["hidden"]


def test_empty_visible_logo_zone_is_available_for_disk_autocomplete(page):
    page.set_content("""
      <style>
        .titlebar { width: 900px; height: 240px; }
        .logo-grid { width: 700px; height: 180px; }
        .hidden { display: none; }
      </style>
      <div class="titlebar">
        <div class="logo-grid"></div>
        <div class="logo-grid hidden"></div>
      </div>
    """)
    assert page.evaluate(fit_logos._ZONES_JS, [".titlebar .logo-grid"]) == []
    zones = page.evaluate(
        fit_logos._ZONES_JS,
        {"sels": [".titlebar .logo-grid"], "includeEmpty": True},
    )
    assert len(zones) == 1
    assert zones[0]["logos"] == []
    assert zones[0]["qrs"] == []
    assert zones[0]["W"] > 8
    assert zones[0]["H"] > 8


def _collected(families: list[str], widgetless: list[dict]) -> dict:
    return {
        "data": {
            "figures": [], "orphans": [], "cols": [], "cards": [],
            "innerVoids": [], "flexbr": [], "widows": [],
            "widgetDiversity": {
                "families": families,
                "widgetless_sections": widgetless,
                "exempt_sections": [],
            },
        },
        "mid_data": {},
    }


def test_diversity_and_missing_widget_use_existing_warning_strict_contract(
        capsys):
    args = _polish.default_polish_args()
    collected = _collected(
        ["banner", "chips", "equation", "key-stat"],
        [{"card_index": 2, "section": "motivation"}],
    )

    assert _polish.report_polish(collected, args, Path("poster.html")) == 0
    advisory = capsys.readouterr()
    assert "WIDGET/DIVERSITY" in advisory.out
    assert "WIDGET/MISSING: section 'motivation'" in advisory.out
    assert "[polish] OK (warnings only)" in advisory.out

    args.strict = True
    assert _polish.report_polish(collected, args, Path("poster.html")) == 1
    strict = capsys.readouterr()
    assert "[polish] FAIL" in strict.err


def test_five_families_and_widgetful_sections_add_no_gate_h_warning(capsys):
    args = _polish.default_polish_args()
    collected = _collected(
        ["banner", "chips", "equation", "key-stat", "timeline"], []
    )
    assert _polish.report_polish(collected, args, Path("poster.html")) == 0
    output = capsys.readouterr().out
    assert "WIDGET/DIVERSITY" not in output
    assert "WIDGET/MISSING" not in output
    assert "[polish] PASS" in output
