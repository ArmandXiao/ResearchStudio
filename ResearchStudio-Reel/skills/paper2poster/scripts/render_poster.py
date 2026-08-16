#!/usr/bin/env python3
"""render_preview - render a poster HTML to print-ready PDF + thumbnail.

Canvas-agnostic: reads ``@page { size: <W> <H> }`` from the input HTML
or accepts ``--canvas '<W>x<H>in'`` / ``--canvas 'A0 portrait'`` as
override. Print-emulates Chromium so MathJax typesets against the
``@media print`` layout from the start.

This is the SOFT path (vs the HARD ``measure`` gate): a MathJax
typeset timeout or a missing ``<mjx-container>`` warns and continues
— users would rather see raw ``$…$`` on the rendered PDF than a
silent abort.

Outputs:
    <stem>.pdf   exact-size PDF
    <stem>.png   scaled thumbnail (default 0.35×)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make `utils` importable when run directly.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from utils import canvas as _canvas  # noqa: E402
from utils import render as _render  # noqa: E402
from utils.cli_common import eprint as _eprint, import_playwright  # noqa: E402
from utils.font_fidelity import (  # noqa: E402
    freeze_system_font_webfont,
    managed_font_asset_names,
)
from utils.textutil import ascii_safe  # noqa: E402


_PUBLIC_ARTIFACT_MODE = 0o644
_FONT_ROLLBACK_PREFIX = ".poster-font-rollback."
_EXPAND_GAP_TOLERANCE_PX = 0.1
_EXPAND_GEOMETRY_TOLERANCE_PX = 1.0
_EXPAND_MEDIA_REL_TOLERANCE = 0.005
_EXPAND_FIG_MIN_RATIO = 0.90
_EXPAND_FIG_MAX_RATIO = 1.01
_LAYOUT_TIMER_GUARD_ID = "poster-layout-timer-guard"


def _pdf_content_scale(
    canvas_in: tuple[float, float], viewport_px: tuple[int, int]
) -> float:
    """Keep a rounded Chromium viewport inside the exact physical page.

    ``viewport_for`` must return integer CSS pixels, while a decimal-inch
    canvas such as Portrait A0 (33.1 x 46.8 in) maps to fractional pixels at
    96 ppi.  When rounding goes up, printing the integer-sized document at
    scale 1 can spill a fraction of a pixel onto a blank second PDF page.
    Scale only by that rounding delta; integer canvases such as 60 x 36 in
    remain exactly 1.0.
    """
    w_in, h_in = canvas_in
    w_px, h_px = viewport_px
    if w_px <= 0 or h_px <= 0:
        return 1.0
    return min(1.0, (w_in * 96.0) / w_px, (h_in * 96.0) / h_px)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    p.add_argument("html", help="poster HTML file")
    p.add_argument(
        "--pdf", default=None,
        help="output PDF path (default: <stem>.pdf)",
    )
    p.add_argument(
        "--png", default=None,
        help="output PNG thumbnail path (default: <stem>.png)",
    )
    p.add_argument(
        "--thumb-scale", type=float, default=0.35,
        help="thumbnail scale factor (default 0.35)",
    )
    p.add_argument(
        "--mathjax-timeout-ms", type=int, default=15000,
        help="timeout for MathJax typesetting (default 15000); "
             "render is the SOFT path; timeout warns, not fails",
    )
    p.add_argument(
        "--canvas", type=_canvas.parse_canvas_arg, default=None,
        help="override canvas (e.g. '60x36in' / 'A0 portrait'); "
             "by default we parse @page from the HTML",
    )
    return p


def _copy_asset_atomic(
    source: Path,
    target: Path,
    *,
    mode: int | None = None,
) -> None:
    """Copy one renderer-managed asset without exposing partial bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".asset-next",
    )
    os.close(descriptor)
    temporary = Path(raw)
    try:
        shutil.copy2(source, temporary)
        if mode is not None:
            temporary.chmod(mode)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _renderer_font_asset_names() -> frozenset[str]:
    """Return the closed set of font files a render may add or replace."""
    skill_fonts = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    bundled = (
        {path.name for path in skill_fonts.glob("*.woff2")}
        if skill_fonts.is_dir()
        else set()
    )
    return frozenset(bundled | set(managed_font_asset_names()))


class _FontAssetJournal:
    """Restore renderer-managed ``assets/fonts`` files if a render fails.

    The staged HTML remains adjacent to the deliverable so every existing
    relative figure/logo/QR URL resolves exactly as it will after promotion.
    That means font preparation also targets the adjacent live ``assets``
    directory.  Each individual write is atomic, and this journal extends the
    HTML/PDF/PNG rollback contract to the complete set of font dependencies.
    """

    def __init__(self, html_path: Path):
        self.fonts_dir = html_path.parent / "assets" / "fonts"
        self.fonts_dir_existed = self.fonts_dir.is_dir()
        if self.fonts_dir.is_symlink() or (
            self.fonts_dir.exists() and not self.fonts_dir.is_dir()
        ):
            raise RuntimeError(
                f"font asset path must be a real directory: {self.fonts_dir}"
            )
        self.backup_dir = Path(
            tempfile.mkdtemp(
                dir=html_path.parent,
                prefix=_FONT_ROLLBACK_PREFIX,
            )
        )
        self.backup_dir.chmod(0o700)
        self.originals: dict[str, Path | None] = {}
        self.closed = False
        try:
            for name in sorted(_renderer_font_asset_names()):
                target = self.fonts_dir / name
                if target.is_symlink() or (
                    target.exists() and not target.is_file()
                ):
                    raise RuntimeError(
                        f"font asset target must be a regular file: {target}"
                    )
                if target.is_file():
                    saved = self.backup_dir / name
                    shutil.copy2(target, saved)
                    self.originals[name] = saved
                else:
                    self.originals[name] = None
        except BaseException:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            raise

    def rollback(self) -> None:
        if self.closed:
            return
        errors: list[str] = []
        try:
            for name, saved in self.originals.items():
                target = self.fonts_dir / name
                try:
                    if saved is None:
                        if target.is_symlink() or target.is_file():
                            target.unlink()
                        elif target.exists():
                            raise RuntimeError(
                                "new font asset became a non-file target"
                            )
                    else:
                        _copy_asset_atomic(saved, target)
                except Exception as exc:  # keep restoring the remaining set
                    errors.append(f"{target}: {exc}")
            if (
                not self.fonts_dir_existed
                and self.fonts_dir.is_dir()
                and not any(self.fonts_dir.iterdir())
            ):
                self.fonts_dir.rmdir()
                assets_dir = self.fonts_dir.parent
                if assets_dir.is_dir() and not any(assets_dir.iterdir()):
                    assets_dir.rmdir()
        finally:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.closed = True
        if errors:
            raise RuntimeError(
                "font asset rollback was incomplete: " + "; ".join(errors)
            )

    def commit(self) -> None:
        if self.closed:
            return
        try:
            shutil.rmtree(self.backup_dir)
        except OSError as exc:
            _eprint(
                f"[render_preview] WARN: could not remove font rollback "
                f"journal {ascii_safe(self.backup_dir)}: {ascii_safe(exc)}"
            )
        self.closed = True


def _sync_bundled_fonts(html_path: Path) -> None:
    """Mirror the skill's bundled Inter webfonts into <html_dir>/assets/fonts/
    so the @font-face URLs in the template (relative `assets/fonts/Inter-*.woff2`)
    resolve when playwright loads the page. Idempotent — only copies missing
    or stale files. Silent no-op if the skill's fonts/ subdir doesn't exist
    (e.g. user editing a template that doesn't use bundled webfonts).

    Why mirror instead of symlink: the deliverable folder needs to be
    self-contained so a reviewer can zip + share it; a symlink into the
    skill assets would break once the folder leaves this machine.
    """
    import shutil
    skill_fonts = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    if not skill_fonts.is_dir():
        return
    out_fonts = html_path.parent / "assets" / "fonts"
    out_fonts.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in skill_fonts.glob("*.woff2"):
        dst = out_fonts / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            _copy_asset_atomic(src, dst, mode=_PUBLIC_ARTIFACT_MODE)
            copied += 1
    if copied:
        _eprint(f"[render_preview] mirrored {copied} font file(s) -> "
                f"{out_fonts.relative_to(html_path.parent)}/")


def _strip_derived_style_block(text: str, style_id: str) -> tuple[str, int]:
    """Remove a renderer-owned style block from HTML text.

    Expand and scan suppression are derived from the current DOM and current
    environment knobs.  Keeping an older block in the source while measuring a
    rerender makes that stale output influence its own replacement (and makes a
    ``0`` override unable to disable it), so both blocks are stripped before
    the first browser navigation and rebuilt from the fresh layout.
    """
    import re

    pattern = re.compile(
        rf'\s*<style\b(?=[^>]*\bid\s*=\s*["\']{re.escape(style_id)}["\'])'
        rf'[^>]*>.*?</style>\s*',
        flags=re.IGNORECASE | re.DOTALL,
    )
    return pattern.subn("\n", text)


def _strip_derived_render_styles(html_path: Path) -> set[str]:
    """Strip stale expand/scan renderer output before a fresh measurement."""
    text = html_path.read_text(encoding="utf-8")
    removed: set[str] = set()
    for style_id in ("poster-expand-baked", "poster-scan-suppress"):
        text, count = _strip_derived_style_block(text, style_id)
        if count:
            removed.add(style_id)
    if removed:
        html_path.write_text(text, encoding="utf-8")
    return removed


