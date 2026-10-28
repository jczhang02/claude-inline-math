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
# 6. Bug: a back edge (its target in an earlier layer) and a self loop were
#    routed like a forward edge: the straight "alternative" ran over the
#    forward edge, the detour columns and rows were too thin to hold a leg
#    (box connector inside the box, arrowhead on the corner, in LR the whole
#    loop gone), the label went on the last leg, and a self loop was drawn
#    inside its box. A back edge now leaves the side of its source that no
#    forward edge uses (right in TD, bottom in LR) and enters the same side of
#    its target; a self loop is a small loop on that side of its box.
sub("function getPath(grid, from, to) {", "function getPath(grid, from, to, loopGraph) {")
sub("""      const newCost = currentCost + 1;
      const nextKey = gridKey(next);""", """      const nextKey = gridKey(next);
      if (loopGraph && next.x % 4 !== 3 && next.y % 4 !== 3 && !((loopGraph.columnWidth.get(next.x) ?? 0) > 0 && (loopGraph.rowHeight.get(next.y) ?? 0) > 0)) {
        continue; /* inline-math patch 6: a loop keeps to the gaps and to columns and rows that have a size */
      }
      const newCost = currentCost + 1 + (loopGraph && loopGraph.claimed.has(nextKey) ? 20 : 0); /* inline-math patch 6: a cell of another edge costs more */""")
