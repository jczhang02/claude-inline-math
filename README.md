# inline-math

A Claude Code mod that draws the LaTeX in Claude's replies as typeset formulas, right in the terminal, and draws ` ```mermaid ` code blocks as diagrams. Each formula is a picture placed inline with the text around it and aligned to the text baseline, so `$e^{i\pi} + 1 = 0$` reads like a line in a paper, not like source code. Display math (`$$...$$`) is centred on its own lines. A mermaid flowchart becomes box-drawing art in the reply. Paragraphs with no formulas or diagrams are drawn by Claude Code's own Markdown renderer, unchanged.

The stored conversation is never changed: the model, `ctrl+o`, exports and copies still see the LaTeX and mermaid source.

## What it draws

- Inline math: `$...$` and `\(...\)`. A formula is kept on one text row when it fits at 80% of its size or more; a taller one (a fraction, a sum with limits) makes only its own line taller.
- Display math: `$$...$$` and `\[...\]`, centred; a formula wider than the reply is drawn smaller to fit.
- Lines are wrapped by the mod (East Asian wide characters count as two cells), so text, Chinese and formulas wrap together and line up with the rest of the reply.
- Bold, italic, strikethrough, inline code and links around formulas keep their styles. Lists with formulas keep their markers and hanging indents.
- Not math: code spans and code blocks, `\$`, and prices such as `$5 and $10` (the pandoc rule: an opening `$` must be followed by a non-space, a closing `$` must follow a non-space and must not be followed by a digit).
- Until a picture is ready, and when TeX cannot typeset a formula, a readable Unicode version stands in its place (`x²`, `α + β`, `√π`, `ℝ`).
- Commands a reply defines for itself: `\newcommand`, `\renewcommand`, `\providecommand`, `\def` (undelimited `#1`...`#9` only), `\let` and `\DeclareMathOperator` (also with `\global` before them) inside the reply's math apply to every formula of that reply, also in headings and tables (as Unicode there). A formula that holds only definitions draws as nothing. See [Reply macros](#reply-macros).
- Mermaid diagrams: a closed ` ```mermaid ` fence at the top level of a reply or in a list item. See [Diagrams](#diagrams).

What TeX can typeset, and what it cannot, is listed in [docs/compatibility.md](docs/compatibility.md): the KaTeX function list, MathJax extensions, commands that only make sense on the web (`\href`, `\url`, `\htmlClass`, `\class`, `\style`, `\require`, ...), which are drawn as their content or as nothing, and the best-effort cases (tikz-cd, Traditional Chinese, Korean).

## Requirements

- **A terminal with the kitty graphics protocol**: Ghostty or kitty. Elsewhere the pictures are replaced by their Unicode text. Diagrams are text and draw in any terminal.
- **tmux** (optional) with passthrough and focus events on, in `~/.config/tmux/tmux.conf` or `~/.tmux.conf`:

  ```tmux
  set -g allow-passthrough on
  set -g focus-events on
  ```

- **TeX Live** (or another TeX distribution) with `latex`, `dvipng` and the `preview`, `amsmath`, `amssymb` and `lmodern` packages; `mylatexformat` makes the precompiled format (below) build faster and is part of TeX Live's `latexextra`. On Arch: `pacman -S texlive-basic texlive-latex texlive-latexrecommended texlive-latexextra texlive-binextra texlive-fontsrecommended`; on Debian and Ubuntu: `apt install texlive-latex-extra dvipng`; on macOS: `brew install --cask mactex-no-gui`. For Chinese text inside a formula (`\text{面积}`), also `CJKutf8` with Chinese fonts: `texlive-langcjk` and `texlive-langchinese` on Arch, `texlive-lang-chinese` on Debian and Ubuntu.
- **python3** with **Pillow**: `pip install --user pillow`, or your system's `python-pillow` / `python3-pil`.
- Claude Code 2.1.311 or later.

Run `/inline-math status` after you install it: it says which of these it found.

## Install

For one session, name the folder on the command line:

```sh
claude --plugin-dir ~/Documents/dev-tools/claude-inline-math
```

For every session, add the folder to `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json` (use `:` between several folders, `;` on Windows):

```json
{ "env": { "CLAUDE_CODE_PLUGIN_DIRS": "/home/you/Documents/dev-tools/claude-inline-math" } }
```

Give the full path there: settings values do not expand `~`. Claude Code reads the mod from that folder, so `/reload-plugins` picks up your edits.

As an installed plugin: make the folder a marketplace by adding `.claude-plugin/marketplace.json` beside `plugin.json`,

```json
{ "name": "inline-math", "owner": { "name": "JC Zhang" }, "plugins": [{ "name": "inline-math", "source": "./" }] }
```

then install it once. The plugin is read from the folder itself, and `/reload-plugins` picks up your edits:

```sh
claude plugin marketplace add ~/Documents/dev-tools/claude-inline-math
claude plugin install inline-math@inline-math
```

## Commands

| Command | What it does |
| --- | --- |
| `/inline-math status` | Backend health (latex, dvipng, Pillow), the render daemon, the precompiled format, your packages and macros, cell size, formula colour, diagram colour, cache counts, recent TeX errors |
| `/inline-math on` / `off` | Draw formulas and diagrams, or leave replies as Claude Code draws them; kept across sessions |
| `/inline-math demo` | A sample reply: inline, tall inline, display, Chinese text, a list, bold with math, a reply macro and a flowchart |
| `/inline-math clear` | Forget the drawn formulas and typeset them again |

## Options

Set them in `/config` (each is a row there), or in `~/.claude/settings.json` under `pluginConfigs`:

```json
{
  "pluginConfigs": {
    "inline-math": {
      "options": {
        "ptPx": 3.6,
        "packages": "physics, tensor",
        "macros": "\\newcommand{\\R}{\\mathbb{R}}\n\\DeclareMathOperator{\\tr}{tr}"
      }
    }
  }
}
```

| Option | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Draw formulas and diagrams. `/inline-math on` or `off` overrides it. |
| `foreground` | `auto` | Formula colour. `auto` follows the desktop's light or dark scheme (Catppuccin Latte text `#4c4f69`, Mocha text `#cdd6f4`) and is looked up again every 5 seconds for 10 minutes after a formula or diagram was last drawn, then every minute; or `#rrggbb`. |
| `cell` | `auto` | One character cell in pixels. `auto` asks tmux, then the terminal; or `WxH`, such as `19x42`. |
| `baseline` | `0.773` | Where the font's baseline sits in a cell, from the top, as a fraction of the cell height. 0.773 is JetBrains Mono. |
| `ptPx` | `3.4` | Formula size in pixels per TeX point. 3.4 matches a 12 pt font with 42 px cells. |
| `minShrink` | `0.8` | How far an inline formula may shrink to stay on one row before it takes more rows. |
| `promptHint` | `true` | Add a short note (under 100 tokens) to Claude's system prompt: write math as `$...$` and `$$...$$`, never in code blocks; ` ```mermaid ` blocks draw as diagrams. |
| `packages` | empty | Extra TeX Live packages for every formula, separated by commas, semicolons or spaces: `physics, tensor`. Package names only (letters, digits, `-`). A package that is not installed, or a bad name, is skipped and named in `/inline-math status`. |
| `macros` | empty | Your own definitions, available in every formula: `\newcommand`, `\DeclareMathOperator`, `\def`, one or more lines. In `settings.json` write `\n` between lines and `\\` for each backslash. |
| `diagramColor` | `true` | Draw diagrams with Catppuccin accent colours: blue boxes, lavender lines and arrows (Latte on a light scheme, Mocha on a dark one), labels in the text colour. Off: the text colour alone. |