def _append_style_at_end(text: str, block: str) -> str:
    """Append a durable style after every existing author style.

    The final ``</body>`` is the stable insertion point used by the generated
    posters.  Falling back to EOF keeps malformed/minimal fixtures usable.
    """
    import re

    closes = list(re.finditer(r"</body\s*>", text, flags=re.IGNORECASE))
    if not closes:
        return text.rstrip() + "\n" + block + "\n"
    at = closes[-1].start()
    return text[:at].rstrip() + "\n" + block + "\n" + text[at:]


def _ensure_unscaled_layout_timer_guard(html_path: Path) -> bool:
    """Run recurring geometry fitters against the unscaled poster canvas.

    Some legacy/model-authored posters keep a figure fitted with a short
    ``setInterval`` callback that reads ``getBoundingClientRect()``.  The
    standalone poster controller deliberately scales the complete fixed canvas
    with a CSS transform.  A timer that reads the transformed rectangle and
    writes that screen-space width back as CSS pixels applies the scale twice.

    Install a tiny early guard only when an authored script contains both
    primitives.  It preserves timer behavior and arguments, but temporarily
    clears the poster's exact inline transform around each interval callback,
    then restores the same value and priority in ``finally``.  This deliberately
    does not call ``__fitPosterStage``: that controller recomputes a transform
    from the viewport and would overwrite an external thumbnail/backfill scale.
    Wrapping every interval on an affected legacy page also covers callbacks
    written as ``() => enforce()`` whose own source hides the geometry read.
    The block is renderer-owned and idempotent so rerenders never accumulate
    shims.
    """
    import re

    original = html_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'\s*<script\b(?=[^>]*\bid\s*=\s*["\']'
        rf'{re.escape(_LAYOUT_TIMER_GUARD_ID)}["\'])[^>]*>.*?</script>\s*',
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = pattern.sub("\n", original)
    authored_scripts = re.findall(
        r"<script\b[^>]*>(.*?)</script>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # The scheduler and the fitter are often authored in separate blocks
    # (for example, one block exports ``fit`` and a later block installs the
    # interval).  Treat the page's authored scripts as one program when
    # deciding whether the guard is required; requiring both primitives in a
    # single ``<script>`` silently misses that common split form.
    authored_program = "\n".join(authored_scripts)
    needs_guard = (
        "setInterval" in authored_program
        and "getBoundingClientRect" in authored_program
    )
    if needs_guard:
        guard = f'''<script id="{_LAYOUT_TIMER_GUARD_ID}">
(() => {{
  const nativeSetInterval = window.setInterval.bind(window);
  window.setInterval = function(callback, delay, ...args) {{
    if (typeof callback !== 'function')
      return nativeSetInterval(callback, delay, ...args);
    const guarded = function(...tickArgs) {{
      const invoke = () => callback.apply(window, tickArgs);
      const poster = document.querySelector('[data-measure-role="poster"]')
        || document.querySelector('.poster')
        || document.body;
      if (!poster) return invoke();
      const oldTransform = poster.style.getPropertyValue('transform');
      const oldPriority = poster.style.getPropertyPriority('transform');
      try {{
        poster.style.setProperty('transform', 'none', 'important');
        return invoke();
      }} finally {{
        if (oldTransform)
          poster.style.setProperty('transform', oldTransform, oldPriority);
        else
          poster.style.removeProperty('transform');
      }}
    }};
    return nativeSetInterval(guarded, delay, ...args);
  }};
}})();
</script>'''
        opening_head = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
        if opening_head:
            at = opening_head.end()
            text = text[:at] + "\n" + guard + text[at:]
        else:
            text = guard + "\n" + text
    if text == original:
        return False
    html_path.write_text(text, encoding="utf-8")
    return True


def _bake_expand_into_html(html_path: Path, baked: list) -> bool:
    """Persist the render-time expand into the deliverable poster.html.

    The expand pass grows each under-filled card's inner row-gaps in the live
    DOM before printing the PDF/PNG. To keep the editable poster.html (its `D`
    debug overlay, and the downstream html2pptx read) consistent with the
    rendered PDF/PNG, write the resulting row-gaps back as one
    <style id="poster-expand-baked"> block. Responsive-safe: the templates use a
    fixed internal layout scaled by an outer `transform: scale()`, so an inline
    px row-gap renders identically at any view size. Idempotent -- a re-render
    strips the old derived block before measuring and appends this fresh block
    after every other author style, so an older/newer autofit block cannot win
    again on reload. Written only at final render (after the fill loop), so the
    loop still measures the natural layout.
    """
    original = html_path.read_text(encoding="utf-8")
    txt, _ = _strip_derived_style_block(original, "poster-expand-baked")
    if baked:
        rules = "\n".join(
            f'  .section[data-section="{sid}"]{{ row-gap: {gap} !important; }}'
            for sid, gap in baked
        )
        block = f'<style id="poster-expand-baked">\n{rules}\n</style>'
        txt = _append_style_at_end(txt, block)
    if txt == original:
        return False
    html_path.write_text(txt, encoding="utf-8")
    return True


_EXPAND_SNAPSHOT_JS = r"""
() => {
  const rectOf = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x:r.x, y:r.y, w:r.width, h:r.height};
  };
  // Match the polish gate for object-fit:contain. scale-down differs when the
  // intrinsic image is already smaller than its CSS box, so preserve its
  // unscaled natural size in that case.
  const paintedDims = (img, r) => {
    const nw = img.naturalWidth || 0;
    const nh = img.naturalHeight || 0;
    const fit = getComputedStyle(img).objectFit || 'fill';
    if (nw <= 0 || nh <= 0 || r.width <= 0 || r.height <= 0)
      return {w:r.width, h:r.height, nw:nw, nh:nh, fit:fit};
    const boxAR = r.width / r.height;
    const natAR = nw / nh;
    const contained = natAR > boxAR
      ? {w:r.width, h:r.width / natAR}
      : {w:r.height * natAR, h:r.height};
    if (fit === 'contain')
      return {...contained, nw:nw, nh:nh, fit:fit};
    if (fit === 'scale-down') {
      const scaled = contained.w <= nw && contained.h <= nh;
      return scaled
        ? {...contained, nw:nw, nh:nh, fit:fit}
        : {w:nw, h:nh, nw:nw, nh:nh, fit:fit};
    }
    if (fit === 'none')
      return {w:Math.min(nw, r.width), h:Math.min(nh, r.height),
              nw:nw, nh:nh, fit:fit};
    // fill and cover both paint the visible element box completely.
    return {w:r.width, h:r.height, nw:nw, nh:nh, fit:fit};
  };
  const sectionNodes = Array.from(
    document.querySelectorAll('.section[data-section]')
  );
  const parentNodes = [];
  const parentKey = node => {
    if (!node) return 'parent:none';
    let index = parentNodes.indexOf(node);
    if (index < 0) { parentNodes.push(node); index = parentNodes.length - 1; }
    return 'parent:' + index;
  };
  const sections = sectionNodes.map((sec, index) => ({
    key:'section:' + index,
    sid:sec.getAttribute('data-section') || '',
    parentKey:parentKey(sec.parentElement),
    rect:rectOf(sec),
    parentRect:rectOf(sec.parentElement),
    rowGap:parseFloat(getComputedStyle(sec).rowGap) || 0,
  }));
  const media = [];
  sectionNodes.forEach((sec, sectionIndex) => {
    const section = sections[sectionIndex];
    const eligibleImages = new Set();
    Array.from(sec.querySelectorAll('img')).forEach((img, imageIndex) => {
      if (img.closest('.section[data-section]') !== sec) return;
      const r = img.getBoundingClientRect();
      if (r.width < 50 || r.height < 1) return;
      eligibleImages.add(img);
      const p = paintedDims(img, r);
      media.push({
        key:section.key + '|img:' + imageIndex,
        sectionKey:section.key, sid:section.sid,
        parentKey:section.parentKey, kind:'img',
        src:img.getAttribute('src') || '',
        currentSrc:img.currentSrc || '',
        boxW:r.width, boxH:r.height,
        paintedW:p.w, paintedH:p.h,
        nw:p.nw, nh:p.nh, fit:p.fit,
      });
    });
    // A fixture or legacy poster may use a painted .figure block instead of
    // an <img>. Keep these fallbacks even when another real image exists
    // elsewhere in the same card.
    Array.from(sec.querySelectorAll('figure, .figure'))
      .forEach((node, figureIndex) => {
        if (node.closest('.section[data-section]') !== sec) return;
        if (Array.from(node.querySelectorAll('img'))
            .some(img => eligibleImages.has(img))) return;
        const r = node.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return;
        media.push({
          key:section.key + '|figure:' + figureIndex,
          sectionKey:section.key, sid:section.sid,
          parentKey:section.parentKey, kind:'figure',
          src:'', currentSrc:'', boxW:r.width, boxH:r.height,
          paintedW:r.width, paintedH:r.height,
          nw:0, nh:0, fit:'fallback',
        });
      });
  });
  return {sections:sections, media:media};
}
"""


def _capture_expand_snapshot(page) -> dict:
    """Capture the natural section/parent/media geometry in one JS source."""
    result = page.evaluate(_EXPAND_SNAPSHOT_JS)
    if not isinstance(result, dict):
        raise RuntimeError("expand snapshot returned malformed data")
    return result


def _capture_card_figure_fills(page) -> list[dict]:
    """Measure every visible card image/figure against its owning section."""
    snapshot = _capture_expand_snapshot(page)
    sections = {
        str(section.get("key", "")): section
        for section in snapshot.get("sections", [])
    }
    rows = []
    for media in snapshot.get("media", []):
        section = sections.get(str(media.get("sectionKey", "")))
        rect = section.get("rect") if section else None
        section_w = float((rect or {}).get("w", 0.0))
        section_h = float((rect or {}).get("h", 0.0))
        if section_w <= 0 or section_h <= 0:
            continue
        painted_w = float(media.get("paintedW", 0.0))
        painted_h = float(media.get("paintedH", 0.0))
        rows.append({
            "key": str(media.get("key", "")),
            "sid": str(media.get("sid", "")),
            "kind": str(media.get("kind", "")),
            "src": str(media.get("src", "")),
            "currentSrc": str(media.get("currentSrc", "")),
            "fill": max(painted_w / section_w, painted_h / section_h),
            "widthFill": painted_w / section_w,
            "heightFill": painted_h / section_h,
        })
    return rows


def _font_freeze_failures(
    before: list[dict],
    after: list[dict],
    *,
    floor: float = _EXPAND_FIG_MIN_RATIO,
    ceiling: float = _EXPAND_FIG_MAX_RATIO,
) -> list[dict]:
    """Return identity or fill-band regressions caused by font freezing."""
    after_by_key = {
        str(figure.get("key", "")): figure for figure in after
    }
    failures = []
    for original in before:
        current = after_by_key.get(str(original.get("key", "")))
        reasons = []
        if current is None:
            reasons.append("media disappeared")
        else:
            for identity in ("kind", "src", "currentSrc"):
                if original.get(identity) != current.get(identity):
                    reasons.append(f"{identity} changed")
            before_fill = float(original.get("fill", 0.0))
            current_fill = float(current.get("fill", 0.0))
            if before_fill + 1e-6 >= floor and current_fill + 1e-6 < floor:
                reasons.append(
                    f"fill dropped below floor ({before_fill:.2%} -> "
                    f"{current_fill:.2%})"
                )
            for axis in ("widthFill", "heightFill"):
                before_axis = float(original.get(axis, 0.0))
                current_axis = float(current.get(axis, 0.0))
                if before_axis <= ceiling and current_axis > ceiling:
                    reasons.append(
                        f"{axis} overflowed ({before_axis:.2%} -> "
                        f"{current_axis:.2%})"
                    )
        if reasons:
            failures.append({
                "before": original,
                "after": current,
                "reasons": reasons,
            })
    before_keys = {str(figure.get("key", "")) for figure in before}
    for current in after:
        if str(current.get("key", "")) not in before_keys:
            failures.append({
                "before": None,
                "after": current,
                "reasons": ["media appeared"],
            })
    return failures


def _wait_for_images_decoded(page, *, timeout_ms: int, label: str) -> bool:
    """Wait until every document image has loaded and decoded.

    A same-context reload can reuse Chromium's decoded-image cache while a
    standalone reopen cannot.  Durable geometry must therefore be measured
    only after a bounded decode wait in the fresh context used for capture.
    """
    try:
        status = page.evaluate(
            """timeoutMs => Promise.race([
              Promise.all(Array.from(document.images).map(async img => {
                if (!img.complete) {
                  await new Promise(resolve => {
                    img.addEventListener('load', resolve, {once:true});
                    img.addEventListener('error', resolve, {once:true});
                  });
                }
                if (typeof img.decode === 'function') {
                  try { await img.decode(); } catch (_) {}
                }
                return img.complete && img.naturalWidth > 0
                  && img.naturalHeight > 0;
              })).then(results => results.every(Boolean) ? 'ok' : 'failed'),
              new Promise(resolve => setTimeout(
                () => resolve('timeout'), timeoutMs)),
            ])""",
            timeout_ms,
        )
    except Exception as exc:
        _eprint(
            f"[render_preview] WARN: {label} image decode check failed: "
            f"{ascii_safe(exc)}."
        )
        return False
    if status != "ok":
        _eprint(
            f"[render_preview] WARN: {label} images did not decode cleanly "
            f"within {timeout_ms} ms ({ascii_safe(status)})."
        )
        return False
    return True


def _validate_durable_expand(
    page,
    records: list[dict],
    baseline: dict,
) -> dict:
    """Validate provisional rules against the whole natural poster snapshot.

    A gap added in one card can make an on-load fitter shrink a figure in a
    sibling card.  Looking only inside the candidate card would miss that
    collateral change, so validation covers every section, its immediate
    parent, and every visible card image/figure fallback.  Failures remove all
    rules in the affected original parent scope; an un-attributable failure
    removes every remaining rule (fail closed).
    """
    after = _capture_expand_snapshot(page)
    result = page.evaluate(
        """({records, baseline, after, gapTol, geomTol,
              mediaRelTol, figMin, figMax}) => {
          const beforeSections = Array.isArray(baseline && baseline.sections)
            ? baseline.sections : [];
          const beforeMedia = Array.isArray(baseline && baseline.media)
            ? baseline.media : [];
          const close = (a, b, tol) => Number.isFinite(a)
            && Number.isFinite(b) && Math.abs(a - b) <= tol;
          const rectClose = (a, b) => !!a && !!b
            && ['x', 'y', 'w', 'h'].every(k => close(a[k], b[k], geomTol));
          // One CSS pixel is the maximum media drift.  The 0.5% allowance is
          // only a tighter tolerance for small media, never a looser large-box
          // tolerance.
          const mediaTol = (a, b) => Math.min(
            geomTol,
            mediaRelTol * Math.max(Math.abs(a), Math.abs(b)),
          );
          const scopedReasons = new Map();
          const globalReasons = [];
          const addFailure = (scope, reason) => {
            const key = String(scope || '');
            if (!key) { globalReasons.push(reason); return; }
            if (!scopedReasons.has(key)) scopedReasons.set(key, []);
            const reasons = scopedReasons.get(key);
            if (!reasons.includes(reason)) reasons.push(reason);
          };
          const recordsByScope = new Map();
          records.forEach(record => {
            const scope = String(record.scope || '');
            if (!recordsByScope.has(scope)) recordsByScope.set(scope, []);
            recordsByScope.get(scope).push(record);
          });

          const style = document.getElementById('poster-expand-baked');
          if (!!style !== (records.length > 0))
            globalReasons.push('persisted expand style presence does not match rules');
          if (document.documentElement.dataset.posterExpandEphemeral)
            globalReasons.push('page still carries a live-only expand marker');

          const beforeSectionByKey = new Map(
            beforeSections.map(item => [item.key, item])
          );
          const sectionByKey = new Map(after.sections.map(item => [item.key, item]));
          const currentNodes = Array.from(
            document.querySelectorAll('.section[data-section]')
          );
          for (const record of records) {
            const sid = String(record.sid || '');
            const matches = currentNodes.filter(
              sec => sec.getAttribute('data-section') === sid
            );
            if (matches.length !== 1) {
              addFailure(record.scope,
                'expected one durable section for ' + sid + ', found ' + matches.length);
              continue;
            }
            const targetGap = parseFloat(record.gap);
            const durableGap = parseFloat(getComputedStyle(matches[0]).rowGap);
            if (!close(targetGap, durableGap, gapTol))
              addFailure(record.scope, 'row-gap lost after reload: ' + sid);
          }

          if (beforeSections.length !== after.sections.length)
            globalReasons.push('section set changed');
          const sectionCount = Math.min(beforeSections.length, after.sections.length);
          for (let i = 0; i < sectionCount; i += 1) {
            const before = beforeSections[i];
            const current = after.sections[i];
            if (before.key !== current.key || before.sid !== current.sid
                || before.parentKey !== current.parentKey) {
              globalReasons.push('section identity/order changed at index ' + i);
              continue;
            }
            if (!rectClose(before.rect, current.rect))
              addFailure(before.parentKey, 'section geometry changed: ' + before.sid);
            if (!rectClose(before.parentRect, current.parentRect))
              addFailure(before.parentKey, 'parent geometry changed: ' + before.sid);
            const target = records.find(record => record.sid === before.sid);
            const expectedGap = target ? parseFloat(target.gap) : before.rowGap;
            if (!close(expectedGap, current.rowGap, gapTol))
              addFailure(before.parentKey, 'unexpected row-gap changed: ' + before.sid);
          }

          const beforeMediaByKey = new Map(beforeMedia.map(item => [item.key, item]));
          const afterMediaByKey = new Map(after.media.map(item => [item.key, item]));
          for (const before of beforeMedia) {
            const current = afterMediaByKey.get(before.key);
            if (!current) {
              addFailure(before.parentKey, 'media removed: ' + before.key);
              continue;
            }
            if (before.kind !== current.kind || before.sectionKey !== current.sectionKey
                || before.sid !== current.sid || before.src !== current.src
                || before.currentSrc !== current.currentSrc || before.fit !== current.fit) {
              addFailure(before.parentKey, 'media identity/source changed: ' + before.key);
              continue;
            }
            const dims = [
              ['box width', before.boxW, current.boxW],
              ['box height', before.boxH, current.boxH],
              ['painted width', before.paintedW, current.paintedW],
              ['painted height', before.paintedH, current.paintedH],
            ];
            for (const [label, oldValue, newValue] of dims) {
              if (!close(oldValue, newValue, mediaTol(oldValue, newValue)))
                addFailure(before.parentKey,
                  'media ' + label + ' changed: ' + before.key);
            }
            if (before.nw !== current.nw || before.nh !== current.nh)
              addFailure(before.parentKey,
                'media intrinsic dimensions changed: ' + before.key);
            if (current.kind === 'img' && (current.nw <= 0 || current.nh <= 0))
              addFailure(before.parentKey, 'media failed to load: ' + before.key);
            const beforeSec = beforeSectionByKey.get(before.sectionKey);
            const sec = sectionByKey.get(current.sectionKey);
            const beforeWr = beforeSec && beforeSec.rect && beforeSec.rect.w > 0
              ? before.paintedW / beforeSec.rect.w : 0;
            const beforeHr = beforeSec && beforeSec.rect && beforeSec.rect.h > 0
              ? before.paintedH / beforeSec.rect.h : 0;
            const wr = sec && sec.rect && sec.rect.w > 0
              ? current.paintedW / sec.rect.w : 0;
            const hr = sec && sec.rect && sec.rect.h > 0
              ? current.paintedH / sec.rect.h : 0;
            // Legacy natural pages can already be outside the finishing band.
            // Reject only a new threshold crossing here; the strict geometry
            // comparisons above still reject any expand-created size drift.
            const beforeFill = Math.max(beforeWr, beforeHr);
            const fill = Math.max(wr, hr);
            if (beforeFill + 1e-6 >= figMin && fill + 1e-6 < figMin)
              addFailure(before.parentKey,
                'media fill dropped below floor: ' + before.key);
            if ((beforeWr <= figMax && wr > figMax)
                || (beforeHr <= figMax && hr > figMax))
              addFailure(before.parentKey,
                'media overflowed its section: ' + before.key);
          }
          for (const current of after.media) {
            if (!beforeMediaByKey.has(current.key))
              addFailure(current.parentKey, 'media added: ' + current.key);
          }

          const unattributed = [...scopedReasons.keys()].filter(
            scope => !(recordsByScope.get(scope) || []).length
          );
          if (unattributed.length) {
            globalReasons.push(...unattributed.map(scope =>
              'collateral change outside a candidate scope: ' + scope));
          }
          const failures = [];
          for (const record of records) {
            const reasons = [
              ...(scopedReasons.get(String(record.scope || '')) || []),
              ...globalReasons,
            ];
            if (reasons.length)
              failures.push({sid:String(record.sid || ''), reasons:reasons});
          }
          const snapshotReasons = [
            ...globalReasons,
            ...[...scopedReasons.values()].flat(),
          ];
          return {
            failures:failures,
            snapshotOk:snapshotReasons.length === 0,
            snapshotReasons:snapshotReasons,
          };
        }""",
        {
            "records": records,
            "baseline": baseline,
            "after": after,
            "gapTol": _EXPAND_GAP_TOLERANCE_PX,
            "geomTol": _EXPAND_GEOMETRY_TOLERANCE_PX,
            "mediaRelTol": _EXPAND_MEDIA_REL_TOLERANCE,
            "figMin": _EXPAND_FIG_MIN_RATIO,
            "figMax": _EXPAND_FIG_MAX_RATIO,
        },
    )
    if not isinstance(result, dict):
        return {
            "failures": [
                {"sid": str(record.get("sid", "")),
                 "reasons": ["durable validator returned malformed data"]}
                for record in records
            ],
            "snapshotOk": False,
            "snapshotReasons": ["durable validator returned malformed data"],
        }
    return result


def _settle_loaded_durable_page(
    page,
    *,
    timeout_ms: int,
    playwright_timeout_error,
    label: str,
) -> bool:
    """Settle one loaded durable page and fail closed for expand validation."""
    stable = True
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except playwright_timeout_error:
        _eprint(
            f"[render_preview] WARN: {label} never went idle within "
            f"{timeout_ms} ms; continuing to the bounded settle check."
        )
    except Exception as exc:
        _eprint(
            f"[render_preview] WARN: {label} load-state check failed: "
            f"{ascii_safe(exc)}."
        )
        stable = False
    if not _wait_for_images_decoded(
        page,
        timeout_ms=timeout_ms,
        label=label,
    ):
        stable = False
    try:
        durable_settle = _render.settle_page(
            page,
            mathjax_timeout_ms=timeout_ms,
            settle_ms=1500,
        )
    except Exception as exc:
        _eprint(
            f"[render_preview] WARN: {label} settle failed: "
            f"{ascii_safe(exc)}; provisional expand rules will fail closed."
        )
        return False
    if durable_settle.mathjax_status == "timeout":
        stable = False
        _eprint(
            f"[render_preview] WARN: MathJax typeset timed out after {label} "
            f"({timeout_ms} ms)."
        )
    elif durable_settle.mathjax_status == "error":
        stable = False
        _eprint(
            f"[render_preview] WARN: MathJax error after {label}: "
            f"{ascii_safe(durable_settle.mathjax_error)}"
        )
    if (durable_settle.mathjax_intended and
            durable_settle.tex_without_mathjax):
        stable = False
        _eprint(
            f"[render_preview] WARN: {label} intended to load MathJax but no "
            "<mjx-container> rendered -- PDF will show raw $...$ text."
        )
    return stable


def _reload_and_settle_after_bake(
    page,
    *,
    timeout_ms: int,
    playwright_timeout_error,
    label: str,
) -> bool:
    """Reload renderer-owned CSS and report whether geometry fully settled.

    Rendering remains a soft path for MathJax/network failures, but provisional
    expand rules are optional.  A rule may be kept only when the durable page
    completed the same settle cycle used for final capture; otherwise its
    validator fails closed and removes it.
    """
    navigated = True
    try:
        # ``Page.reload`` can detach a ``file:`` target with net::ERR_FAILED
        # when the renderer has just atomically rewritten that same file.
        # Navigating the existing page to its identical URL preserves the
        # BrowserContext/cache semantics required by this warm check without
        # depending on Chromium's reload fast path.
        page.goto(
            page.url.split("#", 1)[0],
            timeout=timeout_ms,
            wait_until="domcontentloaded",
        )
    except playwright_timeout_error:
        navigated = False
        _eprint(
            f"[render_preview] WARN: {label} did not reach "
            "`domcontentloaded` within "
            f"{timeout_ms} ms; provisional expand rules will fail closed."
        )
    except Exception as exc:
        _eprint(
            f"[render_preview] WARN: {label} failed: {ascii_safe(exc)}; "
            "provisional expand rules will fail closed."
        )
        return False
    return _settle_loaded_durable_page(
        page,
        timeout_ms=timeout_ms,
        playwright_timeout_error=playwright_timeout_error,
        label=label,
    ) and navigated


def _new_print_context_page(browser, viewport: tuple[int, int]):
    """Create an isolated print-emulated page without warming its context."""
    width, height = viewport
    context = browser.new_context(
        viewport={"width": width, "height": height},
    )
    page = context.new_page()
    _render.route_mathjax_local(page)
    _render.route_katex_local(page)
    page.emulate_media(media="print")
    page.set_viewport_size({"width": width, "height": height})
    return context, page


def _open_fresh_durable_page(
    browser,
    viewport: tuple[int, int],
    html_path: Path,
    *,
    timeout_ms: int,
    playwright_timeout_error,
    label: str,
):
    """Navigate once in a brand-new BrowserContext, then fully settle it."""
    context, page = _new_print_context_page(browser, viewport)
    navigated = True
    try:
        page.goto(
            html_path.as_uri(),
            timeout=timeout_ms,
            wait_until="domcontentloaded",
        )
    except playwright_timeout_error:
        navigated = False
        _eprint(
            f"[render_preview] WARN: {label} did not reach "
            "`domcontentloaded` within "
            f"{timeout_ms} ms; provisional expand rules will fail closed."
        )
    except Exception as exc:
        _eprint(
            f"[render_preview] WARN: {label} failed: {ascii_safe(exc)}; "
            "provisional expand rules will fail closed."
        )
        return context, page, False
    stable = _settle_loaded_durable_page(
        page,
        timeout_ms=timeout_ms,
        playwright_timeout_error=playwright_timeout_error,
        label=label,
    )
    return context, page, stable and navigated


def _capture_style_targets_and_freeze(context, page):
    """Disable page scripts and return CDP nodes used for thumbnail scaling.

    The accepted durable page may still contain recurring author fitters.  They
    are harmless at the 1x print viewport but can observe the renderer's later
    thumbnail transform and write screen-space dimensions back into the fixed
    canvas.  Freeze script execution before either artifact is captured; CDP
    DOM commands remain available for the live-only thumbnail transform.
    """
    session = context.new_cdp_session(page)
    session.send("DOM.enable")
    root = session.send("DOM.getDocument", {"depth": 1}).get("root") or {}
    root_id = int(root.get("nodeId") or 0)
    if not root_id:
        raise RuntimeError("CDP did not return a document node")

    def query(*selectors: str) -> int:
        for selector in selectors:
            result = session.send(
                "DOM.querySelector",
                {"nodeId": root_id, "selector": selector},
            )
            node_id = int(result.get("nodeId") or 0)
            if node_id:
                return node_id
        return 0

    def style_of(node_id: int) -> str:
        attributes = session.send(
            "DOM.getAttributes", {"nodeId": node_id}
        ).get("attributes") or []
        for index in range(0, len(attributes) - 1, 2):
            if attributes[index].lower() == "style":
                return str(attributes[index + 1])
        return ""

    poster_id = query('[data-measure-role="poster"]', ".poster", "body")
    body_id = query("body")
    html_id = query("html")
    if not poster_id or not body_id or not html_id:
        raise RuntimeError("CDP could not resolve poster/body/html nodes")
    targets = {
        "poster": (poster_id, style_of(poster_id)),
        "body": (body_id, style_of(body_id)),
        "html": (html_id, style_of(html_id)),
    }
    session.send("Emulation.setScriptExecutionDisabled", {"value": True})
    return session, targets


def _apply_thumbnail_transform_cdp(session, targets: dict, scale: float) -> None:
    """Apply the thumbnail-only style while page JavaScript stays disabled."""
    declarations = (
        (
            "poster",
            "transform-origin:top left!important;"
            f"transform:scale({scale})!important;",
        ),
        ("body", "margin:0!important;"),
        ("html", "margin:0!important;"),
    )
    # A minimal document can legitimately fall back to <body> as its poster.
    # Coalesce by CDP node id so the later body-margin declaration extends the
    # transform instead of rebuilding the same node from its original style and
    # accidentally erasing that transform.
    updates: dict[int, tuple[str, list[str]]] = {}
    for key, declaration in declarations:
        node_id, original = targets[key]
        if node_id not in updates:
            updates[node_id] = (str(original), [])
        updates[node_id][1].append(declaration)
    for node_id, (original, additions) in updates.items():
        prefix = original.strip()
        if prefix and not prefix.endswith(";"):
            prefix += ";"
        session.send(
            "DOM.setAttributeValue",
            {
                "nodeId": node_id,
                "name": "style",
                "value": prefix + "".join(additions),
            },
        )


def _bake_scan_suppress_into_html(html_path: Path) -> bool:
    """Persist a render-time Scan-to-Read suppression into poster.html.

    When the aspect-ratio guard (below) decides the Scan-to-Read section is too
    elongated for its little QR + caption (the lone-QR-in-empty-space defect),
    it sets the section `display:none` on the live page before the expand pass,
    so the column reflows and the expand pass refills the freed space into the
    flex-grow neighbours. To keep the editable poster.html, its `D` overlay, and
    the downstream html2pptx read consistent with the rendered PDF/PNG, write the
    same suppression back as one idempotent <style> block (mirrors
    `_bake_expand_into_html`; `display:none !important` wins regardless of block
    position). This generalises the 3col layout's static scan suppression to any
    layout whose scan column came out as elongated as a 3-column even split.
    """
    block = ('<style id="poster-scan-suppress">\n'
             '  .section[data-section="scan-to-read"] { display: none !important; }\n'
             '</style>')
    txt = html_path.read_text(encoding="utf-8")
    txt, _ = _strip_derived_style_block(txt, "poster-scan-suppress")
    txt = _append_style_at_end(txt, block)
    html_path.write_text(txt, encoding="utf-8")
    return True


def _autopack_header_logos(html_path: Path) -> None:
    """Step 5.9 auto-run: pack the header institution logos so they FILL their
    zone (multi-row, grown to fit) instead of one tiny row. This is a manual
    step in the docs that the agent routinely skips, so run it here right before
    rendering. Soft: any failure just leaves the raw logos and never blocks the
    render (best-effort, like the render-time expand pass). A non-zero fitter
    exit is still surfaced as a warning so the render log distinguishes
    "attempted" from "successfully packed"."""
    import subprocess
    fit = Path(__file__).resolve().parent.parent / "references" / "fit_logos.py"
    if not fit.exists():
        return
    try:
        r = subprocess.run([sys.executable, str(fit), "--poster", str(html_path)],
                           capture_output=True, text=True, timeout=180)
        for line in (r.stdout or "").splitlines():
            if "baked" in line or "fit_logos" in line:
                print(f"[render_preview] {line.strip()}")
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "no diagnostic output").strip()
            _eprint(
                f"[render_preview] WARN: fit_logos auto-pack exited "
                f"with status {r.returncode}: {ascii_safe(detail)}"
            )
    except Exception as e:                       # noqa: BLE001 -- soft, never block render
        _eprint(f"[render_preview] fit_logos auto-pack skipped ({e})")


