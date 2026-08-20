# paper2video

> Turn a paper's shared assets into a narrated research walkthrough video — editable PPTX, subtitle-ready MP4, no-subtitle MP4 for reels, timeline metadata, and visual attention cues — without breaking alignment between slides, audio, captions, and sections.

`paper2video` is the **video rendering stage** of the ResearchStudio pipeline. It prefers the `<outdir>/` bundle produced by [`paper2assets`](../paper2assets/) so the video uses the same section claims, figures, numbers, logos, QR codes, and narration source as `paper2poster`, `paper2blog`, and `paper2reel`.

```
paper2assets  ──▶  ppt-master  ──▶  paper2video  ──▶  paper2reel
  <outdir>/          deck + notes     video.mp4        no-subtitle video + timeline
                                      video.pptx
```

## Input

Either:

- a `paper2assets` `<outdir>/` containing `manifest.json` and `assets/meta/paper_spec.md`;
- a raw paper PDF, which is resolved to the same `<pdf_stem>/` bundle convention and completed through `paper2assets` first;
- an existing ppt-master project / PPTX with speaker notes for the deck-video route.

The production route should use the shared `paper2assets` package whenever it exists.

## Output

Route A writes back into the same v2 bundle root, next to `manifest.json` and
`assets/`:

| File | What it is |
|---|---|
| `video.mp4` | Final H.264/AAC video with burned-in subtitles in an appended black bottom band |
| `video_no_subtitles.mp4` | Required raw playback copy for `paper2reel`, so the reel CC toggle does not double-subtitle the video |
| `video.pptx` | Editable deck used to render the video |
| `assets/audio/*.{mp3,json}` | Per-section narration, script JSON, word timings, and TTS manifests |
| `assets/captions/{video.srt,video.vtt}` | Subtitle sidecars used for burn-in and reel captions |
| `assets/slides/` | Deck exports and exact rendered slide frames used by the MP4 |
| `assets/clips/` | Raw render / segment intermediates, including the audit copy of the no-subtitle video |
| `assets/meta/` | Timeline, duration reports, visual cues, anchor contracts, and QA reports |

The top level holds only deliverables plus `manifest.json`; everything else lives under `assets/`.
Route B delegates an existing or edited PPTX to the standalone CLI and must use
a fresh output path that does not already exist.

## Usage

Install the two external skills in order, then install and verify the public
`pptx2video` CLI runtime. The runtime requires Python 3.11 or newer:

```bash
npx skills add hugohe3/ppt-master --skill ppt-master
npx skills add ai-nuts/pptx2video --skill pptx2video
python -m pip install \
  'pptx2video[svg] @ git+https://github.com/ai-nuts/pptx2video.git@v0.5.0'
python -m playwright install chromium
pptx2video --version
pptx2video doctor --svg
```

For Route B, keep that destination separate from any existing Route A or
paper2assets bundle:

```bash
PPTX2VIDEO_OUT=/absolute/path/to/new-video-bundle
test ! -e "$PPTX2VIDEO_OUT"
pptx2video render <deck.pptx> "$PPTX2VIDEO_OUT" --resolution 1080p
```

From a Claude Code session:

```text
# preferred: point at the shared paper2assets bundle
> /paper2video ./my_paper/

# or start from a raw PDF; the skill resolves the same bundle root first
> /paper2video ./my_paper.pdf

# render an existing or edited deck through the installed standalone skill
> /pptx2video ./edited.pptx
```

The final package is not complete until `video.mp4`, `video_no_subtitles.mp4`, `video.pptx`, `assets/meta/timeline.json`, and the video QA report are all present.

## How it works

1. **Resolve one bundle root** — the same `<outdir>/` used by `paper2assets`, `paper2poster`, and `paper2blog`.
2. **Shape the narration** with duration control before TTS, using the shared section ids from `assets/meta/narration.json`.
3. **Delegate deck generation to ppt-master** — the skill must run the full ppt-master workflow, not a hand-written shortcut deck.
4. **Generate audio** with the shared `paper2poster/scripts/generate_audio.py` synthesizer, preserving one MP3 per script section.
5. **Build visual cue contracts** so important slide regions are anchored to narration chunks.
6. **Delegate rendering and subtitles** to `python -m pptx2video.render_video` and `python -m pptx2video.add_subtitles`; the public MP4 may use burned, soft, bottom-bar, or disabled captions while the raw compatibility render remains available to downstream Paper2Reel.
7. **Delegate timeline metadata** to `python -m pptx2video.build_timeline` so `paper2reel` can map poster sections, slide thumbnails, subtitles, and video seek times.
8. **Delegate strict media QA** to `python -m pptx2video.check_video_package`.

## Visual attention cues

The production highlight style is `spotlight_laser`: a feathered spotlight over the accepted slide region plus a small red laser-pointer dot at the cue center. The renderer also supports the independent pointer/region combinations `box`, `cursor`, `box_cursor`, `spotlight`, `spotlight_cursor`, `laser`, and `box_laser` for comparison and repair work.

Visual cues are generated from the deck, the script, word-boundary timings, and anchor metadata. Strict QA rejects malformed boxes, weak timing alignment, or low-confidence geometry when final attention cues are required.

## Duration control

Duration is controlled before audio is synthesized. `assets_to_script.py` / `notes_to_script.py` estimate section lengths against the target, `plan_tts_rate.py` checks measured MP4 duration, and large mismatches must be fixed by rewriting the narration rather than by clipping audio or truncating the final video.

## Paper2Video-owned scripts

```
scripts/
├── assets_to_script.py          # paper2assets narration -> video script + duration plan
├── notes_to_script.py           # ppt-master notes -> video script
├── generate_cue_requirements.py # script -> visual anchor contract for ppt-master
├── generate_visual_cues.py      # deck/script/timings -> positioned highlight cues
└── inject_pptx_anchors.py       # preserve cue anchors inside editable PPTX
```

The generic runtime is not owned or vendored by this skill. Install
`pptx2video`, then invoke `python -m pptx2video.generate_edge_audio`,
`python -m pptx2video.render_video`,
`python -m pptx2video.add_subtitles`,
`python -m pptx2video.build_timeline`, and
`python -m pptx2video.check_video_package`.

## Requirements

- Python >= 3.11
- Installed `ppt-master` and `pptx2video` skills
- Compatible `pptx2video` 0.5.x CLI runtime from `ai-nuts/pptx2video`, installed
  with the `svg` extra
- Playwright Chromium installed with `python -m playwright install chromium`
- A passing `pptx2video doctor --svg` check
- LibreOffice, Poppler, FFmpeg / FFprobe
- Playwright + Chrome/Chromium for SVG slide-frame rendering
- Edge TTS by default; Azure TTS is optional

## More detail

[`SKILL.md`](SKILL.md) is the authoritative, agent-facing spec: the full v2 output contract, both supported routes, duration-control loop, visual-cue contract, subtitle requirements, and strict QA gates. The [`references/`](references/) folder documents the narration schema, Paper2Video cue generation, and the minimal standalone-runtime bridge.
