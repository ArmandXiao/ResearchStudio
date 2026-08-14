"""Portable browser-font preparation for generated poster bundles.

The poster composer intentionally exposes familiar Mac/Windows PowerPoint
family names.  Those proprietary fonts are not guaranteed to be installed on
the Linux renderer or on an HTML viewer's machine, so the same CSS can resolve
to different glyph metrics and wrap differently.  This module freezes browser
rendering to a licensed DejaVu face while retaining the requested CSS family
name for the native PPTX handoff.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .cli_common import eprint


_FIDELITY_VERSION = "4"
_LICENSE_NAME = "RS-DejaVu-LICENSE.txt"

_PORTABLE_FAMILIES = {
    "calibri": ("Calibri", "DejaVu Sans"),
    "aptos": ("Aptos", "DejaVu Sans"),
    "arial": ("Arial", "DejaVu Sans"),
    "verdana": ("Verdana", "DejaVu Sans"),
    "trebuchet ms": ("Trebuchet MS", "DejaVu Sans"),
    "cambria": ("Cambria", "DejaVu Serif"),
    "times new roman": ("Times New Roman", "DejaVu Serif"),
    "georgia": ("Georgia", "DejaVu Serif"),
}

_FIDELITY_PATTERN = re.compile(
    r'<style\s+id=["\']poster-font-fidelity["\'].*?</style>'
    r'(?:\s*<script\s+id=["\']poster-font-fidelity-refit["\']'
    r'.*?</script>)?',
    flags=re.IGNORECASE | re.DOTALL,
)


def managed_font_asset_names() -> frozenset[str]:
    """Return every filename this module may create under ``assets/fonts``.

    ``render_poster`` uses this closed set for its rollback journal.  Keep the
    names source-derived so adding another portable family automatically puts
    its files inside the same render transaction.
    """
    names = {_LICENSE_NAME}
    for _requested_family, source_family in _PORTABLE_FAMILIES.values():
        source_slug = source_family.replace(" ", "")
        names.add(f"RS-{source_slug}-Regular.ttf")
        names.add(f"RS-{source_slug}-Bold.ttf")
    return frozenset(names)


def _copy_public_asset_atomic(source: Path, target: Path) -> None:
    """Install one public font asset without exposing a partial overwrite."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".font-next",
    )
    temporary = Path(raw)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_dejavu(family_name: str, style: str) -> Path | None:
    """Resolve only the explicitly requested DejaVu family via fontconfig."""
    try:
        match = subprocess.run(
            [
                "fc-match", "-f", "%{family}\t%{style}\t%{file}\n",
                f"{family_name}:style={style}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if match.returncode != 0 or not match.stdout.strip():
        return None
    fields = match.stdout.strip().split("\t", 2)
    if len(fields) != 3:
        return None
    family, _actual_style, filename = fields
    # fc-match always returns *some* fallback. Redistribute only the requested
    # DejaVu family covered by the license copied below.
    if family.split(",", 1)[0].strip().casefold() != family_name.casefold():
        return None
    path = Path(filename)
    return path if path.is_file() else None


def freeze_system_font_webfont(html_path: Path) -> bool:
    """Freeze selectable OS-font stacks to redistributable browser faces.

    Serif selections map to DejaVu Serif and sans-serif selections map to
    DejaVu Sans.  The exact open-licensed faces and their license notice are
    copied into the deliverable, then exposed through ``@font-face`` rules
    carrying the *requested* family name.  HTML/PDF/PNG therefore use one
    custom face on every client, while html2pptx continues to emit the user's
    requested native family into PowerPoint.

    If the HTML already supplies an independent custom ``@font-face`` for the
    selected family, it is treated as intentionally licensed and left alone.
    Any obsolete fidelity block is removed when freezing no longer applies.
    Returns ``True`` whenever the HTML file was changed.
    """
    html_path = Path(html_path)
    text = html_path.read_text(encoding="utf-8", errors="ignore")

    # Exclude our own prior block while looking for an independently supplied
    # (for example, licensed Georgia) author face.
    text_without_fidelity = _FIDELITY_PATTERN.sub("", text)
    declaration = re.search(
        r"--font-latin\s*:\s*([^;]+);", text, flags=re.IGNORECASE,
    )
    if not declaration:
        if text_without_fidelity == text:
            return False
        html_path.write_text(text_without_fidelity, encoding="utf-8")
        eprint(
            "[paper2poster] removed stale portable-font fidelity block: "
            "the poster no longer declares --font-latin."
        )
        return True
    first_family = declaration.group(1).split(",", 1)[0].strip().strip("\"'")
    selected = _PORTABLE_FAMILIES.get(first_family.casefold())
    if selected is None:
        if text_without_fidelity == text:
            return False
        html_path.write_text(text_without_fidelity, encoding="utf-8")
        eprint(
            "[paper2poster] removed stale portable-font fidelity block: "
            f"{first_family} uses its own browser font configuration."
        )
        return True
    requested_family, source_family = selected
    # Keep the source family in the asset URL. A generic PosterFont.ttf URL
    # can remain cached as Serif after a poster switches Georgia -> Arial (or
    # vice versa), even though the file on the server has been overwritten.
    source_slug = source_family.replace(" ", "")
    regular_name = f"RS-{source_slug}-Regular.ttf"
    bold_name = f"RS-{source_slug}-Bold.ttf"

    for face in re.findall(
        r"@font-face\s*\{.*?\}",
        text_without_fidelity,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        family = re.search(
            r"font-family\s*:\s*([^;}]+)", face, flags=re.IGNORECASE,
        )
        if (
            family
            and family.group(1).strip().strip("\"'").casefold()
            == requested_family.casefold()
        ):
            if text_without_fidelity == text:
                return False
            html_path.write_text(text_without_fidelity, encoding="utf-8")
            eprint(
                "[paper2poster] removed stale portable-font fidelity block: "
                f"the poster supplies its own {requested_family} @font-face."
            )
            return True

    out_fonts = html_path.parent / "assets" / "fonts"
    prior = _FIDELITY_PATTERN.search(text)
    if prior:
        requested_attr = re.search(
            r'data-requested-family=["\']([^"\']+)["\']',
            prior.group(0),
            flags=re.IGNORECASE,
        )
        source_attr = re.search(
            r'data-source-family=["\']([^"\']+)["\']',
            prior.group(0),
            flags=re.IGNORECASE,
        )
        version_attr = re.search(
            r'data-fidelity-version=["\']([^"\']+)["\']',
            prior.group(0),
            flags=re.IGNORECASE,
        )
        refit_tag = re.search(
            r'<script\s+id=["\']poster-font-fidelity-refit["\'][^>]*>',
            prior.group(0),
            flags=re.IGNORECASE,
        )
        refit_version_attr = (
            re.search(
                r'data-fidelity-version=["\']([^"\']+)["\']',
                refit_tag.group(0),
                flags=re.IGNORECASE,
            )
            if refit_tag
            else None
        )
        assets_complete = all(
            (out_fonts / name).is_file()
            and (out_fonts / name).stat().st_size
            > (100_000 if name.endswith(".ttf") else 100)
            for name in (regular_name, bold_name, _LICENSE_NAME)
        )
        if (
            requested_attr
            and source_attr
            and version_attr
            and refit_version_attr
            and assets_complete
            and requested_attr.group(1).casefold()
            == requested_family.casefold()
            and source_attr.group(1).casefold() == source_family.casefold()
            and version_attr.group(1) == _FIDELITY_VERSION
            and refit_version_attr.group(1) == _FIDELITY_VERSION
        ):
            return False

    regular = _resolve_dejavu(source_family, "Book")
    bold = _resolve_dejavu(source_family, "Bold")
    license_candidates = (
        Path("/usr/share/doc/fonts-dejavu-core/copyright"),
        Path("/usr/share/licenses/ttf-dejavu/LICENSE"),
        Path("/usr/share/licenses/dejavu-fonts/LICENSE"),
    )
    license_path = next((p for p in license_candidates if p.is_file()), None)
    if regular is None or bold is None or license_path is None:
        eprint(
            f"[paper2poster] WARN: {requested_family} is not portable on "
            f"this host: the licensed {source_family} fallback or its license "
            "notice could not be located; continuing with the platform font "
            "stack."
        )
        return False

    out_fonts.mkdir(parents=True, exist_ok=True)
    _copy_public_asset_atomic(regular, out_fonts / regular_name)
    _copy_public_asset_atomic(bold, out_fonts / bold_name)
    _copy_public_asset_atomic(license_path, out_fonts / _LICENSE_NAME)

    block = f'''<style id="poster-font-fidelity" data-fidelity-version="{_FIDELITY_VERSION}" data-requested-family="{requested_family}" data-source-family="{source_family}">
  /* Portable browser fallback. The CSS family name intentionally stays the
     requested OS family so native PPTX retains the user's selection. */
  @font-face {{
    font-family: "{requested_family}";
    src: url("assets/fonts/{regular_name}") format("truetype");
    font-style: normal;
    font-weight: 400;
    font-display: block;
  }}
  @font-face {{
    font-family: "{requested_family}";
    src: url("assets/fonts/{bold_name}") format("truetype");
    font-style: normal;
    font-weight: 700;
    font-display: block;
  }}
  /* Italic/oblique text intentionally synthesizes from these same custom
     normal faces. It cannot fall back to a client's local OS-family italic. */
</style>
<script id="poster-font-fidelity-refit" data-fidelity-version="{_FIDELITY_VERSION}">
(() => {{
  if (window.__posterFontFidelityRefitInstalled) return;
  window.__posterFontFidelityRefitInstalled = true;
  const refitAfterLayoutAssets = () => {{
    const fontsReady = document.fonts && document.fonts.ready
      ? Promise.resolve(document.fonts.ready).catch(() => {{}})
      : Promise.resolve();
    const mathReady = (() => {{
      const mj = window.MathJax;
      if (!mj || !mj.startup || !mj.startup.promise) return Promise.resolve();
      const typeset = Promise.resolve(mj.startup.promise).then(() =>
        typeof mj.typesetPromise === "function" ? mj.typesetPromise() : null
      );
      /* A blocked/failed math engine must not prevent the portable-font refit.
         Match the renderer's bounded wait, then continue with what loaded. */
      return Promise.race([
        typeset.catch(() => {{}}),
        new Promise(resolve => setTimeout(resolve, 15000)),
      ]);
    }})();
    Promise.all([fontsReady, mathReady]).then(() => {{
      /* Use the template's existing resize -> relayout() path. Its listener
         passes fitAll into __fitPosterStage while unscaled, so figures and the
         outer scale are recomputed together after final font AND math metrics
         settle in a reopened standalone HTML poster. */
      requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    }}).catch(() => {{}});
  }};
  if (document.readyState === "complete") refitAfterLayoutAssets();
  else window.addEventListener("load", refitAfterLayoutAssets, {{ once: true }});
}})();
</script>'''
    if _FIDELITY_PATTERN.search(text):
        text = _FIDELITY_PATTERN.sub(block, text, count=1)
    elif "</head>" in text:
        text = text.replace("</head>", block + "\n</head>", 1)
    else:
        text = block + "\n" + text
    html_path.write_text(text, encoding="utf-8")
    eprint(
        f"[paper2poster] froze {requested_family} browser rendering to "
        f"bundled {source_family} (PPTX family remains {requested_family})."
    )
    return True