sub("""function determinePath(graph, edge) {
  const sourceSg = getNodeSubgraph(graph, edge.from);
  const targetSg = getNodeSubgraph(graph, edge.to);
  const effectiveDir = sourceSg && sourceSg === targetSg && sourceSg.direction ? sourceSg.direction : graph.config.graphDirection;
""", """/* inline-math patch 6: back edges and self loops */
var layoutReportValue = null;
function layoutReport() {
  return layoutReportValue;
}
function edgeLayoutDirection(graph, edge) {
  const sourceSg = getNodeSubgraph(graph, edge.from);
  const targetSg = getNodeSubgraph(graph, edge.to);
  return sourceSg && sourceSg === targetSg && sourceSg.direction ? sourceSg.direction : graph.config.graphDirection;
}
function isBackEdge(graph, edge) {
  if (edge.from === edge.to || !edge.from.gridCoord || !edge.to.gridCoord)
    return false;
  const from = edge.from.gridCoord;
  const to = edge.to.gridCoord;
  return edgeLayoutDirection(graph, edge) === "LR" ? to.x < from.x : to.y < from.y;
}
function pathCells(path) {
  const cells = [];
  for (let i = 0;i < path.length; i++) {
    const a = path[i];
    cells.push(a);
    const b = path[i + 1];
    if (b && (a.x === b.x) !== (a.y === b.y)) {
      const stepX = Math.sign(b.x - a.x);
      const stepY = Math.sign(b.y - a.y);
      for (let c = { x: a.x + stepX, y: a.y + stepY };c.x !== b.x || c.y !== b.y; c = { x: c.x + stepX, y: c.y + stepY }) {
        cells.push(c);
      }
    }
  }
  return cells;
}
function claimPath(graph, path) {
  for (const c of pathCells(path)) {
    graph.claimed.set(gridKey(c), true);
  }
}
function routeBackEdge(graph, edge, dir) {
  const side = dir === "LR" ? Down : Right;
  const outward = { x: side.x - 1, y: side.y - 1 };
  const from = gridCoordDirection(edge.from.gridCoord, side);
  const to = gridCoordDirection(edge.to.gridCoord, side);
  const first = { x: from.x + outward.x, y: from.y + outward.y };
  const last = { x: to.x + outward.x, y: to.y + outward.y };
  edge.isBack = true;
  edge.startDir = side;
  edge.endDir = side;
  const leaves = `out ${edge.from.name}`;
  const enters = `in ${edge.to.name}`;
  const usable = (c, tag) => !graph.claimed.has(gridKey(c)) || graph.claimedEnds.get(gridKey(c)) === tag;
  if (!usable(first, leaves) || !usable(last, enters)) {
    edge.path = [];
    return;
  }
  const inner = getPath(graph.grid, first, last, graph);
  if (inner === null) {
    edge.path = [];
    return;
  }
  edge.path = mergePath([from, ...inner, to]);
  for (const c of edge.path.slice(1, -1)) {
    if (c.x % 4 === 3) {
      graph.columnWidth.set(c.x, Math.max(graph.columnWidth.get(c.x) ?? 0, 3));
    }
    if (c.y % 4 === 3) {
      graph.rowHeight.set(c.y, Math.max(graph.rowHeight.get(c.y) ?? 0, 3));
    }
  }
  claimPath(graph, edge.path);
  graph.claimedEnds.set(gridKey(first), leaves);
  graph.claimedEnds.set(gridKey(last), enters);
}
function reserveSelfLoop(graph, edge) {
  const gc = edge.from.gridCoord;
  edge.path = [];
  edge.isSelfLoop = true;
  edge.startDir = Middle;
  edge.endDir = Middle;
  const width = edge.text.length > 0 ? maxLineWidth(edge.text) : 0;
  const lines = edge.text.length > 0 ? lineCount(edge.text) : 0;
  if (edgeLayoutDirection(graph, edge) === "LR") {
    graph.rowHeight.set(gc.y + 3, Math.max(graph.rowHeight.get(gc.y + 3) ?? 0, 3 + lines));
  } else {
    graph.columnWidth.set(gc.x + 3, Math.max(graph.columnWidth.get(gc.x + 3) ?? 0, width > 0 ? width + 4 : 3));
  }
}
function routeLoops(graph, loops) {
  graph.claimed = new Map;
  graph.claimedEnds = new Map;
  for (const edge of graph.edges) {
    claimPath(graph, edge.path);
  }
  const span = (e) => Math.abs(e.from.gridCoord.x - e.to.gridCoord.x) + Math.abs(e.from.gridCoord.y - e.to.gridCoord.y);
  loops.sort((a, b) => span(a) - span(b));
  const touching = (node) => loops.filter((e) => e.from === node || e.to === node);
  const selfLooped = new Set;
  for (const edge of loops) {
    if (edge.from === edge.to) {
      const node = edge.from;
      const side = edgeLayoutDirection(graph, edge) === "LR" ? Down : Right;
      const crowded = selfLooped.has(node) || touching(node).some((e) => e.from !== e.to) || graph.edges.some((e) => !e.isBack && e.path.length > 0 && (e.from === node && dirEquals(e.startDir, side) || e.to === node && dirEquals(e.endDir, side)));
      if (!crowded) {
        selfLooped.add(node);
        reserveSelfLoop(graph, edge);
      }
    } else if (![...touching(edge.from), ...touching(edge.to)].some((e) => e.from === e.to)) {
      determinePath(graph, edge);
    }
    increaseGridSizeForPath(graph, edge.path);
    determineLabelLine(graph, edge);
  }
}
function dropOverlaps(graph) {
  const seen = new Set;
  for (const edge of graph.edges) {
    if (edge.path.length < 2 || edge.bundle)
      continue;
    const key = pathCells(edge.path).map(gridKey).sort().join(";");
    if (seen.has(key)) {
      edge.path = [];
    } else {
      seen.add(key);
    }
  }
}
function buildLayoutReport(graph) {
  const heads = new Set;
  const labels = [];
  let dropped = 0;
  for (const edge of graph.edges) {
    if (edge.path.length === 0 && !edge.isSelfLoop) {
      dropped++;
      continue;
    }
    if (edge.hasArrowEnd) {
      heads.add(edge.isSelfLoop ? `self ${edge.from.name}` : `end ${gridKey(edge.path[edge.path.length - 1])}`);
    }
    if (edge.hasArrowStart) {
      heads.add(edge.isSelfLoop ? `self ${edge.from.name}` : `start ${gridKey(edge.path[0])}`);
    }
    if (edge.text.length > 0) {
      labels.push(edge.text);
    }
  }
  return { dropped, heads: heads.size, labels };
}
function determinePath(graph, edge) {
  const effectiveDir = edgeLayoutDirection(graph, edge);
  if (isBackEdge(graph, edge)) {
    routeBackEdge(graph, edge, effectiveDir);
    return;
  }
""")

