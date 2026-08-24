# Standalone pptx2video CLI reference

Paper2Video delegates every render to the independently maintained
[`ai-nuts/pptx2video`](https://github.com/ai-nuts/pptx2video) skill and its
public 0.5.x CLI. This skill does not copy or vendor that package's runtime,
and it never imports a private `pptx2video.<module>` path. This file lists the
exact flags Step 5 of `SKILL.md` relies on and the pitfalls that make an
almost-right invocation silently diverge from the protocol.

## Install once per environment

```bash
npx skills add hugohe3/ppt-master --skill ppt-master
npx skills add ai-nuts/pptx2video --skill pptx2video
python -m pip install \
  'pptx2video[svg] @ git+https://github.com/ai-nuts/pptx2video.git@v0.5.0'
python -m playwright install chromium
python3 -m pptx2video --version
python3 -m pptx2video doctor --svg
```

## The PATH trap: always use `python3 -m pptx2video`

A host can have an older `pptx2video` earlier on `PATH` than the intended
0.5.x install. The bare `pptx2video` command then silently resolves to that
older version with no error; it does not fail, it just renders with a
different, incompatible flag surface. `python3 -m pptx2video --version` must
print `pptx2video 0.5.x`. Every command in this file and in `SKILL.md` uses
`python3 -m pptx2video`, never the bare `pptx2video` binary, for exactly this
reason.

## The render command Step 5 uses

```bash
python3 -m pptx2video render \
  "<project_path>/exports/<name>.pptx" \
  "$PPTX2VIDEO_TMP" \
  --resolution 1080p \
  --script-json "$VIDEO_AUDIO/script.json" \
  --ids-from-script "$VIDEO_AUDIO/script.json" \
  --animation-order-policy animation-pane \
  --click-group-policy <pptx2video_click_group_policy from Step 2>
```

`<PPTX2VIDEO_TMP>` (the `output` positional argument) must not already exist;
the CLI refuses to write into an existing path.

## Flags this skill's protocol depends on, and why

- **`--click-group-policy {normalize,preserve}` (no default assumed here).**
  The CLI's own default is `normalize`. This skill never relies on that
  default: it always passes the exact value
  `ppt_options_contract.py resolve-ppt-trigger` printed in Step 2
  (`normalize` for `on-click`, `preserve` for `after-previous`). Passing
  `normalize` when the user asked for `after-previous` silently rewrites a
  deliberately authored click-free cascade into a numbered click-driven deck,
  which is the opposite of the request. This is the single most important
  flag in the whole handoff; never omit it and never let it fall back to the
  CLI default.
- **`--animation-order-policy {auto,animation-pane,reading-order}`, default
  `auto`.** Pass `animation-pane` explicitly. The CLI's own `auto` default
  only prompts interactively when `stdin` is a TTY; in a non-interactive
  agent session with an actual Animation-Pane-versus-narration-order conflict,
  `auto` (and `reading-order`) exit `3` without producing a bundle at all,
  and print a decision report path instead of rendering. `animation-pane`
  confirms up front "trust the deck's own Animation Pane order," so a real
  conflict is resolved deterministically instead of stalling the run.
- **`--script-json PATH`.** Supplies section-level (or handle-level) narration
  text as the authority, overriding whatever the PPTX's own Notes/Alt Text
  carry. Required so pptx2video's narration does not silently depend on
  ppt-master's plain-paragraph Notes (see
  `references/ppt_trigger_handoff.md` and `references/script_json_schema.md`
  for why that matters).
- **`--ids-from-script PATH`.** Fixes each slide's `section_id` to this file's
  `sections[*].id`, in order. Pass the same file used for `--script-json`
  (paper2video's Step 4 builds exactly one file for both flags). This makes
  pptx2video's own section-count/section-id validation pass automatically;
  do not hand-build a separate id-mapping file.
- **`--resolution {720p,1080p,1440p,4k}`, default `1080p`.** Pass `1080p`
  explicitly for a predictable, documented default; do not silently rely on
  whatever the installed CLI version happens to default to.
- **Do not pass `--no-qa`.** It does not exist on the top-level `render`
  subcommand. QA is unconditionally enforced by `cli.py`'s `_render()`, which
  reads `assets/meta/reports/video_qa_report.json` after every render and
  raises `SystemExit` unless `passed` is `true` with zero `error` and zero
  `warning` counts. There is no flag on this CLI surface that disables that
  check; a hand-rolled call into an internal module is the only way to skip
  it, which is exactly why this skill forbids calling any internal module
  directly (see `SKILL.md`'s "Why full delegation is mandatory").
- **Do not pass `--baseline-pptx` / `--narration-mode regenerate` for a first
  render.** Those flags exist for an explicit later re-render of a previously
  delivered deck against a new edited PPTX; they are irrelevant to Step 5's
  first pass.
- **`--no-subtitles`.** Omit it unless the user explicitly disabled captions.
  The default burns captions into `video.mp4` in an appended black bottom
  band while leaving `video_no_subtitles.mp4` and the `.srt`/`.vtt` sidecars
  untouched, which is what `paper2reel` expects downstream.

## Full flag surface (from `pptx2video render --help`, 0.5.x)

```
pptx2video render <pptx> <output>
  --resolution {720p,1080p,1440p,4k}      default 1080p
  --fps FPS                               default 30
  --voice VOICE                           Edge TTS voice, default en-US-AriaNeural
  --rate RATE                             Edge TTS rate, default +0%
  --tts-cache-dir DIR | --no-tts-cache
  --ids-from-script PATH
  --script-json PATH
  --baseline-pptx PATH
  --narration-mode {keep,regenerate}      default keep
  --narration-order-policy {geometry,author-notes}   default geometry
  --regeneration-model MODEL              default gpt-5.6-sol
  --highlight-style {box,spotlight,cursor,box_cursor,spotlight_cursor,
                      laser,box_laser,spotlight_laser}  default spotlight_laser
  --animation-order-policy {auto,animation-pane,reading-order}  default auto
  --animation-order-sequence [SLIDE=]ORDER  (repeatable)
  --animation-order-report PATH
  --semantic-profile {concise,detailed}   default concise
  --click-group-policy {normalize,preserve}  default normalize
  --no-subtitles
  --keep-temp
```

There is no `--no-qa` flag on this list; see above.

## Verify dependencies before rendering

```bash
python3 -m pptx2video doctor --svg
```

Checks Python, native rendering (LibreOffice, Poppler, FFmpeg/FFprobe), and
SVG dependencies (Playwright plus a system or bundled Chromium). Fix whatever
`doctor` reports before running Step 5; do not proceed past a failing
`doctor` check by assuming the render will still work.

## Where narration audio actually comes from

`pptx2video render` synthesizes all narration audio itself, automatically,
using Edge TTS (`generate_edge_audio.py`, default voice
`en-US-AriaNeural`). Paper2Video does not pre-synthesize any MP3 and does not
pass a `--voice` flag by default; `--script-json`'s `text` is spoken content,
not a file of pre-rendered audio. See `references/script_json_schema.md` for
the exact schema `--script-json` and `--ids-from-script` both read.

## Advanced authoring surface

For direct manual edits to an already-rendered deck (handle-level Notes/Alt
Text markers, `--baseline-pptx` re-renders, `pptx2video bootstrap`), use the
installed `/pptx2video` skill directly and read its own
`references/cli.md` and `references/editable_pptx.md`. Those flows are
outside this skill's one supported route (paper -> ppt-master -> `render`);
this skill's Step 5 command above is the only render invocation this skill's
protocol issues.