`packages` and `macros` are yours, so they are trusted as given: they go into the LaTeX preamble (and the precompiled format) without the safety check that Claude's formulas get. They load before the mod's own definitions, so a package's command wins over the mod's stand-in of the same name: with `physics`, `\abs`, `\norm`, `\dv`, `\pdv` and `\qty` are physics' own, and physics also redefines `\div`, `\Re` and `\Im` (and its `\qty` replaces siunitx's). When the preamble does not work with all of them, the mod keeps what works: your `macros` (as a whole) if they work alone, then each package that still works when added; each one left out is named, with TeX's error, in `/inline-math status`. That check runs once per set of options. Changing either option typesets every formula again with the new preamble.

Typeset pictures are cached on disk in `$XDG_CACHE_HOME/claude-inline-math` (else `~/.cache/claude-inline-math`), so a formula seen before is drawn without running TeX.

## How it stays fast

Targets: drawing an already-typeset reply again costs under 5 ms of the mod's time; one new formula is back in under 50 ms, a batch of ten in under 100 ms, process start included.

- **Precompiled format.** The whole preamble (fonts, `amsmath`, `mathtools`, the symbol packages, your `packages` and `macros`) is dumped once into a LaTeX format file in `~/.cache/claude-inline-math/fmt/` (with `mylatexformat`, or `latex -ini`). A TeX run then starts with the preamble already loaded, so a large preamble costs almost nothing per formula. The format is built again by itself when TeX Live changes, when the package list or your `packages` and `macros` change, and when the renderer is updated.
- **Render daemon.** Claude Code starts a new process for every call a mod makes, so the backend keeps its own: `bin/client.py` (standard library only, quick to start) hands each request to `bin/renderd.py`, a daemon that keeps Pillow loaded, keeps TeX processes waiting with the format already read, and keeps `dvipng` running. It listens on a socket in `$XDG_RUNTIME_DIR/claude-inline-math/` (else `/tmp/claude-inline-math-<uid>/`, else `~/.cache/claude-inline-math/run/`), a folder only you can use (`/inline-math status` says why a folder was refused); it serves every Claude Code session you have open, stops after 10 minutes without a request, and replaces itself when the renderer files change. When the daemon cannot be started or reached, `client.py` renders in its own process: slower, the same pictures.
- **Batches.** Formulas asked for within 30 ms go to the backend in one call of up to 16. Formulas found while a reply streams (below) gather for up to 400 ms, so a long reply costs a few calls, not one per formula. In the daemon, a batch heavy with chemistry (`\ce`, `\pu`: tens of milliseconds of TeX each) is split over its waiting TeX processes, which typeset the parts side by side.
- **Warm start.** The daemon stops after 10 idle minutes; when a turn starts after a pause of a minute or more, the mod starts it again (and has it load the format) while Claude is still thinking, so the reply's first formulas do not wait for it.
- **Cheap redraws.** Claude Code asks a mod to draw every reply on screen again whenever any redraw is asked for. The mod parses each reply once, and a reply whose pictures are all ready returns the drawing it made last time (a lookup per formula). When a batch of pictures arrives, only the replies that were waiting for one of them are drawn again; the rest of the transcript is not touched.

