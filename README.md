# inline-math

A Claude Code mod that draws the LaTeX in Claude's replies as typeset formulas, right in the terminal. Each formula is a picture placed inline with the text around it and aligned to the text baseline, so `$e^{i\pi} + 1 = 0$` reads like a line in a paper, not like source code. Display math (`$$...$$`) is centred on its own lines. Paragraphs with no formulas are drawn by Claude Code's own Markdown renderer, unchanged.

The stored conversation is never changed: the model, `ctrl+o`, exports and copies still see the LaTeX source.

## What it draws

- Inline math: `$...$` and `\(...\)`. A formula is kept on one text row when it fits at 80% of its size or more; a taller one (a fraction, a sum with limits) makes only its own line taller.
- Display math: `$$...$$` and `\[...\]`, centred; a formula wider than the reply is drawn smaller to fit.
- Lines are wrapped by the mod (East Asian wide characters count as two cells), so text, Chinese and formulas wrap together and line up with the rest of the reply.
- Bold, italic, strikethrough, inline code and links around formulas keep their styles. Lists with formulas keep their markers and hanging indents.
- Not math: code spans and code blocks, `\$`, and prices such as `$5 and $10` (the pandoc rule: an opening `$` must be followed by a non-space, a closing `$` must follow a non-space and must not be followed by a digit).
- While a reply streams, a formula that is not closed yet stays as text; each finished formula is drawn as soon as it is typeset.
- Until a picture is ready, and when TeX cannot typeset a formula, a readable Unicode version stands in its place (`x²`, `α + β`, `√π`, `ℝ`).

## Requirements

- **A terminal with the kitty graphics protocol**: Ghostty or kitty. Elsewhere the pictures are replaced by their Unicode text.
- **tmux** (optional) with passthrough and focus events on, in `~/.config/tmux/tmux.conf` or `~/.tmux.conf`:

  ```tmux
  set -g allow-passthrough on
  set -g focus-events on
  ```

- **TeX Live** (or another TeX distribution) with `latex`, `dvipng` and the `preview`, `amsmath`, `amssymb` and `lmodern` packages. On Arch: `pacman -S texlive-basic texlive-latex texlive-latexrecommended texlive-latexextra texlive-binextra texlive-fontsrecommended`; on Debian and Ubuntu: `apt install texlive-latex-extra dvipng`; on macOS: `brew install --cask mactex-no-gui`. For Chinese text inside a formula (`\text{面积}`), also `CJKutf8` with Chinese fonts: `texlive-langcjk` and `texlive-langchinese` on Arch, `texlive-lang-chinese` on Debian and Ubuntu.
- **python3** with **Pillow**: `pip install --user pillow`, or your system's `python-pillow` / `python3-pil`.
- Claude Code 2.1.311 or later.

Run `/inline-math status` after you install it: it says which of these it found.

## Install

From a local folder, for one session:

```sh
claude --plugin-dir /path/to/inline-math
```

For every session, add the folder to `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json` (use `:` between several folders, `;` on Windows):

```json
{ "env": { "CLAUDE_CODE_PLUGIN_DIRS": "/path/to/inline-math" } }
```

As an installed plugin: make the folder a marketplace by adding `.claude-plugin/marketplace.json` beside `plugin.json`,

```json
{ "name": "inline-math", "owner": { "name": "JC Zhang" }, "plugins": [{ "name": "inline-math", "source": "./" }] }
```

then install it once. The plugin is read from the folder itself, and `/reload-plugins` picks up your edits:

```sh
claude plugin marketplace add /path/to/inline-math
claude plugin install inline-math@inline-math
```

## Commands

| Command | What it does |
| --- | --- |
| `/inline-math status` | Backend health (latex, dvipng, Pillow), cell size, formula colour, cache counts, recent TeX errors |
| `/inline-math on` / `off` | Draw formulas, or leave replies as Claude Code draws them; kept across sessions |
| `/inline-math demo` | A sample reply: inline, tall inline, display, Chinese text, a list, bold with math |
| `/inline-math clear` | Forget the drawn formulas and typeset them again |

