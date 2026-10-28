# Vendored code

## mermaid-ascii.js

- Package: [beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid) **1.1.3** (npm), MIT License, Copyright (c) 2026 Craft Docs. The license text is in `LICENSE` beside this file.
- npm integrity: `sha512-TItrtrAyHp1vwFfFVYauWGrquouk/6SS21Aq3RsxindSYZODcN4xYrPZD6BiZRU+o5mKJzDPz9MUSMvELdylyg==`
- What is in it: only the ASCII/Unicode renderer, `src/ascii/index.ts` and what it imports (35 modules: the flowchart, state, sequence, class, ER and xychart parsers and text renderers). The SVG renderer and its dependencies (`elkjs`, `entities`) are not in the bundle. No Node or DOM API is used on the path the mod calls: `detectColorMode`, the one function that reads `globalThis.process` or `document`, runs only when `colorMode` is not given, and `hooks/mermaid.ts` always gives it.
- Export used: `renderMermaidASCII(text, options)`. Types: `mermaid-ascii.d.ts` (written by hand, only what the mod uses).
- sha256 of the shipped file: `b6845d25b4e957746294102b952de476ed720b0f44f9cfe82b7bfc59d25fd726`
- Supported diagram types in 1.1.3: `flowchart`/`graph`, `stateDiagram`/`stateDiagram-v2`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `xychart-beta`. Everything else (pie, gantt, mindmap, ...) is refused by `hooks/mermaid.ts` before the library sees it. The xychart plot is a fixed 60 columns wide in this version (about 65-70 columns with the axis), so a narrower terminal shows the source with a reason.

### Patches

`patch-mermaid-ascii.py` applies these to the bundle (each marked `inline-math patch N` in the code; each is an exact replacement that must match once):

1. Bug: node placement keeps one position per level in a fixed array of 100 slots; a flowchart deeper than 24 levels read `undefined`, made `NaN` coordinates, and the edge search then never ended (the page hung until `Map maximum size exceeded`). A missing slot now reads 0.
2. Bug: the A* edge router searched an unbounded quarter-plane, so an unreachable target looped until memory ran out. It now refuses non-finite ends and searches only up to 16 grid cells past the drawing. The drawing's extent is cached per grid and recomputed when the grid's size changes; the grid is append-only (only `set` is ever called on it), so the size is a sound staleness check.
3. Speed: a blank canvas column is made with `fill`, not one `push` per cell.
4. Speed: `mergeCanvases` merges into the base canvas when the overlays fit it (every caller replaces the base with the result), and the flowchart drawer sets character roles in the same pass instead of a second scan of every overlay.
5. Speed: an edge with no arrowhead at an end, or with no label, gets a 1x1 blank overlay instead of a copy of the whole canvas.

Patches 3-5 do not change output: on a corpus of 72 renders (flowcharts in all directions with subgraphs, shapes, labels, bundles; sequence, state, class, ER), the patched and unpatched bundles give byte-identical results, colour codes included. Together they take a 30-node flowchart from about 15 ms to about 6 ms (node 26, after warmup).

### How it was built (and how to update it)

Run each step outside the download folder; never run the package's scripts.

```sh
mkdir -p /tmp/bm/npm /tmp/bm/build && cd /tmp/bm/npm
echo '{"name":"scratch","private":true}' > package.json
npm install --ignore-scripts --no-audit --no-fund beautiful-mermaid@1.1.3
npm view beautiful-mermaid@1.1.3 dist.integrity   # compare with package-lock.json
cd /tmp/bm/build
echo "export { renderMermaidASCII } from '../npm/node_modules/beautiful-mermaid/src/ascii/index.ts'" > entry.ts
bun build entry.ts --target=browser --format=esm \
  --banner='/*! beautiful-mermaid 1.1.3 (https://github.com/lukilabs/beautiful-mermaid), MIT License, Copyright (c) 2026 Craft Docs. ASCII/Unicode renderer only (src/ascii/index.ts), bundled unminified with bun build and patched; see VENDOR.md. */' \
  --outfile=out/mermaid-ascii.js
python3 -I <mod>/hooks/vendor/patch-mermaid-ascii.py out/mermaid-ascii.js
cp out/mermaid-ascii.js <mod>/hooks/vendor/mermaid-ascii.js
cp ../npm/node_modules/beautiful-mermaid/LICENSE <mod>/hooks/vendor/LICENSE
```

Built with bun 1.4.4. The bundle is left unminified so it can be read and reviewed.

To update: change the version in the steps above (and the comment rewrite at the top of the patch script), read the upstream changes to `src/ascii/` (`grid.ts` placement and `pathfinder.ts` especially, in case patches 1-2 were fixed upstream), rebuild, and run the patch script. A patch that no longer matches stops the script; fix or drop it. Then check that the speed patches still leave the output unchanged (render a corpus with the patched and unpatched bundles and compare, colours included), and run `claude plugin test` on the mod: `hooks/mermaid.test.ts` imports the bundle through `hooks/mermaid.ts`, so it also shows the bundle loads in the hooks runtime.

### Known layout limits (not patched)

Upstream 1.1.3 has layout bugs that lose information rather than crash
(found in review; the patches above do not touch the layout of edges):

- A back edge of a loop (`A --> B` then `B -->|no| A`) is drawn on the
  same path as the forward edge, so one of the two is lost, and a
  left-right pair of opposite edges can become one double-headed line.
- An edge into a node placed on the drawing's last row can leave the
  canvas and be cut off (`D --> B` in `A-->B{Valid?}; B-->|No|D; D-->B`).
- A cycle into a subgraph throws a TypeError.

`hooks/mermaid.ts` does not patch these. It checks each flowchart it draws
(an arrowhead for every node an edge points at, no line running off the
first or last row), tries its other layouts, and otherwise shows the
source with the reason, so a wrong diagram is never shown as right.

### Credits

The wide-character trick in `hooks/mermaid.ts` (a filler character after each two-cell grapheme so the library, which counts one cell per UTF-16 unit, sizes boxes right, removed after drawing), the LR-to-TD retry when a chart is too wide, the dropping of inline HTML tags and of `-->  |label|` spacing come from [prismantis](https://github.com/NahumLitvin/prismantis) (MIT, `hooks/mermaid.tsx`, which vendors its own build of beautiful-mermaid as `hooks/vendor/mermaid-text.js`). No code from it is copied; this mod has no dependency on it.