## Streaming

Claude Code draws a reply's text itself while it streams, so formulas show as their LaTeX source until the text block they are in is finished; this mod cannot replace that live text. Meanwhile it watches the text stream by (without changing it: the transcript and the screen get exactly what the model wrote) and queues each formula for TeX once its closing `$` has arrived; queued formulas go to the backend together, every 400 ms or so. When the block ends (at a tool call, at the next block, or at the end of the reply), the formulas it closed since are queued and everything queued is sent at once. So the formulas of a finished block are usually typeset already and appear at once, with no Unicode stand-in in between; a formula closed in the last ~0.1 s before the block ends can show as its Unicode stand-in for one backend round trip (about 30-60 ms, longer for chemistry). The watcher reads a block again at most every 100 ms, and less often when a reading takes longer (a block of tens of thousands of characters), so it does not slow the stream. Formulas in subagents' replies, in thinking and in code are not watched.

## Diagrams

A closed ` ```mermaid ` code block, at the top level of a reply or in a list item, is drawn as Unicode box-drawing art by the bundled [beautiful-mermaid](https://www.npmjs.com/package/beautiful-mermaid) library (MIT, `hooks/vendor/`). Drawn kinds: `flowchart` / `graph`, `sequenceDiagram`, `stateDiagram`, `classDiagram`, `erDiagram` and `xychart-beta`. Chinese, Japanese and Korean labels count as two cells, so the boxes stay aligned.

Any other kind (`pie`, `gantt`, `mindmap`, ...), a block the library cannot parse, and a diagram that is too big (more than 80 rows, more than 8000 characters or 150 lines, a flowchart of more than 100 nodes or more than 60 nodes in subgraphs, or wider than the reply) is shown as Claude Code shows a code block, with one dim line under it that says why. A mermaid block inside a blockquote, and one that is not closed, are left to Claude Code. `style`, `classDef`, `class`, `linkStyle` and `click` statements are ignored (the art has no node colours or links); mermaid's entity codes (`#quot;`, `#lt;`, `#9829;`) and markdown-string labels (`` "`**bold** text`" ``) draw as their text.

The library's layout has limits of its own. It draws every loop wrongly (a retry such as `B -->|no| A` after `A --> B`, a training loop, `A --> A`, a state that goes back to an earlier one): the back edge goes over another edge, loses its arrowheads, or moves its label onto the wrong edge. So a flowchart or state diagram with a loop is not drawn; it shows as source with the reason `diagram has a loop, which the text renderer cannot draw`. Lines without a direction (`---`) and two-way arrows (`<-->`) do not count as loops. An edge into a node the library placed on the bottom row can also run off the drawing. The mod checks every flowchart it draws (an arrowhead for each node an edge points at, no line leaving the drawing) and, when an edge was dropped or cut, tries the other layouts it has, then shows the source with the reason `diagram renderer dropped or cut an edge`, rather than a diagram that says something the source does not.

## Reply macros

Claude sometimes defines a command in a reply and uses it later in the same reply:

```latex
$$\newcommand{\norm}[1]{\left\lVert #1 \right\rVert}$$
Then $\norm{x} \le 1$ for every $x$.
```

The mod expands such definitions as text before TeX sees a formula, so `\norm{x}` reaches TeX as `\left\lVert x \right\rVert`, and the definition itself draws as nothing. The expanded formula still goes through the renderer's safety check. Rules:

- The scope is one reply (one text block). Definitions do not carry over to the next reply.
- A name defined twice takes its last definition, wherever in the reply it is; `\providecommand` does not replace an earlier definition. `\global` before a definition is accepted (and changes nothing: the scope is the reply).
- Arguments are pasted as TeX reads them: `\newcommand{\norm}[1]{\lVert#1\rVert}` with `\norm{x}` gives `\lVert x\rVert`, not the unknown `\lVertx`. A macro whose body is empty draws as nothing.
- `\edef` and `\xdef` (definitions that expand their body at once) are not expanded; TeX refuses them.
- Arguments `#1` to `#9`, with an optional first argument and its default (`\newcommand{\f}[2][x]{...}`). `\def` takes undelimited parameters only (`\def\f#1#2{...}`); a `\def` with delimited parameters is not expanded, and TeX refuses it.
- Expansion stops at 32 levels of nesting or 8000 characters. A formula that passes either limit (a macro that calls itself, for example), or that uses a macro without its argument, is drawn as written, which TeX then refuses.

## Limitations

- Pictures need the kitty graphics protocol. iTerm2, WezTerm, Alacritty, VS Code's terminal and Windows Terminal show the Unicode stand-ins (diagrams still draw).
- Only the terminal is drawn. The desktop app, VS Code and mobile show replies as they always do.
- While a reply streams, its formulas and diagrams show as source; they are drawn when the text block finishes (see [Streaming](#streaming)).
- Headings, tables and blockquotes are drawn by Claude Code's Markdown renderer, so a formula inside one shows as its Unicode stand-in, not as a picture.
- Text in a paragraph that holds a formula is selected and copied as the text around the pictures; a picture copies as nothing. Use `ctrl+o` to see and copy the source.
- The formula colour follows the desktop scheme, not a theme you set in the terminal alone. Set `foreground` if your terminal's text colour differs.
- After the terminal font size changes, pictures are typeset again within a few seconds; until then they are scaled by the terminal.
- Claude's formulas are typeset in a safe subset of LaTeX: commands that read or write files, run programs, or change TeX's rules are refused and shown as text. One formula cannot change how another is drawn (reply macros are expanded by the mod, not by TeX).

## Troubleshooting

- **Formulas show as Unicode text, never as pictures.** Run `/inline-math status`. If the backend is not ready, install what it names, then run `/inline-math clear`. If it is ready: check that your terminal is Ghostty or kitty, and inside tmux that `tmux show -g allow-passthrough` says `on` (and `focus-events`). Restart tmux after you change its config.
- **A toast says formulas are shown as text.** The backend could not run (no `python3`, no Pillow, no `latex` or `dvipng`). The reason is in the toast and in `/inline-math status`. Once it is fixed, formulas come back within a minute, or at once with `/inline-math status`.
- **Formulas are slow to appear.** `/inline-math status` shows the daemon and the format. `daemon: not reached` means each batch starts TeX from nothing: the status problems name each folder the daemon's socket could not use and why (another user's folder, open to other users, not writable), and `renderd.log` in the daemon's folder logs its errors. `format: not built` means every TeX run reads the whole preamble: usually a package in `packages` or a line in `macros` that breaks it, named in the status problems. The first formula after a TeX Live update, or after you change `packages` or `macros`, waits for the format to be built again (a few seconds).
- **Stop the daemon or rebuild the format by hand.** The daemon stops by itself after 10 idle minutes; `pkill -f renderd.py` stops it now, and the next formula starts a new one. Deleting `~/.cache/claude-inline-math/fmt/` makes the next formula build the format again. Then run `/inline-math clear` so every formula on screen is typeset again.
- **Every formula is typeset again after an update.** The picture cache is keyed by the renderer's version, so an update that changes how formulas are drawn typesets each one once more.
- **Formulas sit too high or too low.** Set `baseline` for your font, and `cell` if tmux reports the wrong size (`tmux display -p '#{client_cell_width}x#{client_cell_height}'`).
- **Formulas are too large or too small next to the text.** Change `ptPx`.
- **Formulas are hard to read after switching light and dark.** They are typeset again within a few seconds; `/inline-math clear` does it at once. Or set `foreground`.
- **One formula shows as text.** TeX refused it; `/inline-math status` lists recent errors. Check [docs/compatibility.md](docs/compatibility.md) for the command.
- **A diagram shows as source.** The dim line under it says why: a kind the library does not draw, a parse error, or a diagram too big for the window. A wider window can help with the last.
- **Claude writes math in code blocks or as plain text.** Check that `promptHint` is on; the note applies from the next turn.
- **Something looks wrong in a reply.** `/inline-math off` draws replies as Claude Code does; `claude --debug` logs why a hook was skipped.

## License

MIT, see [LICENSE](LICENSE). The bundled beautiful-mermaid library is MIT too, see `hooks/vendor/LICENSE`.
