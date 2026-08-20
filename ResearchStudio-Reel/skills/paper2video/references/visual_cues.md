# Paper2Video visual cue bridge

Paper2Video owns semantic cue planning for a research-paper narrative. The
installed `pptx2video` runtime owns the cue JSON schema and renders the accepted
boxes and points. Read the standalone skill's `references/visual_cues.md` when
changing that public schema or renderer behavior.

## Semantic and geometry review

`generate_visual_cues.py` keeps semantic matching separate from rendered
geometry. A cue may select an SVG semantic group, then render a matched PPTX
box. It may promote a line-level text target to a nearby module when that
parent remains suitably bounded. Connected PPTX clusters are filtered so a
large union box does not cross unrelated regions.

Always write review artifacts during automatic cue generation:

```bash
python skills/paper2video/scripts/generate_visual_cues.py <project_path> \
  --script-json <project_path>/audio/script.json \
  --audio-dir <project_path>/audio \
  --pptx <project_path>/exports/<name>.pptx \
  --timings-json <project_path>/audio/word_timings.json \
  --strict-gate \
  --require-timestamps \
  --out <project_path>/visual_cues.json \
  --geometry-report-out <project_path>/geometry_resolution.json \
  --cue-plan-out <project_path>/visual_cue_plan.json \
  --audit-out <project_path>/cue_audit.json \
  --html-audit-out <project_path>/cue_audit.html \
  --candidate-review-out <project_path>/cue_candidate_review.html
```

Use `cue_candidate_review.html` to inspect the narration chunk, word-timing
match, semantic target, final geometry target, promotion reason, and rejected
candidates.

## Anchor contract

Create the contract before asking ppt-master to author the deck:

```bash
python skills/paper2video/scripts/generate_cue_requirements.py \
  <project_path>/audio/script.json \
  --out <project_path>/cue_requirements.json \
  --contract-out <project_path>/visual_anchor_contract.json \
  --markdown-out <project_path>/cue_requirements.md
```

Write each stable `cue_` anchor into both SVG and PPTX metadata. Anchor a chart
row, formula block, diagram panel, card, or figure subregion. Do not anchor
headers, captions, logos, QR tiles, page numbers, or decorative backgrounds.
When `--anchor-contract` is present, require exact `anchor_id` matching instead
of falling back to layout guesses.

For final highlighted video, require PPTX-backed anchors:

```bash
python skills/paper2video/scripts/generate_visual_cues.py <project_path> \
  --script-json <project_path>/audio/script.json \
  --audio-dir <project_path>/audio \
  --pptx <project_path>/exports/<name>.pptx \
  --anchor-contract <project_path>/visual_anchor_contract.json \
  --require-pptx-anchors \
  --timings-json <project_path>/audio/word_timings.json \
  --strict-gate \
  --require-timestamps \
  --out <project_path>/visual_cues.json \
  --geometry-report-out <project_path>/geometry_resolution.json \
  --candidate-review-out <project_path>/cue_candidate_review.html \
  --cue-plan-out <project_path>/visual_cue_plan.json \
  --audit-out <project_path>/cue_audit.json \
  --html-audit-out <project_path>/cue_audit.html \
  --repair-out <project_path>/cue_repair_requests.json \
  --repair-md-out <project_path>/cue_repair_requests.md
```

Review `geometry_resolution.json` and `cue_audit.html` when a box is too broad
or too narrow. Use `--no-prefer-pptx-geometry` only to debug SVG/PPTX geometry.
If strict mode fails, repair the deck or narration and rerun before rendering.