sub("""    determinePath(graph, edge);
    increaseGridSizeForPath(graph, edge.path);
    determineLabelLine(graph, edge);
  }
  for (const node of graph.nodes) {
    node.drawingCoord""", """    if (edge.from === edge.to || isBackEdge(graph, edge)) {
      loops.push(edge); /* inline-math patch 6: routed after the forward edges */
      continue;
    }
    determinePath(graph, edge);
    increaseGridSizeForPath(graph, edge.path);
    determineLabelLine(graph, edge);
  }
  if (loops.length > 0) {
    routeLoops(graph, loops);
  }
  dropOverlaps(graph);
  for (const node of graph.nodes) {
    node.drawingCoord""")
sub("""  for (const edge of graph.edges) {
    if (edge.bundle && edge.path.length > 0) {
      increaseGridSizeForPath(graph, edge.path);""", """  const loops = [];
  graph.loopNodes = new Set;
  for (const edge of graph.edges) {
    if (edge.from === edge.to || isBackEdge(graph, edge)) {
      graph.loopNodes.add(edge.from); /* inline-math patch 6: loops need the side of their nodes */
      graph.loopNodes.add(edge.to);
    }
  }
  for (const edge of graph.edges) {
    if (edge.bundle && edge.path.length > 0) {
      increaseGridSizeForPath(graph, edge.path);""")
sub("""  for (const edge of graph.edges) {
    if (edge.from === edge.to)
      continue;
    const existing = edgesByTarget.get(edge.to) ?? [];""", """  for (const edge of graph.edges) {
    if (edge.from === edge.to || isBackEdge(graph, edge))
      continue;
    const existing = edgesByTarget.get(edge.to) ?? [];""")
sub("""  for (const edge of graph.edges) {
    if (edge.from === edge.to)
      continue;
    if (bundledEdges.has(edge))
      continue;""", """  for (const edge of graph.edges) {
    if (edge.from === edge.to || isBackEdge(graph, edge))
      continue;
    if (bundledEdges.has(edge))
      continue;""")
sub("""function determineLabelLine(graph, edge) {
  if (edge.text.length === 0)
    return;""", """function determineLabelLine(graph, edge) {
  if (edge.text.length === 0 || edge.path.length < 2)
    return;""")
sub("""  let largestLine;
  if (suitableSegments.length > 0) {""", """  let largestLine;
  if (edge.isBack || edge.legLabel) {
    /* inline-math patch 6: a back edge's label goes on its long middle leg */
    const legs = segments.length >= 3 ? segments.slice(1, -1) : segments;
    const length = (s) => Math.abs(s.line[0].x - s.line[1].x) + Math.abs(s.line[0].y - s.line[1].y);
    /* A run along the gaps is labelled over the widest column between its ends that is a gap or a box's
       inside; a box's border column is not widened (its edges would come off the box). */
    const columns = (s) => {
      const left = Math.min(s.line[0].x, s.line[1].x);
      const right = Math.max(s.line[0].x, s.line[1].x);
      return s.line[0].y === s.line[1].y ? Array.from({ length: Math.max(0, right - left - 1) }, (_, i) => left + 1 + i).filter((x) => x % 2 === 1) : [s.line[0].x];
    };
    const fitting = legs.filter((s) => columns(s).length > 0);
    const leg = (fitting.length > 0 ? fitting : legs).reduce((best, s) => length(s) > length(best) ? s : best);
    largestLine = leg.line;
    if (largestLine[0].y === largestLine[1].y) {
      let column = columns(leg)[0] ?? Math.min(largestLine[0].x, largestLine[1].x) + 1;
      for (const x of columns(leg)) {
        if ((graph.columnWidth.get(x) ?? 0) > (graph.columnWidth.get(column) ?? 0)) {
          column = x;
        }
      }
      largestLine = [{ x: column, y: largestLine[0].y }, { x: column, y: largestLine[0].y }];
    }
  } else if (suitableSegments.length > 0) {""")