def _render_staged(
    args: argparse.Namespace,
    html_path: Path,
    pdf_path: Path,
    png_path: Path,
) -> int:
    """Render a disposable HTML working copy into staged artifacts.

    ``html_path`` is deliberately not the user's deliverable.  Every renderer
    mutation (font freeze, logo fitting, stale-style removal, and the fresh
    derived bake) lands on this adjacent working copy.  The caller promotes it
    only after both staged exports have completed successfully.
    """
    # Expand and scan suppression are renderer-derived final state.  Measure a
    # fresh source layout on every invocation; otherwise an older bake feeds
    # back into its own rerender and POSTER_* = 0 cannot disable it.
    _strip_derived_render_styles(html_path)
    _ensure_unscaled_layout_timer_guard(html_path)
    _sync_bundled_fonts(html_path)

    resolved = _canvas.resolve_canvas(
        html_path, args.canvas, label="[render_preview]"
    )
    if resolved is None:
        _eprint(
            "ERROR: could not find `@page { size: <W> <H> }` in HTML. "
            "Add an @page rule (units: in/mm/cm/pt) or pass "
            "`--canvas <W>x<H>in` / `--canvas 'A0 portrait'`. "
            "Refusing to silently fall back."
        )
        return 2
    canvas, viewport = resolved
    w_in, h_in = canvas

    pw = import_playwright()
    if pw is None:
        return 2
    sync_playwright, PWTimeoutError = pw

    # Font fidelity runs before logo packing and the expand baseline.  Compare
    # the same settled page immediately before and after that mutation so a
    # metric-incompatible fallback cannot silently turn a passing figure into
    # a sub-90% stamp before the later validator starts observing geometry.
    _font_gate_error = ""
    _font_gate_warning = ""
    with sync_playwright() as p_:
        _font_browser, _font_ctx, _font_page = _render.open_print_emulated_page(
            p_, viewport
        )
        _prefont_ready = True
        try:
            _font_page.goto(
                html_path.as_uri(), timeout=args.mathjax_timeout_ms,
            )
        except PWTimeoutError:
            _eprint(
                "[render_preview] WARN: pre-font page did not reach `load` "
                f"within {args.mathjax_timeout_ms} ms; continuing with the "
                "bounded settle check."
            )
        except Exception as exc:
            _prefont_ready = False
            _eprint(
                "[render_preview] WARN: pre-font page load failed: "
                f"{ascii_safe(exc)}"
            )
        _prefont_ready = (
            _settle_loaded_durable_page(
                _font_page,
                timeout_ms=args.mathjax_timeout_ms,
                playwright_timeout_error=PWTimeoutError,
                label="pre-font fidelity baseline",
            ) and _prefont_ready
        )
        try:
            _prefont_figures = (
                _capture_card_figure_fills(_font_page)
                if _prefont_ready else []
            )
            _font_changed = freeze_system_font_webfont(html_path)
            if _font_changed:
                _postfont_ready = _reload_and_settle_after_bake(
                    _font_page,
                    timeout_ms=args.mathjax_timeout_ms,
                    playwright_timeout_error=PWTimeoutError,
                    label="post-font fidelity reload",
                )
                if not _prefont_ready or not _postfont_ready:
                    _font_gate_warning = (
                        "font fidelity changed the staged HTML, but its "
                        "pre/post geometry could not be settled reliably; "
                        "continuing on the renderer's existing soft path"
                    )
                else:
                    _postfont_figures = _capture_card_figure_fills(_font_page)
                    _font_failures = _font_freeze_failures(
                        _prefont_figures, _postfont_figures,
                    )
                    if _font_failures:
                        _details = []
                        for _failure in _font_failures:
                            _before = _failure.get("before") or {}
                            _after = _failure.get("after") or {}
                            _label = (
                                _before.get("sid") or _after.get("sid")
                                or _before.get("key") or _after.get("key")
                            )
                            _details.append(
                                f"{_label}: "
                                + ", ".join(_failure.get("reasons") or [])
                            )
                        _font_gate_error = (
                            "font fidelity changed card media outside its "
                            "accepted identity/fill band: "
                            + "; ".join(_details)
                        )
        except Exception as exc:
            _font_gate_error = (
                "font fidelity geometry validation failed: "
                f"{ascii_safe(exc)}"
            )
        finally:
            _font_browser.close()

    if _font_gate_warning:
        _eprint(
            "[render_preview] WARN: " + ascii_safe(_font_gate_warning)
        )
    if _font_gate_error:
        _eprint(
            "[render_preview] ERROR: " + ascii_safe(_font_gate_error)
        )
        return 2

    _autopack_header_logos(html_path)   # Step 5.9, auto-run so it's never skipped

    with sync_playwright() as p_:
        browser, ctx, page = _render.open_print_emulated_page(
            p_, viewport
        )
        # Soft path: a hung CDN (blocked MathJax fetch, unreachable
        # web font) must not hard-crash render. Playwright's default
        # `page.goto` waits for `load` (all subresources), which can
        # block ~30s on a single blocked CDN. settle_page below has
        # its own bounded waits; let it surface MathJax issues as
        # warnings, not tracebacks.
        try:
            page.goto(html_path.as_uri(), timeout=args.mathjax_timeout_ms)
        except PWTimeoutError:
            _eprint(
                f"[render_preview] WARN: page.goto did not reach `load` "
                f"within {args.mathjax_timeout_ms} ms; continuing with "
                f"whatever has loaded (a CDN or external resource is "
                f"likely blocked)."
            )
        try:
            page.wait_for_load_state(
                "networkidle", timeout=args.mathjax_timeout_ms,
            )
        except PWTimeoutError:
            _eprint(
                f"[render_preview] WARN: network never went idle within "
                f"{args.mathjax_timeout_ms} ms; continuing with whatever "
                f"loaded (likely a slow/blocked external resource)."
            )

        _initial_images_ready = _wait_for_images_decoded(
            page,
            timeout_ms=args.mathjax_timeout_ms,
            label="initial load",
        )

        settle = _render.settle_page(
            page,
            mathjax_timeout_ms=args.mathjax_timeout_ms,
            settle_ms=1500,
        )
        # Render is soft path: warn but continue, even on MathJax
        # problems — the user can SEE raw $...$ on the resulting PDF.
        if settle.mathjax_status == "timeout":
            _eprint(
                f"[render_preview] WARN: MathJax typeset timed out "
                f"after {args.mathjax_timeout_ms} ms."
            )
        elif settle.mathjax_status == "error":
            _eprint(
                f"[render_preview] WARN: MathJax error: "
                f"{ascii_safe(settle.mathjax_error)}"
            )
        if settle.mathjax_intended and settle.tex_without_mathjax:
            _eprint(
                "[render_preview] WARN: page intended to load MathJax "
                "but no <mjx-container> rendered -- MathJax may have "
                "failed to load. PDF will show raw $...$ text."
            )

        # Scan-to-Read aspect guard (runs BEFORE the expand pass). A scan section
        # that came out wide and flat -- its own width many times its own height
        # -- holds just a small QR (and maybe a heading) marooned in horizontal
        # empty space. Poster #8 is the worst case: a grid column that blew out
        # to ~2x width (a wide unbreakable child forced the track wider) left the
        # directory scan variant a 730x156 px band with one 110 px QR filling
        # ~15% of the width. The metric is the section's OWN aspect ratio
        # (width / height) -- NOT relative to the canvas -- mirroring the 3col
        # layout's static suppression ("far more horizontal room than its little
        # content fills"). When width/height exceeds the threshold, hide the
        # section now so the column reflows and the expand pass below refills the
        # freed height into the flex-grow neighbours. Defensive: a scan section
        # that is the ONLY section in its column is never removed (would blank a
        # whole column). Tunable via POSTER_SCAN_SUPPRESS_WH (default 3.8 -- in
        # the v2 benchmark the offending #8 reads ~4.7 while well-filled scan
        # sections sit at 1.2-3.3; 0 disables).
        _scan_suppressed = False
        try:
            _scan_wh = float(os.environ.get("POSTER_SCAN_SUPPRESS_WH", "3.8"))
        except Exception:
            _scan_wh = 3.8
        if _scan_wh > 0:
            try:
                _sv = page.evaluate(
                    """(T) => {
                      const sec = document.querySelector('.section[data-section="scan-to-read"]');
                      if (!sec) return {act:false, why:'none'};
                      const r = sec.getBoundingClientRect();
                      if (r.width < 4 || r.height < 4) return {act:false, why:'hidden'};
                      const col = sec.closest('.col');
                      if (col && col.querySelectorAll('.section').length <= 1)
                        return {act:false, why:'alone'};        // never blank a whole column
                      const wh = r.width / r.height;            // the section's OWN aspect (width / height)
                      if (wh >= T) { sec.style.display = 'none';
                        return {act:true, why:'aspect', wh: wh}; }
                      return {act:false, why:'within', wh: wh};
                    }""",
                    _scan_wh,
                )
                if isinstance(_sv, dict) and _sv.get("wh") is not None:
                    _eprint(
                        "[render_preview] Scan-to-Read aspect (w/h) "
                        f"{round(float(_sv['wh']), 2)} "
                        f"(suppress at >= {_scan_wh}) -> "
                        f"{'SUPPRESSED' if _sv.get('act') else 'kept'}."
                    )
                if isinstance(_sv, dict) and _sv.get("act"):
                    _scan_suppressed = True
                    page.wait_for_timeout(120)          # let the column reflow before the expand pass measures it
            except Exception:
                pass

        # Expand deliverable: stretch each under-filled card's inner content to
        # ~POSTER_EXPAND_THRESHOLD of the card (default 0.98) by growing the
        # row-gaps BETWEEN its rows -- COLUMN bottoms stay aligned. Figure/image
        # cards participate too, but only provisionally: after the rule is baked
        # and the page's on-load figure fitter runs again, a durable validation
        # removes any rule that changes figure dimensions or drops the strict
        # >=90% figure-fill gate. Two immediate guardrails remain: (a) the slack
        # cap -- never push content past the bottom padding; (b) the
        # PARENT-height revert -- if
        # growing the gap changes the card's CONTAINER (column/grid) height, undo
        # it. (b) is deliberately on the parent, not the card: a flex:1 grow card
        # absorbs the fill inside its column (column height unchanged -> bottoms
        # stay put -> we DO fill that trailing space, which is the whole point),
        # whereas a grid/content card whose fill would grow its container (pushing
        # the fixed-canvas layout) is reverted. This is a render-time "expand"
        # pass separate from the staged-fill loop's FILL gate (POSTER_FULL_THRESHOLD,
        # default 0.90): the loop still measures the natural top-aligned layout via
        # check_poster.py so the fill gate stays correct; this only makes the final
        # deliverable read fuller. Configurable: POSTER_EXPAND_THRESHOLD (0 disables).
        try:
            _expand_t = float(os.environ.get("POSTER_EXPAND_THRESHOLD", "0.98"))
        except Exception:
            _expand_t = 0.98
        _expand_records = []
        _expand_baseline = {}
        _expand_failed = False
        _baked = []
        if _expand_t > 0 and _initial_images_ready:
            try:
                # This is the one natural-layout capture. It completes before
                # the proposal evaluator is allowed to mutate any row-gap.
                _expand_baseline = _capture_expand_snapshot(page)
                _expand_result = page.evaluate(
                    """({T, baseline}) => {
                      // Phase 1 is strictly read-only.  Every proposal and the
                      // target gap is collected before a single row-gap changes,
                      // so DOM order cannot make later proposals depend on an
                      // earlier candidate.
                      const sectionNodes = Array.from(
                        document.querySelectorAll('.section[data-section]')
                      );
                      const sidCounts = new Map();
                      sectionNodes.forEach(sec => {
                        const sid = sec.getAttribute('data-section') || '';
                        sidCounts.set(sid, (sidCounts.get(sid) || 0) + 1);
                      });
                      const proposals = [];
                      sectionNodes.forEach((sec, sectionIndex) => {
                        const sid = sec.getAttribute('data-section') || '';
                        // One CSS selector cannot persist two different live
                        // element decisions. Fail closed on malformed duplicate
                        // ids rather than baking an ambiguous rule.
                        if (!sid || sidCounts.get(sid) !== 1) return;
                        const kids = Array.from(sec.children).filter(k => k.classList
                          && !k.classList.contains('listen-btn')
                          && !k.classList.contains('dbg-badge')
                          && !k.classList.contains('dbg-bbox'));
                        if (kids.length < 2) return;
                        const oldInlineGap = sec.style.getPropertyValue('row-gap');
                        const oldInlinePriority = sec.style.getPropertyPriority('row-gap');
                        const authoredInlineImportant = [
                          'row-gap', 'gap', 'grid-row-gap'
                        ].some(prop => sec.style.getPropertyPriority(prop) === 'important');
                        if (authoredInlineImportant) return;
                        const sb = sec.getBoundingClientRect();
                        const bot = Math.max.apply(null,
                          kids.map(k => k.getBoundingClientRect().bottom));
                        const cur = (bot - sb.top) / sb.height;
                        if (cur >= T) return;
                        const cs = getComputedStyle(sec);
                        const padBot = parseFloat(cs.paddingBottom) || 0;
                        const scale = sec.offsetHeight ? sb.height / sec.offsetHeight : 1;
                        const slack = (sb.bottom - padBot * scale) - bot;
                        if (slack <= 1) return;
                        const add = Math.min((T - cur) * sb.height, slack);
                        const per = add / (kids.length - 1);
                        const curGap = parseFloat(cs.rowGap) || 0;
                        proposals.push({
                          sec:sec, sid:sid,
                          scope:baseline.sections[sectionIndex].parentKey,
                          gap:(curGap + per) + 'px',
                          parent:sec.parentElement,
                          oldInlineGap:oldInlineGap,
                          oldInlinePriority:oldInlinePriority,
                        });
                      });

                      // Phase 2 mutates only after every natural baseline and
                      // target gap is frozen.  The immediate parent guard stays
                      // local to each mutation; the durable global validator
                      // below catches sibling and cross-card collateral changes.
                      const records = [];
                      if (proposals.length)
                        document.documentElement.dataset.posterExpandEphemeral = '1';
                      for (const proposal of proposals) {
                        const par = proposal.parent;
                        const liveParentH = par ? par.getBoundingClientRect().height : 0;
                        proposal.sec.style.setProperty(
                          'row-gap', proposal.gap, 'important'
                        );
                        if (par && Math.abs(
                            par.getBoundingClientRect().height - liveParentH
                        ) > 1) {
                          if (proposal.oldInlineGap)
                            proposal.sec.style.setProperty(
                              'row-gap', proposal.oldInlineGap,
                              proposal.oldInlinePriority,
                            );
                          else
                            proposal.sec.style.removeProperty('row-gap');
                          continue;
                        }
                        records.push({
                          sid:proposal.sid,
                          scope:proposal.scope,
                          gap:getComputedStyle(proposal.sec).rowGap || '0px',
                        });
                      }
                      if (!records.length)
                        delete document.documentElement.dataset.posterExpandEphemeral;
                      return records;
                    }""",
                    {"T": _expand_t, "baseline": _expand_baseline},
                )
                page.wait_for_timeout(150)
                # Persist the expand into the deliverable html so poster.html,
                # its `D` overlay, the PDF/PNG, and the downstream html2pptx read
                # all show the same expanded layout (not the pre-expand one).
                if not isinstance(_expand_result, list):
                    raise RuntimeError("expand pass returned malformed data")
                _expand_records = list(_expand_result)
                if _expand_records and not _expand_baseline:
                    raise RuntimeError("expand pass omitted its natural baseline")
                _baked = [
                    [record["sid"], record["gap"]]
                    for record in _expand_records
                ]
            except Exception as exc:
                _expand_failed = True
                _expand_records = []
                _expand_baseline = {}
                _baked = []
                _eprint(
                    "[render_preview] WARN: provisional expand failed: "
                    f"{ascii_safe(exc)}; reloading the natural persisted HTML."
                )
        elif _expand_t > 0:
            _eprint(
                "[render_preview] WARN: render-time expand skipped because "
                "the natural image baseline was not fully decoded."
            )

        # Persist renderer-derived state in cascade order: scan first, expand
        # LAST.  The latter must follow autofit and every other author style so
        # the computed gap captured above is still the winner after reload.
        _html_mutated = False
        if _scan_suppressed:
            _html_mutated = _bake_scan_suppress_into_html(html_path)
        if _baked:
            _html_mutated = _bake_expand_into_html(html_path, _baked) or _html_mutated

        # Render artifacts only from the durable HTML.  Provisional rules are
        # monotonically pruned by original parent scope until the reloaded page
        # matches the complete natural geometry/media snapshot.  A clean state
        # must survive one extra identical-rule reload before capture; this
        # catches load-count-dependent fitters and late cascade overrides.
        _survivors = list(_expand_records)
        _had_provisional_expand = bool(_survivors)
        _durable_ready = True
        if _html_mutated or _expand_failed:
            _durable_ready = _reload_and_settle_after_bake(
                page,
                timeout_ms=args.mathjax_timeout_ms,
                playwright_timeout_error=PWTimeoutError,
                label="post-bake reload",
            )
        if _expand_failed and not _durable_ready:
            _eprint(
                "[render_preview] ERROR: provisional expand failed and the "
                "natural persisted HTML could not be reloaded reliably; "
                "refusing to capture a possibly live-only DOM."
            )
            browser.close()
            return 2

        if _had_provisional_expand:
            _clean_validations = 0
            while True:
                if not _durable_ready and _survivors:
                    _validation = {
                        "failures": [
                            {
                                "sid": str(record.get("sid", "")),
                                "reasons": ["durable settle incomplete"],
                            }
                            for record in _survivors
                        ],
                        "snapshotOk": False,
                        "snapshotReasons": ["durable settle incomplete"],
                    }
                else:
                    try:
                        _validation = _validate_durable_expand(
                            page, _survivors, _expand_baseline,
                        )
                    except Exception as exc:
                        _reason = f"durable validation failed: {ascii_safe(exc)}"
                        _validation = {
                            "failures": [
                                {
                                    "sid": str(record.get("sid", "")),
                                    "reasons": [_reason],
                                }
                                for record in _survivors
                            ],
                            "snapshotOk": False,
                            "snapshotReasons": [_reason],
                        }

                _failures = list(_validation.get("failures") or [])
                _snapshot_ok = bool(_validation.get("snapshotOk"))
                if not _failures and _snapshot_ok:
                    if _clean_validations >= 1:
                        break
                    _clean_validations += 1
                    _durable_ready = _reload_and_settle_after_bake(
                        page,
                        timeout_ms=args.mathjax_timeout_ms,
                        playwright_timeout_error=PWTimeoutError,
                        label="post-expand confirmation reload",
                    )
                    continue

                if _survivors:
                    _failed_sids = {
                        str(failure.get("sid", ""))
                        for failure in _failures
                        if str(failure.get("sid", ""))
                    }
                    _known_sids = {
                        str(record.get("sid", "")) for record in _survivors
                    }
                    # Unknown/unattributed validation output cannot safely pick
                    # one rule.  Remove all remaining provisional rules.
                    if (not _failed_sids
                            or not _failed_sids.issubset(_known_sids)):
                        _failed_sids = set(_known_sids)
                    _next_survivors = [
                        record for record in _survivors
                        if str(record.get("sid", "")) not in _failed_sids
                    ]
                    if len(_next_survivors) >= len(_survivors):
                        _failed_sids = set(_known_sids)
                        _next_survivors = []
                    for failure in _failures:
                        sid = str(failure.get("sid", ""))
                        if sid not in _failed_sids:
                            continue
                        reasons = "; ".join(
                            str(reason)
                            for reason in (failure.get("reasons") or [])
                        ) or "durable validation failed"
                        _eprint(
                            f"[render_preview] expand rollback {sid!r}: "
                            f"{ascii_safe(reasons)}"
                        )
                    if not _failures:
                        reasons = "; ".join(
                            str(reason) for reason in
                            (_validation.get("snapshotReasons") or [])
                        ) or "unattributed durable snapshot mismatch"
                        _eprint(
                            "[render_preview] expand rollback (all rules): "
                            f"{ascii_safe(reasons)}"
                        )
                    _survivors = _next_survivors
                    _bake_expand_into_html(
                        html_path,
                        [
                            [record["sid"], record["gap"]]
                            for record in _survivors
                        ],
                    )
                    _durable_ready = _reload_and_settle_after_bake(
                        page,
                        timeout_ms=args.mathjax_timeout_ms,
                        playwright_timeout_error=PWTimeoutError,
                        label="post-expand rollback reload",
                    )
                    _clean_validations = 0
                    continue

                # All optional rules are already gone.  If the natural page
                # still differs from the read-only baseline, capturing it would
                # expose a live-only/stale document.  Abort the staged render so
                # the user's previous HTML/PDF/PNG transaction stays untouched.
                _reasons = "; ".join(
                    str(reason) for reason in
                    (_validation.get("snapshotReasons") or [])
                ) or "natural durable snapshot mismatch"
                _eprint(
                    "[render_preview] ERROR: natural HTML did not restore "
                    f"after expand rollback: {ascii_safe(_reasons)}"
                )
                browser.close()
                return 2

        # Same-context reloads reuse storage, decoded images, and other browser
        # caches.  They are useful for cheaply pruning ordinary cascade/refit
        # failures above, but they do not prove that the baked HTML survives a
        # real standalone open.  Confirm each remaining fixed point in a brand-
        # new BrowserContext.  A rejected candidate is closed; after its parent
        # scope is removed from the bake, the next attempt gets another new
        # context rather than a warmed retry.  The accepted cold page becomes
        # the capture page, so validation and artifacts share one DOM.
        _needs_fresh_confirmation = bool(
            _html_mutated or _expand_failed or _had_provisional_expand
        )
        if _needs_fresh_confirmation:
            _fresh_attempt = 0
            while True:
                _fresh_attempt += 1
                _fresh_ctx, _fresh_page, _fresh_ready = (
                    _open_fresh_durable_page(
                        browser,
                        viewport,
                        html_path,
                        timeout_ms=args.mathjax_timeout_ms,
                        playwright_timeout_error=PWTimeoutError,
                        label=(
                            "fresh-context expand confirmation "
                            f"#{_fresh_attempt}"
                        ),
                    )
                )
                if not _fresh_ready:
                    _fresh_validation = {
                        "failures": [
                            {
                                "sid": str(record.get("sid", "")),
                                "reasons": ["fresh-context settle incomplete"],
                            }
                            for record in _survivors
                        ],
                        "snapshotOk": False,
                        "snapshotReasons": [
                            "fresh-context settle incomplete"
                        ],
                    }
                elif _expand_baseline:
                    try:
                        _fresh_validation = _validate_durable_expand(
                            _fresh_page,
                            _survivors,
                            _expand_baseline,
                        )
                    except Exception as exc:
                        _reason = (
                            "fresh-context durable validation failed: "
                            f"{ascii_safe(exc)}"
                        )
                        _fresh_validation = {
                            "failures": [
                                {
                                    "sid": str(record.get("sid", "")),
                                    "reasons": [_reason],
                                }
                                for record in _survivors
                            ],
                            "snapshotOk": False,
                            "snapshotReasons": [_reason],
                        }
                else:
                    # Scan suppression and a failed/no-op provisional pass can
                    # mutate the staged HTML without producing an expand
                    # baseline.  A fully settled one-navigation cold page is
                    # still required, but there is no optional geometry record
                    # to compare or prune.
                    _fresh_validation = {
                        "failures": [],
                        "snapshotOk": True,
                        "snapshotReasons": [],
                    }

                _fresh_failures = list(
                    _fresh_validation.get("failures") or []
                )
                _fresh_snapshot_ok = bool(
                    _fresh_validation.get("snapshotOk")
                )
                if (not _fresh_failures and _fresh_snapshot_ok
                        and _fresh_ready):
                    _old_ctx = ctx
                    ctx, page = _fresh_ctx, _fresh_page
                    try:
                        _old_ctx.close()
                    except Exception:
                        pass
                    break

                try:
                    _fresh_ctx.close()
                except Exception:
                    pass

                if _survivors:
                    _failed_sids = {
                        str(failure.get("sid", ""))
                        for failure in _fresh_failures
                        if str(failure.get("sid", ""))
                    }
                    _known_sids = {
                        str(record.get("sid", "")) for record in _survivors
                    }
                    if (not _failed_sids
                            or not _failed_sids.issubset(_known_sids)):
                        _failed_sids = set(_known_sids)
                    _next_survivors = [
                        record for record in _survivors
                        if str(record.get("sid", "")) not in _failed_sids
                    ]
                    if len(_next_survivors) >= len(_survivors):
                        _failed_sids = set(_known_sids)
                        _next_survivors = []
                    for failure in _fresh_failures:
                        sid = str(failure.get("sid", ""))
                        if sid not in _failed_sids:
                            continue
                        reasons = "; ".join(
                            str(reason)
                            for reason in (failure.get("reasons") or [])
                        ) or "fresh-context durable validation failed"
                        _eprint(
                            f"[render_preview] expand rollback {sid!r}: "
                            f"{ascii_safe(reasons)}"
                        )
                    if not _fresh_failures:
                        reasons = "; ".join(
                            str(reason) for reason in
                            (_fresh_validation.get("snapshotReasons") or [])
                        ) or "unattributed fresh-context snapshot mismatch"
                        _eprint(
                            "[render_preview] expand rollback (all rules): "
                            f"{ascii_safe(reasons)}"
                        )
                    _survivors = _next_survivors
                    _bake_expand_into_html(
                        html_path,
                        [
                            [record["sid"], record["gap"]]
                            for record in _survivors
                        ],
                    )
                    continue

                _reasons = "; ".join(
                    str(reason) for reason in
                    (_fresh_validation.get("snapshotReasons") or [])
                ) or "natural fresh-context snapshot mismatch"
                _eprint(
                    "[render_preview] ERROR: natural HTML did not survive "
                    "a fresh-context confirmation after expand rollback: "
                    f"{ascii_safe(_reasons)}"
                )
                browser.close()
                return 2

        # If the provisional script itself raised after partially touching the
        # live DOM, the reload above is the rollback.  Verify a live-only marker
        # did not survive and that capture is still on the staged persisted URL.
        try:
            _final_page_state = page.evaluate(
                """() => ({
                  ephemeral:document.documentElement.dataset.posterExpandEphemeral || '',
                  hasExpand:!!document.getElementById('poster-expand-baked'),
                })"""
            )
        except Exception as exc:
            _eprint(
                "[render_preview] ERROR: could not verify final persisted "
                f"HTML before capture: {ascii_safe(exc)}"
            )
            browser.close()
            return 2
        _expected_expand = bool(_survivors)
        if (page.url.split("#", 1)[0] != html_path.as_uri()
                or bool(_final_page_state.get("ephemeral"))
                or bool(_final_page_state.get("hasExpand")) != _expected_expand):
            _eprint(
                "[render_preview] ERROR: final browser page is not the final "
                "persisted expand state; refusing to capture stale HTML."
            )
            browser.close()
            return 2

        try:
            _capture_cdp, _capture_style_targets = (
                _capture_style_targets_and_freeze(ctx, page)
            )
        except Exception as exc:
            _eprint(
                "[render_preview] ERROR: could not freeze the accepted "
                "durable page before capture: "
                f"{ascii_safe(exc)}"
            )
            browser.close()
            return 2

        # ---- PDF: exact poster size, print-emulated ----
        pdf_scale = _pdf_content_scale(canvas, viewport)
        if pdf_scale < 0.999999:
            _eprint(
                "[render_preview] fractional canvas rounding: PDF content "
                f"scale={pdf_scale:.6f} to prevent a blank trailing page"
            )
        page.pdf(
            path=str(pdf_path),
            width=f"{w_in}in",
            height=f"{h_in}in",
            scale=pdf_scale,
            print_background=True,
            margin={"top": "0", "bottom": "0",
                    "left": "0", "right": "0"},
        )

        # ---- PNG: scaled thumbnail of `.poster` (or document body) ----
        # IMPORTANT: do NOT resize the viewport for the screenshot. The
        # poster CSS uses `width: min(100vw, calc(100vh * 5 / 3))`, so a
        # viewport change retriggers reflow and the scaled poster ends up
        # occupying only a fraction of the captured area. Instead, keep
        # the print viewport, apply the scale transform, and `clip` the
        # screenshot to the scaled region.
        s = args.thumb_scale
        _apply_thumbnail_transform_cdp(
            _capture_cdp,
            _capture_style_targets,
            s,
        )
        thumb_w = int(round(w_in * 96 * s))
        thumb_h = int(round(h_in * 96 * s))
        page.screenshot(
            path=str(png_path),
            full_page=False,
            clip={"x": 0, "y": 0,
                  "width": thumb_w, "height": thumb_h},
        )

        browser.close()

    return 0


