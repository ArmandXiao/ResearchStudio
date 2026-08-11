"""Portrait composition and sampling regression tests.

These tests are deliberately browser-free.  They pin the deterministic batch
sampler and the source-DOM contracts that the rendering stages depend on.

Run with either::

    pytest ResearchStudio-Reel/skills/paper2poster/tests/test_portrait_composition.py -q
    python -m unittest ResearchStudio-Reel/skills/paper2poster/tests/test_portrait_composition.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from lxml import html as LH


SKILL = Path(__file__).resolve().parent.parent
REFERENCES = SKILL / "references"
sys.path.insert(0, str(REFERENCES))
sys.path.insert(0, str(SKILL / "scripts"))

import compose_poster  # noqa: E402
import apply_theme  # noqa: E402
import render_poster  # noqa: E402


BODY_AXES = ("orientation", "layout", "style", "header", "theme")


def _composition(html_path: Path) -> dict:
    """Read the embedded composition manifest from a composed poster."""
    root = LH.parse(str(html_path)).getroot()
    nodes = root.xpath('//script[@id="paper2poster-composition"]')
    if len(nodes) != 1:
        raise AssertionError(
            f"expected one composition manifest in {html_path}, got {len(nodes)}"
        )
    return json.loads(nodes[0].text or "")


def _body_axes(html_path: Path) -> dict[str, str | None]:
    """Read the five resolved composition axes stamped on ``<body>``."""
    root = LH.parse(str(html_path)).getroot()
    nodes = root.xpath("//body")
    if len(nodes) != 1:
        raise AssertionError(f"expected one body in {html_path}, got {len(nodes)}")
    return {
        axis: nodes[0].get(f"data-poster-{axis}")
        for axis in BODY_AXES
    }


def _compose(out: Path, **overrides) -> tuple[Path, dict]:
    """Compose a Portrait poster quietly and return its embedded manifest."""
    kwargs = {
        "layout": "random",
        "style": "random",
        "header": "random",
        "outpath": out,
        "orientation": "portrait",
        "theme": "random",
    }
    kwargs.update(overrides)
    with contextlib.redirect_stdout(io.StringIO()):
        result = compose_poster.compose(**kwargs)
    return result, _composition(result)


class PortraitSamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_explicit_seed_is_stable_and_output_path_independent(self) -> None:
        first, first_meta = _compose(
            self.tmp / "paper-a" / "poster.html", seed="stable-paper-seed"
        )
        second, second_meta = _compose(
            self.tmp / "paper-b" / "different-name.html", seed="stable-paper-seed"
        )

        self.assertEqual(first_meta["seed_source"], "argument")
        self.assertEqual(first_meta["seed_sha256"], second_meta["seed_sha256"])
        self.assertEqual(first_meta["resolved"], second_meta["resolved"])
        self.assertEqual(first.read_text(encoding="utf-8"),
                         second.read_text(encoding="utf-8"))

    def test_variant_seed_and_index_replay_is_byte_stable(self) -> None:
        kwargs = {
            "seed": "stable-replay-base",
            "variant_index": 17,
            "variant_seed": "stable-replay-wave",
        }
        first, first_meta = _compose(
            self.tmp / "replay-a" / "poster.html", **kwargs
        )
        first_bytes = first.read_bytes()

        # Prove replay is derived from seed + catalog rather than depending on
        # the process-local incremental sampler cache.
        with compose_poster._JOINT_CACHE_LOCK:
            compose_poster._JOINT_CACHE.clear()

        replay, replay_meta = _compose(first, **kwargs)
        second, second_meta = _compose(
            self.tmp / "replay-b" / "different-name.html", **kwargs
        )

        self.assertEqual(replay_meta, first_meta)
        self.assertEqual(replay.read_bytes(), first_bytes)
        self.assertEqual(second_meta, first_meta)
        self.assertEqual(second.read_bytes(), first_bytes)

    def test_default_seed_uses_resolved_output_path_and_spreads_directories(self) -> None:
        manifests = []
        for index in range(8):
            _, manifest = _compose(
                self.tmp / f"paper-{index:02d}" / "poster.html"
            )
            manifests.append(manifest)

        self.assertTrue(all(m["seed_source"] == "resolved_output_path"
                            for m in manifests))
        self.assertEqual(len({m["seed_sha256"] for m in manifests}), 8)
        resolved = {
            tuple(m["resolved"][axis]
                  for axis in ("layout", "style", "header", "theme"))
            for m in manifests
        }
        self.assertGreater(
            len(resolved), 1,
            "different absolute output directories should not collapse to one style",
        )

    def test_thirty_balanced_portrait_variants_cover_every_axis(self) -> None:
        manifests = []
        for index in range(30):
            _, manifest = _compose(
                self.tmp / f"variant-{index:02d}" / "poster.html",
                variant_index=index,
                variant_seed="portrait-batch-2026",
            )
            manifests.append(manifest)

        axes = ("layout", "style", "header", "theme")
        combinations = {
            tuple(m["resolved"][axis] for axis in axes) for m in manifests
        }
        self.assertEqual(len(combinations), 30)

        expected_pools = {
            "layout": {"full", "half"},
            "style": {
                "elevated", "framed", "left-bar",
                "legend-frame", "neo-brutal", "simple", "solid", "tag",
                "tinted",
            },
            "header": {"pv1", "pv2", "pv3", "pv4", "pv5"},
            "theme": {
                "blue", "burgundy", "green", "mono", "plum", "purple",
                "rust", "slate", "teal",
            },
        }
        for axis, expected in expected_pools.items():
            counts = Counter(m["resolved"][axis] for m in manifests)
            self.assertEqual(set(counts), expected, axis)
            self.assertLessEqual(
                max(counts.values()) - min(counts.values()), 1,
                f"{axis} is not marginally balanced: {dict(counts)}",
            )

    def test_thirty_balanced_variants_with_fixed_layout_cover_random_axes(self) -> None:
        """Method-driven layout selection must not weaken visual diversity."""
        manifests = []
        for index in range(30):
            _, manifest = _compose(
                self.tmp / f"fixed-layout-{index:02d}" / "poster.html",
                layout="full",
                variant_index=index,
                variant_seed="portrait-fixed-layout-batch-2026",
            )
            manifests.append(manifest)

        self.assertTrue(all(m["resolved"]["layout"] == "full"
                            for m in manifests))
        combinations = {
            tuple(m["resolved"][axis]
                  for axis in ("style", "header", "theme"))
            for m in manifests
        }
        self.assertEqual(len(combinations), 30)
        self.assertEqual(
            {m["resolved"]["style"] for m in manifests},
            {
                "elevated", "framed", "left-bar",
                "legend-frame", "neo-brutal", "simple", "solid", "tag",
                "tinted",
            },
        )
        self.assertEqual(
            {m["resolved"]["header"] for m in manifests},
            {"pv1", "pv2", "pv3", "pv4", "pv5"},
        )
        self.assertEqual(
            {m["resolved"]["theme"] for m in manifests},
            {
                "blue", "burgundy", "green", "mono", "plum", "purple",
                "rust", "slate", "teal",
            },
        )

    def test_joint_sampler_property_fixed_layout_100_seeds(self) -> None:
        """Every 30-poster fixed-layout wave is unique and marginally fair."""
        axis_options = {
            "style": [
                "elevated", "framed", "left-bar", "legend-frame",
                "neo-brutal", "simple", "solid", "tag", "tinted",
            ],
            "header": ["pv1", "pv2", "pv3", "pv4", "pv5"],
            "theme": [
                "blue", "burgundy", "green", "mono", "plum", "purple",
                "rust", "slate", "teal",
            ],
        }
        for seed_index in range(100):
            seed = f"portrait-joint-fixed-property-{seed_index:03d}"
            resolved = [
                compose_poster._joint_balanced_pick(
                    axis_options, seed=seed, index=index
                )
                for index in range(30)
            ]
            tuples = [
                tuple(item[axis] for axis in axis_options) for item in resolved
            ]
            self.assertEqual(len(set(tuples)), 30, seed)
            for axis, pool in axis_options.items():
                counts = Counter(item[axis] for item in resolved)
                self.assertEqual(set(counts), set(pool), (seed, axis))
                self.assertLessEqual(
                    max(counts.values()) - min(counts.values()), 1,
                    (seed, axis, dict(counts)),
                )

    def test_joint_sampler_property_full_random_100_seeds(self) -> None:
        """Adding the layout axis keeps 30 complete tuples collision-free."""
        axis_options = {
            "layout": ["full", "half"],
            "style": [
                "elevated", "framed", "left-bar", "legend-frame",
                "neo-brutal", "simple", "solid", "tag", "tinted",
            ],
            "header": ["pv1", "pv2", "pv3", "pv4", "pv5"],
            "theme": [
                "blue", "burgundy", "green", "mono", "plum", "purple",
                "rust", "slate", "teal",
            ],
        }
        for seed_index in range(100):
            seed = f"portrait-joint-full-property-{seed_index:03d}"
            resolved = [
                compose_poster._joint_balanced_pick(
                    axis_options, seed=seed, index=index
                )
                for index in range(30)
            ]
            tuples = [
                tuple(item[axis] for axis in axis_options) for item in resolved
            ]
            self.assertEqual(len(set(tuples)), 30, seed)
            for axis, pool in axis_options.items():
                counts = Counter(item[axis] for item in resolved)
                self.assertEqual(set(counts), set(pool), (seed, axis))
                self.assertLessEqual(
                    max(counts.values()) - min(counts.values()), 1,
                    (seed, axis, dict(counts)),
                )

    def test_equal_size_style_and_theme_pools_do_not_lock_pairings(self) -> None:
        manifests = []
        for index in range(27):
            _, manifest = _compose(
                self.tmp / f"pairing-{index:02d}" / "poster.html",
                layout="full",
                variant_index=index,
                variant_seed="portrait-pairing-diversity-2026",
            )
            manifests.append(manifest)

        pairs = {
            (m["resolved"]["style"], m["resolved"]["theme"])
            for m in manifests
        }
        self.assertGreater(
            len(pairs),
            9,
            "equal-size style and theme pools must not repeat one fixed pairing map",
        )
        for start in (0, 9, 18):
            block = manifests[start:start + 9]
            self.assertEqual(len({m["resolved"]["style"] for m in block}), 9)
            self.assertEqual(len({m["resolved"]["theme"] for m in block}), 9)

    def test_landscape_balanced_sampling_keeps_all_eleven_styles(self) -> None:
        styles = set()
        for index in range(11):
            output = self.tmp / f"landscape-variant-{index:02d}" / "poster.html"
            with contextlib.redirect_stdout(io.StringIO()):
                compose_poster.compose(
                    "half",
                    "random",
                    "v1",
                    output,
                    orientation="landscape",
                    theme="blue",
                    variant_index=index,
                    variant_seed="landscape-style-preservation-2026",
                )
            styles.add(_composition(output)["resolved"]["style"])

        self.assertEqual(
            styles,
            {
                "solid", "framed", "simple", "left-bar", "elevated",
                "neo-brutal", "tag", "underline", "tinted", "double-rule",
                "legend-frame",
            },
        )

    def test_manifest_records_batch_sampler_metadata(self) -> None:
        variant_seed = "portrait-manifest-wave"
        variant_index = 23
        _, manifest = _compose(
            self.tmp / "manifest" / "poster.html",
            seed="portrait-manifest-base",
            variant_index=variant_index,
            variant_seed=variant_seed,
        )

        catalog_payload = json.dumps(
            manifest["catalog"], sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(manifest["schema_version"],
                         "paper2poster.composition.v1")
        self.assertEqual(manifest["sampler_version"],
                         compose_poster.SAMPLER_VERSION)
        self.assertEqual(manifest["seed_source"], "argument")
        self.assertEqual(manifest["variant_index"], variant_index)
        self.assertEqual(
            manifest["variant_seed_sha256"],
            hashlib.sha256(variant_seed.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            manifest["catalog_digest"],
            hashlib.sha256(catalog_payload.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            manifest["variant_joint_axes"],
            ["layout", "style", "header", "theme"],
        )
        self.assertEqual(manifest["variant_combination_space"], 2 * 9 * 5 * 9)

    def test_selection_out_matches_embedded_manifest(self) -> None:
        selection = self.tmp / "audit" / "selection.json"
        output, embedded = _compose(
            self.tmp / "paper" / "poster.html",
            seed="selection-audit",
            selection_out=selection,
        )
        self.assertTrue(output.exists())
        external = json.loads(selection.read_text(encoding="utf-8"))
        body_axes = _body_axes(output)
        expected_body_axes = {
            axis: embedded["resolved"][axis] for axis in BODY_AXES
        }

        self.assertEqual(external, embedded)
        self.assertEqual(body_axes, expected_body_axes)
        self.assertEqual(
            body_axes,
            {axis: external["resolved"][axis] for axis in BODY_AXES},
        )

    def test_retheme_keeps_css_html_and_selection_metadata_aligned(self) -> None:
        selection = self.tmp / "retheme" / "selection.json"
        output, _ = _compose(
            self.tmp / "retheme" / "poster.html",
            style="solid", header="pv1", theme="blue",
            selection_out=selection,
        )

        resolved = apply_theme.apply_theme_to_file(output, "rust")
        html = output.read_text(encoding="utf-8")
        embedded = _composition(output)
        external = json.loads(selection.read_text(encoding="utf-8"))

        self.assertEqual(resolved, "rust")
        self.assertIn(f'--accent: {apply_theme.THEMES["rust"]["accent"]}', html)
        self.assertEqual(_body_axes(output)["theme"], "rust")
        self.assertEqual(embedded["resolved"]["theme"], "rust")
        self.assertEqual(external["resolved"]["theme"], "rust")

    def test_environment_variant_seed_matches_explicit_batch_seed(self) -> None:
        variant_seed = "portrait-env-wave"
        common = {
            "seed": "portrait-env-base",
            "variant_index": 19,
        }
        with mock.patch.dict(
            os.environ, {"POSTER_VARIANT_SEED": variant_seed}, clear=False
        ):
            from_environment, environment_manifest = _compose(
                self.tmp / "env-variant" / "poster.html", **common
            )

        explicit, explicit_manifest = _compose(
            self.tmp / "explicit-variant" / "poster.html",
            variant_seed=variant_seed,
            **common,
        )

        self.assertEqual(environment_manifest, explicit_manifest)
        self.assertEqual(from_environment.read_bytes(), explicit.read_bytes())
        self.assertEqual(environment_manifest["variant_index"], 19)
        self.assertEqual(
            environment_manifest["variant_seed_sha256"],
            hashlib.sha256(variant_seed.encode("utf-8")).hexdigest(),
        )

    def test_cli_honors_portrait_axis_environment_defaults(self) -> None:
        output = self.tmp / "env" / "poster.html"
        selection = self.tmp / "env" / "selection.json"
        env = {
            "POSTER_ORIENTATION": "portrait",
            "POSTER_STYLE": "tag",
            "POSTER_HEADER": "pv4",
            "POSTER_THEME": "mono",
            "POSTER_SEED": "env-seed",
            "POSTER_VARIANT_SEED": "env-wave",
            "POSTER_MATH": compose_poster.MATH_ENGINE_DEFAULT,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = compose_poster.main([
                    "--layout", "full",
                    "--out", str(output),
                    "--selection-out", str(selection),
                ])
        self.assertEqual(rc, 0)
        manifest = json.loads(selection.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["resolved"],
            {
                "orientation": "portrait",
                "layout": "full",
                "style": "tag",
                "header": "pv4",
                "scan": None,
                "theme": "mono",
                "math": compose_poster.MATH_ENGINE_DEFAULT,
            },
        )
        self.assertEqual(manifest["seed_source"], "environment")

    def test_invalid_orientation_environment_fails_instead_of_falling_back(self) -> None:
        output = self.tmp / "invalid-orientation" / "poster.html"
        with mock.patch.dict(
            os.environ,
            {"POSTER_ORIENTATION": "sideways"},
            clear=False,
        ):
            with self.assertRaises(SystemExit) as raised:
                compose_poster.main([
                    "--layout", "full",
                    "--out", str(output),
                ])
        self.assertIn("unknown orientation", str(raised.exception))
        self.assertFalse(output.exists())


class PortraitStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_portrait_header_composes_with_both_layouts(self) -> None:
        for layout in ("full", "half"):
            for header in ("pv1", "pv2", "pv3", "pv4", "pv5"):
                with self.subTest(layout=layout, header=header):
                    output, manifest = _compose(
                        self.tmp / layout / header / "poster.html",
                        layout=layout,
                        style="solid",
                        header=header,
                        theme="blue",
                    )
                    html = output.read_text(encoding="utf-8")
                    self.assertFalse(
                        any(hook in html for hook in compose_poster.STRUCT_HOOKS),
                        "a structural composition hook survived injection",
                    )
                    self.assertIn(f'data-poster-layout="{layout}"', html)
                    self.assertIn(f'data-poster-header="{header}"', html)
                    self.assertEqual(manifest["resolved"]["layout"], layout)
                    self.assertEqual(manifest["resolved"]["header"], header)

    def test_every_portrait_style_composes_with_both_portrait_layouts(self) -> None:
        styles = (
            "solid", "framed", "simple", "left-bar", "elevated",
            "neo-brutal", "tag", "tinted", "legend-frame",
        )
        for layout in ("full", "half"):
            for style in styles:
                with self.subTest(layout=layout, style=style):
                    output, manifest = _compose(
                        self.tmp / "styles" / layout / style / "poster.html",
                        layout=layout,
                        style=style,
                        header="pv1",
                        theme="mono",
                    )
                    html = output.read_text(encoding="utf-8")
                    self.assertFalse(
                        any(hook in html for hook in compose_poster.STRUCT_HOOKS),
                        "a structural composition hook survived injection",
                    )
                    self.assertIn(f'data-poster-style="{style}"', html)
                    self.assertEqual(manifest["resolved"]["style"], style)

    def test_portrait_rejects_underline_and_double_rule(self) -> None:
        for style in ("underline", "double-rule"):
            output = self.tmp / "excluded-styles" / style / "poster.html"
            with self.subTest(style=style):
                with self.assertRaises(SystemExit) as raised:
                    _compose(
                        output,
                        layout="half",
                        style=style,
                        header="pv1",
                        theme="blue",
                    )
                self.assertIn("Landscape-only", str(raised.exception))
                self.assertFalse(output.exists())

    def test_landscape_keeps_underline_and_double_rule(self) -> None:
        for style in ("underline", "double-rule"):
            output = self.tmp / "landscape-styles" / style / "poster.html"
            with self.subTest(style=style):
                with contextlib.redirect_stdout(io.StringIO()):
                    compose_poster.compose(
                        "half",
                        style,
                        "v1",
                        output,
                        orientation="landscape",
                        theme="blue",
                    )
                manifest = _composition(output)
                self.assertEqual(manifest["resolved"]["style"], style)
                self.assertIn("underline", manifest["catalog"]["styles"])
                self.assertIn("double-rule", manifest["catalog"]["styles"])

    def test_full_has_takeaway(self) -> None:
        output, _ = _compose(
            self.tmp / "full" / "poster.html",
            layout="full",
            style="solid",
            header="pv1",
            theme="blue",
        )
        root = LH.parse(str(output)).getroot()
        takeaway = root.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), '
            '" section ")][@data-section="takeaway"]'
        )
        self.assertEqual(len(takeaway), 1)

    def test_half_has_exactly_one_bottom_grow_section_per_column(self) -> None:
        output, _ = _compose(
            self.tmp / "half" / "poster.html",
            layout="half",
            style="solid",
            header="pv1",
            theme="blue",
        )
        root = LH.parse(str(output)).getroot()
        columns = root.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), '
            '" columns ")]/*[contains(concat(" ", normalize-space(@class), '
            '" "), " col ")]'
        )
        self.assertEqual(len(columns), 2)
        for index, column in enumerate(columns):
            grows = column.xpath(
                './/*[contains(concat(" ", normalize-space(@class), " "), '
                '" section ") and contains(concat(" ", normalize-space(@class), '
                '" "), " grow ")]'
            )
            self.assertEqual(
                len(grows), 1,
                f"Portrait Half column {index} must have exactly one .grow section",
            )
            direct_sections = column.xpath(
                './*[contains(concat(" ", normalize-space(@class), " "), '
                '" section ")]'
            )
            self.assertIs(
                grows[0], direct_sections[-1],
                f"Portrait Half column {index} .grow section must be last",
            )

    def test_full_results_band_keeps_asymmetric_three_column_ratio(self) -> None:
        output, _ = _compose(
            self.tmp / "full-ratio" / "poster.html",
            layout="full",
            style="solid",
            header="pv1",
            theme="blue",
        )
        html = output.read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: 1.5fr 1fr 1fr", html)


class PortraitRenderGeometryTests(unittest.TestCase):
    def test_decimal_portrait_canvas_scales_below_rounded_viewport(self) -> None:
        scale = render_poster._pdf_content_scale((33.1, 46.8), (3178, 4493))
        self.assertGreater(scale, 0.999)
        self.assertLess(scale, 1.0)

    def test_integer_landscape_canvas_keeps_identity_scale(self) -> None:
        self.assertEqual(
            render_poster._pdf_content_scale((60.0, 36.0), (5760, 3456)),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