## Options

Set them in `/config` (each is a row there), or in `~/.claude/settings.json` under `pluginConfigs`:

```json
{ "pluginConfigs": { "inline-math": { "options": { "ptPx": 3.6 } } } }
```

| Option | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Draw formulas. `/inline-math on` or `off` overrides it. |
| `foreground` | `auto` | Formula colour. `auto` follows the desktop's light or dark scheme (Catppuccin Latte text `#4c4f69`, Mocha text `#cdd6f4`) and is looked up again every 5 seconds for 10 minutes after a formula was last drawn, then every minute; or `#rrggbb`. |
| `cell` | `auto` | One character cell in pixels. `auto` asks tmux, then the terminal; or `WxH`, such as `19x42`. |
| `baseline` | `0.773` | Where the font's baseline sits in a cell, from the top, as a fraction of the cell height. 0.773 is JetBrains Mono. |
| `ptPx` | `3.4` | Formula size in pixels per TeX point. 3.4 matches a 12 pt font with 42 px cells. |
| `minShrink` | `0.8` | How far an inline formula may shrink to stay on one row before it takes more rows. |
| `promptHint` | `true` | Add a short note to Claude's system prompt: write math as `$...$` and `$$...$$`, never in code blocks. |

Typeset pictures are cached on disk in `$XDG_CACHE_HOME/claude-inline-math` (else `~/.cache/claude-inline-math`), so a formula seen before is drawn without running TeX.

## Limitations

- Pictures need the kitty graphics protocol. iTerm2, WezTerm, Alacritty, VS Code's terminal and Windows Terminal show the Unicode stand-ins.
- Only the terminal is drawn. The desktop app, VS Code and mobile show replies as they always do.
- Headings, tables and blockquotes are drawn by Claude Code's Markdown renderer, so a formula inside one shows as its Unicode stand-in, not as a picture.
- Text in a paragraph that holds a formula is selected and copied as the text around the pictures; a picture copies as nothing. Use `ctrl+o` to see and copy the source.
- The formula colour follows the desktop scheme, not a theme you set in the terminal alone. Set `foreground` if your terminal's text colour differs.
- After the terminal font size changes, pictures are typeset again within a few seconds; until then they are scaled by the terminal.
- A formula is typeset in a safe subset of LaTeX: commands that read or write files, run programs, define commands or change TeX's rules, and environments other than the math ones (`aligned`, `cases`, the `matrix` family, `array`, ...), are refused and shown as text. One formula cannot change how another is drawn.

## Troubleshooting

- **Formulas show as Unicode text, never as pictures.** Run `/inline-math status`. If the backend is not ready, install what it names, then run `/inline-math clear`. If it is ready: check that your terminal is Ghostty or kitty, and inside tmux that `tmux show -g allow-passthrough` says `on` (and `focus-events`). Restart tmux after you change its config.
- **A toast says formulas are shown as text.** The backend could not run (no `python3`, no Pillow, no `latex` or `dvipng`). The reason is in the toast and in `/inline-math status`. Once it is fixed, formulas come back within a minute, or at once with `/inline-math status`.
- **Formulas sit too high or too low.** Set `baseline` for your font, and `cell` if tmux reports the wrong size (`tmux display -p '#{client_cell_width}x#{client_cell_height}'`).
- **Formulas are too large or too small next to the text.** Change `ptPx`.
- **Formulas are hard to read after switching light and dark.** They are typeset again within a few seconds; `/inline-math clear` does it at once. Or set `foreground`.
- **One formula shows as text.** TeX refused it; `/inline-math status` lists recent errors.
- **Claude writes math in code blocks or as plain text.** Check that `promptHint` is on; the note applies from the next turn.
- **Something looks wrong in a reply.** `/inline-math off` draws replies as Claude Code does; `claude --debug` logs why a hook was skipped.

## License

MIT, see [LICENSE](LICENSE).