def _temporary_path(
    parent: Path,
    *,
    prefix: str,
    suffix: str,
    mode: int | None = None,
) -> Path:
    """Reserve a unique adjacent path suitable for Chromium or an HTML copy.

    ``mkstemp`` deliberately creates private ``0600`` files.  That is right for
    rollback backups, but HTML/PDF/PNG deliverables are public bundle assets.
    Their intended mode must be applied to the *staged* inode before promotion
    so permissions are committed atomically with the file contents.
    """
    fd, raw = tempfile.mkstemp(dir=parent, prefix=prefix, suffix=suffix)
    os.close(fd)
    path = Path(raw)
    try:
        if mode is not None:
            path.chmod(mode)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _promote_render_transaction(pairs: list[tuple[Path, Path]]) -> None:
    """Promote all staged files, rolling every target back on any failure.

    ``os.replace`` is atomic for each same-directory file.  A small backup
    journal extends that property across the HTML/PDF/PNG set: if any replace
    fails, already-promoted targets are removed and all prior files are put
    back byte-for-byte before the exception escapes.
    """
    targets = [target for _staged, target in pairs]
    if len(set(targets)) != len(targets):
        raise ValueError("HTML, PDF, and PNG output paths must be distinct")

    for staged, target in pairs:
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise RuntimeError(f"staged render output is missing or empty: {staged}")
        if target.exists() and not target.is_file():
            raise IsADirectoryError(f"render output target is not a file: {target}")

    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    try:
        for staged, target in pairs:
            if target.exists():
                backup = _temporary_path(
                    target.parent,
                    prefix=f".{target.name}.",
                    suffix=".render-backup",
                )
                backup.unlink()
                os.replace(target, backup)
                backups[target] = backup
            os.replace(staged, target)
            promoted.append(target)
    except BaseException:
        rollback_errors: list[str] = []
        for target in reversed(promoted):
            try:
                if target.exists():
                    target.unlink()
            except OSError as exc:
                rollback_errors.append(f"remove {target}: {exc}")
        for target, backup in backups.items():
            try:
                if backup.exists():
                    os.replace(backup, target)
            except OSError as exc:
                rollback_errors.append(f"restore {target}: {exc}")
        if rollback_errors:
            _eprint(
                "[render_preview] ERROR: render promotion rollback was "
                "incomplete: " + "; ".join(rollback_errors)
            )
        raise
    else:
        for backup in backups.values():
            try:
                if backup.exists():
                    backup.unlink()
            except OSError as exc:
                # The committed deliverables are complete; a stale private
                # backup is cleanup debt, not a failed render transaction.
                _eprint(
                    f"[render_preview] WARN: could not remove render backup "
                    f"{ascii_safe(backup)}: {ascii_safe(exc)}"
                )