sub("""function drawArrow(graph, edge) {
  if (edge.path.length === 0) {""", """function drawSelfLoop(graph, edge) {
  const node = edge.from;
  const none = () => mkCanvas(0, 0);
  const width = node.drawing.length;
  const height = node.drawing[0].length;
  if (width < 5 || height < 5) {
    layoutReportValue.dropped++;
    return [none(), none(), none(), none(), none(), none()];
  }
  const useAscii = graph.config.useAscii;
  const chars = LINE_CHARS[edge.style];
  const hChar = useAscii ? chars.h.ascii : chars.h.unicode;
  const vChar = useAscii ? chars.v.ascii : chars.v.unicode;
  const [lineC, boxStartC, arrowC, cornerC, labelC] = [0, 1, 2, 3, 4].map(() => copyCanvas(graph.canvas));
  const put = (canvas, x, y, c) => {
    if (canvas[x] && y >= 0 && y < canvas[x].length)
      canvas[x][y] = c;
  };
  const x0 = node.drawingCoord.x;
  const y0 = node.drawingCoord.y;
  const lines = splitLines(edge.text);
  if (edgeLayoutDirection(graph, edge) === "LR") {
    const bottom = y0 + height - 1;
    const middle = x0 + Math.floor((width - 1) / 2);
    put(boxStartC, middle - 1, bottom, useAscii ? " " : "┬");
    put(lineC, middle - 1, bottom + 1, vChar);
    put(lineC, middle, bottom + 2, hChar);
    put(cornerC, middle - 1, bottom + 2, useAscii ? "+" : "└");
    put(cornerC, middle + 1, bottom + 2, useAscii ? "+" : "┘");
    put(arrowC, middle + 1, bottom + 1, useAscii ? "^" : "▲");
    if (edge.text.length > 0) {
      lines.forEach((text, i) => drawText(labelC, { x: middle - Math.floor(text.length / 2), y: bottom + 3 + i }, text));
    }
  } else {
    const right = x0 + width - 1;
    const top = y0 + 1;
    const bottom = y0 + height - 2;
    put(boxStartC, right, top, useAscii ? " " : "├");
    put(lineC, right + 1, top, hChar);
    put(cornerC, right + 2, top, useAscii ? "+" : "┐");
    for (let y = top + 1;y < bottom; y++) {
      put(lineC, right + 2, y, vChar);
    }
    put(cornerC, right + 2, bottom, useAscii ? "+" : "┘");
    put(arrowC, right + 1, bottom, useAscii ? "<" : "◄");
    if (edge.text.length > 0) {
      const first = y0 + Math.floor(height / 2) - Math.floor((lines.length - 1) / 2);
      lines.forEach((text, i) => drawText(labelC, { x: right + 4, y: first + i }, text));
    }
  }
  return [lineC, boxStartC, arrowC, none(), cornerC, labelC];
}
function drawArrow(graph, edge) {
  if (edge.isSelfLoop) {
    return drawSelfLoop(graph, edge);
  }
  if (edge.path.length === 0) {""")
sub("""function renderMermaidASCII(text, options = {}) {
""", """function renderMermaidASCII(text, options = {}) {
  layoutReportValue = null; /* inline-math patch 6 */
""")
sub("""      createMapping(graph);
      drawGraph(graph);""", """      createMapping(graph);
      layoutReportValue = buildLayoutReport(graph); /* inline-math patch 6 */
      drawGraph(graph);""")
