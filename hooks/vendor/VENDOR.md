# Vendored code

## mermaid-ascii.js

- Package: [beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid) **1.1.3** (npm), MIT License, Copyright (c) 2026 Craft Docs. The license text is in `LICENSE` beside this file.
- npm integrity: `sha512-TItrtrAyHp1vwFfFVYauWGrquouk/6SS21Aq3RsxindSYZODcN4xYrPZD6BiZRU+o5mKJzDPz9MUSMvELdylyg==`
- What is in it: only the ASCII/Unicode renderer, `src/ascii/index.ts` and what it imports (35 modules: the flowchart, state, sequence, class, ER and xychart parsers and text renderers). The SVG renderer and its dependencies (`elkjs`, `entities`) are not in the bundle. No Node or DOM API is used on the path the mod calls: `detectColorMode`, the one function that reads `globalThis.process` or `document`, runs only when `colorMode` is not given, and `hooks/mermaid.ts` always gives it.
- Exports used: `renderMermaidASCII(text, options)`, and `layoutReport()` (added by patch 6: what the last flowchart or state render dropped, drew and labelled). Types: `mermaid-ascii.d.ts` (written by hand, only what the mod uses).
- sha256 of the shipped file: `fce92b31e20d3ab1731f0b5f1a29813c69bf9597513d7f0271c4e482c394c4de`
- Supported diagram types in 1.1.3: `flowchart`/`graph`, `stateDiagram`/`stateDiagram-v2`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `xychart-beta`. Everything else (pie, gantt, mindmap, ...) is refused by `hooks/mermaid.ts` before the library sees it. The xychart plot is a fixed 60 columns wide in this version (about 65-70 columns with the axis), so a narrower terminal shows the source with a reason.

### Patches

`patch-mermaid-ascii.py` applies these to the bundle (each marked `inline-math patch N` in the code; each is an exact replacement that must match once). Patch 6 was checked by applying it alone to the bundle with patches 0-5 and comparing with the shipped file; the whole script was not run again from npm.

1. Bug: node placement keeps one position per level in a fixed array of 100 slots; a flowchart deeper than 24 levels read `undefined`, made `NaN` coordinates, and the edge search then never ended (the page hung until `Map maximum size exceeded`). A missing slot now reads 0.
2. Bug: the A* edge router searched an unbounded quarter-plane, so an unreachable target looped until memory ran out. It now refuses non-finite ends and searches only up to 16 grid cells past the drawing. The drawing's extent is cached per grid and recomputed when the grid's size changes; the grid is append-only (only `set` is ever called on it), so the size is a sound staleness check.
3. Speed: a blank canvas column is made with `fill`, not one `push` per cell.
4. Speed: `mergeCanvases` merges into the base canvas when the overlays fit it (every caller replaces the base with the result), and the flowchart drawer sets character roles in the same pass instead of a second scan of every overlay.
5. Speed: an edge with no arrowhead at an end, or with no label, gets a 1x1 blank overlay instead of a copy of the whole canvas.