def main() -> int:
    args = build_parser().parse_args()

    deliverable_html = Path(args.html).resolve()
    if not deliverable_html.is_file():
        _eprint(f"ERROR: HTML not found: {ascii_safe(deliverable_html)}")
        return 2

    pdf_path = (
        Path(args.pdf).resolve() if args.pdf
        else deliverable_html.with_name(deliverable_html.stem + ".pdf")
    )
    png_path = (
        Path(args.png).resolve() if args.png
        else deliverable_html.with_name(deliverable_html.stem + ".png")
    )
    targets = (deliverable_html, pdf_path, png_path)
    if len(set(targets)) != len(targets):
        _eprint("ERROR: HTML, PDF, and PNG output paths must be distinct")
        return 2
    if not pdf_path.parent.is_dir() or not png_path.parent.is_dir():
        _eprint("ERROR: PDF and PNG output directories must already exist")
        return 2

    staged_html = _temporary_path(
        deliverable_html.parent,
        prefix=f".{deliverable_html.name}.",
        suffix=".render.html",
    )
    staged_pdf = _temporary_path(
        pdf_path.parent,
        prefix=f".{pdf_path.name}.",
        suffix=".render.pdf",
        mode=_PUBLIC_ARTIFACT_MODE,
    )
    staged_png = _temporary_path(
        png_path.parent,
        prefix=f".{png_path.name}.",
        suffix=".render.png",
        mode=_PUBLIC_ARTIFACT_MODE,
    )
    staged_paths = (staged_html, staged_pdf, staged_png)
    font_journal: _FontAssetJournal | None = None
    try:
        shutil.copy2(deliverable_html, staged_html)
        # copy2 preserves the source mode (including legacy 0700/0755 poster
        # HTML). Normalize the staged inode before rendering so the final
        # three-file promotion commits content and public-readability together.
        staged_html.chmod(_PUBLIC_ARTIFACT_MODE)
        font_journal = _FontAssetJournal(deliverable_html)
        result = _render_staged(args, staged_html, staged_pdf, staged_png)
        if result != 0:
            return result

        _promote_render_transaction(
            [
                (staged_pdf, pdf_path),
                (staged_png, png_path),
                # HTML is the commit marker: never expose the freshly baked
                # source until both binary artifacts are ready to accompany it.
                (staged_html, deliverable_html),
            ]
        )
        font_journal.commit()
    finally:
        if font_journal is not None and not font_journal.closed:
            active_error = sys.exc_info()[0]
            try:
                font_journal.rollback()
            except Exception as exc:
                _eprint(
                    "[render_preview] ERROR: "
                    f"{ascii_safe(exc)}"
                )
                # Do not hide an exception already in flight, but a failed
                # rollback after a normal non-zero return is itself fatal.
                if active_error is None:
                    raise
        for staged in staged_paths:
            try:
                if staged.exists():
                    staged.unlink()
            except OSError:
                pass

    print(
        f"[render_preview] PDF -> {ascii_safe(pdf_path)}  "
        f"({pdf_path.stat().st_size / 1024:.1f} KB)"
    )
    print(
        f"[render_preview] PNG -> {ascii_safe(png_path)}  "
        f"({png_path.stat().st_size / 1024:.1f} KB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
