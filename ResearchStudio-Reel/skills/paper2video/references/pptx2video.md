# Standalone pptx2video bridge

Paper2Video delegates an existing or edited native PPTX to the independently
maintained [`ai-nuts/pptx2video`](https://github.com/ai-nuts/pptx2video)
skill and CLI. This skill does not copy or vendor that package's runtime.

Install `ppt-master` first, then install the standalone `pptx2video` skill and
its compatible 0.5.x public CLI runtime:

```bash
npx skills add hugohe3/ppt-master --skill ppt-master
npx skills add ai-nuts/pptx2video --skill pptx2video
python -m pip install \
  'pptx2video[svg] @ git+https://github.com/ai-nuts/pptx2video.git@v0.5.0'
python -m playwright install chromium
pptx2video --version
```

The skill can be invoked directly as `/pptx2video`. Paper2Video uses the same
installed public CLI and has no repository-relative source path.

Verify Python, native rendering, and SVG browser dependencies:

```bash
pptx2video doctor --svg
```

Render into a new output directory:

```bash
pptx2video render <deck.pptx> <new-video-bundle> --resolution 1080p
```

The output directory must not already exist. Treat a zero exit status as valid
only because the standalone CLI itself requires strict QA with zero errors and
zero warnings. For authoring protocol, conflict resolution, and advanced flags,
use the installed `/pptx2video` skill and its references in the standalone
repository.
