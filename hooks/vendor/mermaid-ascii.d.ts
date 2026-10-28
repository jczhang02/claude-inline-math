// Types for the vendored beautiful-mermaid ASCII renderer (mermaid-ascii.js).
// Only the one export the mod uses; see VENDOR.md.

export type AsciiTheme = {
  fg?: string
  border?: string
  line?: string
  arrow?: string
  accent?: string
  bg?: string
  corner?: string
  junction?: string
}

export type AsciiRenderOptions = {
  useAscii?: boolean
  paddingX?: number
  paddingY?: number
  boxBorderPadding?: number
  colorMode?: 'none' | 'ansi16' | 'ansi256' | 'truecolor' | 'html'
  theme?: AsciiTheme
}

export function renderMermaidASCII(text: string, options?: AsciiRenderOptions): string

// What the last renderMermaidASCII call drew of a flowchart or state diagram
// (null for other diagrams): edges it could not route or draw, the number of
// distinct arrowheads it drew, and the edge labels it drew.
export type AsciiLayoutReport = { dropped: number; heads: number; labels: string[] }

export function layoutReport(): AsciiLayoutReport | null
