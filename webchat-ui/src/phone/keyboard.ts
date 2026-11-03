import { KEYBOARD_MIN_HEIGHT } from "@/phone/orbState"

// Whether the on-screen keyboard is up, from the viewport sizes. On iOS the visual viewport shrinks while the
// layout viewport stays; on Android the layout viewport shrinks too, so the tallest layout seen at this width
// is the baseline. A change of width (turning the phone) starts the baseline again.

export class KeyboardTracker {
  private baseline = 0
  private width = 0

  /** `layoutHeight` is `window.innerHeight`, `visualHeight` the visual viewport's height. */
  update(layoutHeight: number, visualHeight: number, width: number): boolean {
    if (width !== this.width) {
      this.width = width
      this.baseline = 0
    }
    this.baseline = Math.max(this.baseline, layoutHeight, visualHeight)
    return this.baseline - visualHeight >= KEYBOARD_MIN_HEIGHT
  }
}