6. Bug: loops. The library routed a back edge (target in an earlier layer) like a forward edge. The "alternative" path of a back edge is the straight line the forward edge already uses, so `A --> B` with `B --> A` drew one edge, and with labels both stacked on it; the detour columns and rows it did use were 1 cell wide (0 in LR), so the box connector landed inside the box (`│ C├│┘`), the arrowhead on the corner, and in LR the loop vanished; the label went on the short last leg (beside the box, widening the box's border column); a self loop was drawn inside its box; a back edge could be bundled with forward edges. Now:
   - A back edge leaves the right side of its source (bottom in LR) and enters the same side of its target. Its route is searched only in the gaps between nodes (and in columns and rows that already have a size), the gaps it turns in are made 3 wide (or tall), and cells of other edges cost more, so loops go around each other. Back edges are routed after all forward edges, shortest first; a loop whose start or end cell is taken by another role (a retry that leaves a node that another retry enters) is dropped.
   - A forward edge that would use the side a loop of its node needs takes its other route (e.g. `B --> D` leaves the bottom of B, not the right side), and its label goes on the long middle leg.
   - The label of a back edge is centred on its longest middle leg, over a gap or the inside of a box, never a box's border column.
   - A self loop is not routed: it is drawn on the right side of the box (below it in LR), with its label beside (under) it.
   - A loop with one end in a subgraph and the other outside is dropped; a loop with both ends in one subgraph makes the subgraph's box bigger.
   - Two edges that end up on exactly the same cells are drawn once; the other is dropped.
   - `layoutReport()` says how many edges were dropped, how many distinct arrowheads were drawn and which labels; `hooks/mermaid.ts` refuses a drawing that does not show them all.
7. Bug: an edge label widened the middle column of its line; when that was a box's border column (the label of an edge from the bottom of a diamond to a node at its lower right), the border grew and the edge leaving the box's side started one cell off it (`│ formula? │ ├─yes─►`). A border column is not widened; the gap beside it is.

8. Bug: an edge whose first leg is one cell long (a box's border, then a gap row of height 1 with the bend in it) drew no cells there, and its connector (`┬`, `├`) was put one cell before the border, inside the box (`│    ┬   │` inside a diamond). The leg now reports the cell after the border, as a longer one does.
9. A labelled edge that bends in a gap row of height 1 (bend, label and arrowhead in one row, under a diamond) gets a gap row 3 high, as a loop does.

Patches 3-5 do not change output: on a corpus of 72 renders (flowcharts in all directions with subgraphs, shapes, labels, bundles; sequence, state, class, ER), the patched and unpatched bundles give byte-identical results, colour codes included. Together they take a 30-node flowchart from about 15 ms to about 6 ms (node 26, after warmup).

Patch 6 changes output only where the layout has a back edge or a self loop. On 400 random flowcharts without a cycle (all four directions, five shapes, labels), 349 give the same text before and after; the other 51 are the ones where the layout puts an edge into an earlier layer (the library places a node one layer below the first parent it finds, so an edge `C --> D` can go up when `A --> D` is also there), in the ones looked at, the unpatched bundle drew them wrongly (an edge over another edge, an arrowhead on the wrong box).

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

To update: change the version in the steps above (and the comment rewrite at the top of the patch script), read the upstream changes to `src/ascii/` (`grid.ts` placement and `pathfinder.ts` especially, in case patches 1-2 were fixed upstream), rebuild, and run the patch script. A patch that no longer matches stops the script; fix or drop it. Then check that the speed patches still leave the output unchanged (render a corpus without back edges with the patched and unpatched bundles and compare, colours included), and run `claude plugin test` on the mod: `hooks/mermaid.test.ts` imports the bundle through `hooks/mermaid.ts`, so it also shows the bundle loads in the hooks runtime.

### Known layout limits (not patched)

Upstream 1.1.3 has layout bugs that lose information rather than crash
(found in review; the patches above do not touch the layout of forward edges):

- An edge into a node placed on the drawing's last row can leave the
  canvas and be cut off (`D --> B` in `A-->B{Valid?}; B-->|No|D; D-->B`
  before patch 6).
- Edges between nodes of the same layer (`B --> C` with `C --> B`, or one
  edge twice) run on the same cells; patch 6 drops the second, so the mod
  refuses the drawing.
- A cycle into a subgraph throws a TypeError (no node is left to start from).
  Not patched: the mod shows the source with "diagram renderer failed on this
  diagram".
- `flowchart RL` is drawn left to right.

`hooks/mermaid.ts` checks each flowchart and state diagram it draws (no edge
dropped, an arrowhead for every one `layoutReport()` counts, every edge label
once, no line running off the first or last row), tries its other layouts,
and otherwise shows the source with the reason, so a wrong diagram is never
shown as right.

### Credits

The wide-character trick in `hooks/mermaid.ts` (a filler character after each two-cell grapheme so the library, which counts one cell per UTF-16 unit, sizes boxes right, removed after drawing), the LR-to-TD retry when a chart is too wide, the dropping of inline HTML tags and of `-->  |label|` spacing come from [prismantis](https://github.com/NahumLitvin/prismantis) (MIT, `hooks/mermaid.tsx`, which vendors its own build of beautiful-mermaid as `hooks/vendor/mermaid-text.js`). No code from it is copied; this mod has no dependency on it.