sub("""export {
  renderMermaidASCII
};""", """export {
  renderMermaidASCII,
  layoutReport
};""")
sub("""  const subgraphPadding = 2;
  const subgraphLabelSpace = 2;
""", """  for (const edge of graph.edges) {
    const bounds = sg.nodes.includes(edge.from) && sg.nodes.includes(edge.to) ? loopBounds(graph, edge) : null;
    if (bounds) {
      minX = Math.min(minX, bounds.minX); /* inline-math patch 6: a loop inside a subgraph stays in its box */
      minY = Math.min(minY, bounds.minY);
      maxX = Math.max(maxX, bounds.maxX);
      maxY = Math.max(maxY, bounds.maxY);
    }
  }
  const subgraphPadding = 2;
  const subgraphLabelSpace = 2;
""")
sub("""  calculateSubgraphBoundingBoxes(graph);
  offsetDrawingForSubgraphs(graph);
}""", """  calculateSubgraphBoundingBoxes(graph);
  dropLoopsAcrossSubgraphs(graph); /* inline-math patch 6 */
  offsetDrawingForSubgraphs(graph);
}""")
sub("""function dropOverlaps(graph) {""", """function loopBounds(graph, edge) {
  const labelWidth = edge.text.length > 0 ? maxLineWidth(edge.text) : 0;
  const labelLines = edge.text.length > 0 ? lineCount(edge.text) : 0;
  if (edge.isSelfLoop) {
    const x0 = edge.from.drawingCoord.x;
    const y0 = edge.from.drawingCoord.y;
    const width = edge.from.drawing.length;
    const height = edge.from.drawing[0].length;
    if (edgeLayoutDirection(graph, edge) === "LR") {
      const middle = x0 + Math.floor((width - 1) / 2);
      return { minX: Math.min(x0, middle - Math.floor(labelWidth / 2)), maxX: Math.max(x0 + width - 1, middle + Math.ceil(labelWidth / 2)), minY: y0, maxY: y0 + height + 1 + labelLines };
    }
    return { minX: x0, maxX: x0 + width + 1 + (labelWidth > 0 ? labelWidth + 1 : 0), minY: y0, maxY: y0 + height - 1 };
  }
  if (!edge.isBack || edge.path.length < 2)
    return null;
  const points = lineToDrawing(graph, edge.path);
  const half = Math.ceil(labelWidth / 2);
  return {
    minX: Math.min(...points.map((p) => p.x)) - half,
    maxX: Math.max(...points.map((p) => p.x)) + half,
    minY: Math.min(...points.map((p) => p.y)) - labelLines,
    maxY: Math.max(...points.map((p) => p.y)) + labelLines
  };
}
function dropLoopsAcrossSubgraphs(graph) {
  for (const edge of graph.edges) {
    if (!edge.isBack && !edge.isSelfLoop)
      continue;
    const parts = [];
    if (edge.isBack && edge.path.length >= 2) {
      const points = lineToDrawing(graph, edge.path);
      for (let i = 1;i < points.length; i++) {
        parts.push({ minX: Math.min(points[i - 1].x, points[i].x), maxX: Math.max(points[i - 1].x, points[i].x), minY: Math.min(points[i - 1].y, points[i].y), maxY: Math.max(points[i - 1].y, points[i].y) });
      }
    } else if (edge.isSelfLoop) {
      parts.push(loopBounds(graph, edge));
    }
    const crosses = graph.subgraphs.some((sg) => sg.nodes.length > 0 && !(sg.nodes.includes(edge.from) && sg.nodes.includes(edge.to)) && parts.some((r) => r.minX <= sg.maxX && r.maxX >= sg.minX && r.minY <= sg.maxY && r.maxY >= sg.minY));
    if (crosses) {
      edge.path = [];
      edge.isBack = false;
      edge.isSelfLoop = false;
    }
  }
}
function dropOverlaps(graph) {""")
sub("""  drawTextOnLine(canvas, drawingLine, edge.text, isUpwardEdge);""", """  if (edge.isBack || edge.legLabel) {
    isUpwardEdge = undefined; /* inline-math patch 6: a loop's label is centred on its leg */
  }
  drawTextOnLine(canvas, drawingLine, edge.text, isUpwardEdge);""")
