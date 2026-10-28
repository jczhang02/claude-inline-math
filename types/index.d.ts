// The inline-math contract: the state the mod keeps for the session.

/** A drawn site's redraw count: bumped when a batch of pictures the site waited for lands. */
export type InlineMathRedraw = number

declare module 'claude-code' {
  interface PluginState {
    // One member per drawn site (a message's requestId); a reply reads its
    // own while drawing, so a write redraws that reply and no other.
    'inline-math': { redraw: StateFamily<InlineMathRedraw> }
  }
}
