# Applies the mod's patches to the bun bundle of beautiful-mermaid 1.1.3's
# ASCII renderer, in place: python3 -I patch-mermaid-ascii.py <bundle.js>
# Every patch is an exact replacement that must match once, so a changed
# upstream fails loudly here instead of patching the wrong code. See VENDOR.md.
import sys

path = sys.argv[1]
s = open(path, encoding='utf-8').read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


# 0. Name the bundled modules by package, not by the build machine's path.
s = s.replace('// ../npm/node_modules/beautiful-mermaid/', '// beautiful-mermaid@1.1.3/')
# 1. Levels past the fixed 100-slot array read undefined -> NaN coordinates.
sub("          highestPosition = highestPositionPerLevel[childLevel];",
    "          highestPosition = highestPositionPerLevel[childLevel] ?? 0; /* inline-math patch 1 */")
sub("      const requested = dir === \"LR\" ? { x: subgraphLevel, y: highestPositionPerLevel[subgraphLevel] } : { x: highestPositionPerLevel[subgraphLevel], y: subgraphLevel };",
    "      const requested = dir === \"LR\" ? { x: subgraphLevel, y: highestPositionPerLevel[subgraphLevel] ?? 0 } : { x: highestPositionPerLevel[subgraphLevel] ?? 0, y: subgraphLevel }; /* inline-math patch 1 */")
# 2. A* searched an unbounded quarter-plane: an unreachable target never ended.
sub("""function getPath(grid, from, to) {
  const pq = new MinHeap;""", """var gridExtents = new WeakMap;
function getPath(grid, from, to) {
  /* inline-math patch 2: refuse non-finite ends, search only near the drawing */
  if (!Number.isFinite(from.x + from.y + to.x + to.y)) {
    throw new Error("diagram layout failed");
  }
  let extent = gridExtents.get(grid);
  if (!extent || extent.size !== grid.size) {
    extent = { size: grid.size, x: 0, y: 0 };
    for (const key of grid.keys()) {
      const comma = key.indexOf(",");
      extent.x = Math.max(extent.x, Number(key.slice(0, comma)));
      extent.y = Math.max(extent.y, Number(key.slice(comma + 1)));
    }
    gridExtents.set(grid, extent);
  }
  const limitX = Math.max(from.x, to.x, extent.x) + 16;
  const limitY = Math.max(from.y, to.y, extent.y) + 16;
  const pq = new MinHeap;""")
sub("""      const next = { x: current.x + dir.x, y: current.y + dir.y };
      if (!isFreeInGrid(grid, next) && !gridCoordEquals(next, to)) {""", """      const next = { x: current.x + dir.x, y: current.y + dir.y };
      if (next.x > limitX || next.y > limitY) {
        continue;
      }
      if (!isFreeInGrid(grid, next) && !gridCoordEquals(next, to)) {""")
# 3. Speed: a canvas column is built with fill, not one push per cell.
sub("""  for (let i = 0;i <= x; i++) {
    const col = [];
    for (let j = 0;j <= y; j++) {
      col.push(" ");
    }
    canvas.push(col);
  }""", """  for (let i = 0;i <= x; i++) {
    canvas.push(new Array(y + 1).fill(" ")); /* inline-math patch 3 */
  }""")
# 4. Speed: merge into the base when the overlays fit it (every caller replaces
#    the base with the result), and set roles in the same pass.
end = s.index("\n}\n", s.index("function mergeCanvases(base, offset, useAscii, ...overlays) {")) + 3
old_merge = s[s.index("function mergeCanvases(base, offset, useAscii, ...overlays) {"):end]
new_merge = """function mergeCanvases(base, offset, useAscii, ...overlays) {
  return mergeCanvasesWithRoles(base, offset, useAscii, overlays, null, null);
}
/* inline-math patch 4: in place when the overlays fit, roles in the same pass */
function mergeCanvasesWithRoles(base, offset, useAscii, overlays, roleCanvas, role) {
  const [baseX, baseY] = getCanvasSize(base);
  let maxX = baseX;
  let maxY = baseY;
  for (const overlay of overlays) {
    const [oX, oY] = getCanvasSize(overlay);
    maxX = Math.max(maxX, oX + offset.x);
    maxY = Math.max(maxY, oY + offset.y);
  }
  let merged = base;
  if (maxX !== baseX || maxY !== baseY) {
    merged = mkCanvas(maxX, maxY);
    for (let x = 0;x < base.length; x++) {
      const from = base[x];
      const to = merged[x];
      for (let y = 0;y < from.length; y++) {
        to[y] = from[y];
      }
    }
  }
  for (const overlay of overlays) {
    const height = overlay[0]?.length ?? 0;
    for (let x = 0;x < overlay.length; x++) {
      const column = overlay[x];
      for (let y = 0;y < height; y++) {
        const c = column[y];
        if (c !== " ") {
          const mx = x + offset.x;
          const my = y + offset.y;
          const current = merged[mx][my];
          if (!useAscii && isJunctionChar(c) && isJunctionChar(current)) {
            merged[mx][my] = mergeJunctions(current, c);
          } else if (isAlphanumeric(current) && isAlphanumeric(c)) {} else {
            merged[mx][my] = c;
          }
          if (roleCanvas && c && mx >= 0 && my >= 0) {
            setRole(roleCanvas, mx, my, role);
          }
        }
      }
    }
  }
  return merged;
}
"""
sub(old_merge, new_merge)
for name, role in [("lineCanvases", "line"), ("cornerCanvases", "corner"), ("junctionCanvases", "junction"), ("arrowHeadEndCanvases", "arrow"), ("boxStartCanvases", "junction"), ("arrowHeadStartCanvases", "arrow"), ("labelCanvases", "text")]:
    sub(f"""  graph.canvas = mergeCanvases(graph.canvas, zero, useAscii, ...{name});
  fillRolesFromCanvases(graph.roleCanvas, {name}, zero, "{role}");""",
        f"""  graph.canvas = mergeCanvasesWithRoles(graph.canvas, zero, useAscii, {name}, graph.roleCanvas, "{role}"); /* inline-math patch 4 */""")
# 5. Speed: an edge with nothing to draw in a layer gets a 1x1 blank overlay,
#    not a copy of the whole canvas (the overlays are only merged, never kept).
sub("""  } else {
    arrowHeadEndCanvas = copyCanvas(graph.canvas);
  }""", """  } else {
    arrowHeadEndCanvas = mkCanvas(0, 0); /* inline-math patch 5 */
  }""")
sub("""  } else {
    arrowHeadStartCanvas = copyCanvas(graph.canvas);
  }""", """  } else {
    arrowHeadStartCanvas = mkCanvas(0, 0); /* inline-math patch 5 */
  }""")
sub("""function drawArrowLabel(graph, edge) {
  const canvas = copyCanvas(graph.canvas);
  if (edge.text.length === 0)
    return canvas;""", """function drawArrowLabel(graph, edge) {
  if (edge.text.length === 0)
    return mkCanvas(0, 0); /* inline-math patch 5 */
  const canvas = copyCanvas(graph.canvas);""")
open(path, 'w', encoding='utf-8').write(s)