sub("""    if (preferredPath.length <= alternativePath.length) {
      edge.startDir = preferredDir;""", """    /* inline-math patch 6: a forward edge keeps off the side a loop of its node uses */
    const side = effectiveDir === "LR" ? Down : Right;
    const clashes = (start, end) => graph.loopNodes?.has(edge.from) && dirEquals(start, side) || graph.loopNodes?.has(edge.to) && dirEquals(end, side);
    const preferredClashes = clashes(preferredDir, preferredOppositeDir);
    const alternativeClashes = clashes(alternativeDir, alternativeOppositeDir);
    if (preferredClashes === alternativeClashes ? preferredPath.length <= alternativePath.length : alternativeClashes) {
      edge.startDir = preferredDir;""")
sub("""    } else {
      edge.startDir = alternativeDir;
      edge.endDir = alternativeOppositeDir;
      edge.path = alternativePath;
    }
    return;""", """    } else {
      edge.startDir = alternativeDir;
      edge.endDir = alternativeOppositeDir;
      edge.path = alternativePath;
      edge.legLabel = preferredClashes && !alternativeClashes; /* inline-math patch 6 */
    }
    return;""")
# 7. Bug: the label of an edge widened the middle column of its line; when that
#    was the border column of a box (a label on an edge from the bottom of a
#    diamond to a node at the lower right), the border grew and the edge that
#    leaves the box's side started one cell off it (`}  ├─yes─►`). A border
#    column (grid x 0 or 2 of a node's three) is not widened; the gap beside it is.
sub("""  const current = graph.columnWidth.get(middleX) ?? 0;
  graph.columnWidth.set(middleX, Math.max(current, lenLabel + 2));
  edge.labelLine = [largestLine[0], largestLine[1]];""", """  const widenX = middleX % 4 === 2 ? middleX + 1 : middleX % 4 === 0 && middleX > 0 ? middleX - 1 : middleX; /* inline-math patch 7 */
  const current = graph.columnWidth.get(widenX) ?? 0;
  graph.columnWidth.set(widenX, Math.max(current, lenLabel + 2));
  edge.labelLine = [largestLine[0], largestLine[1]];""")
# 8. Bug: the first leg of an edge that is a single cell long (a box's border,
#    then a gap row of height 1 with the bend in it) drew no cells, and the
#    connector (`┬`, `├`) was put one cell before the border, inside the box.
#    Such a leg now reports the cell after the border, as a longer one does.
sub("""    if (segment.length === 0)
      segment.push(prevDC);
    linesDrawn.push(segment);""", """    if (segment.length === 0)
      segment.push(linesDrawn.length === 0 && (prevDC.x === nextDC.x || prevDC.y === nextDC.y) ? { x: prevDC.x + Math.sign(nextDC.x - prevDC.x), y: prevDC.y + Math.sign(nextDC.y - prevDC.y) } : prevDC); /* inline-math patch 8 */
    linesDrawn.push(segment);""")
# 9. A labelled edge that bends in a gap row of height 1 (the bend, the label
#    and the arrowhead all in one row, under a diamond) has no room: the gap
#    row is made 3 high, as for a loop.
sub("""    determinePath(graph, edge);
    increaseGridSizeForPath(graph, edge.path);
    determineLabelLine(graph, edge);
  }
  if (loops.length > 0) {""", """    determinePath(graph, edge);
    if (edge.text.length > 0) {
      for (const c of edge.path.slice(1, -1)) {
        if (c.y % 4 === 3) {
          graph.rowHeight.set(c.y, Math.max(graph.rowHeight.get(c.y) ?? 0, 3)); /* inline-math patch 9 */
        }
      }
    }
    increaseGridSizeForPath(graph, edge.path);
    determineLabelLine(graph, edge);
  }
  if (loops.length > 0) {""")
open(path, 'w', encoding='utf-8').write(s)
