// The code of a reply exactly as it was written. The Markdown library takes backslash escapes out of inline code and
// indented code blocks (CommonMark leaves code alone), so `\\server\share` would lose a backslash and `\d+\.\d+` its
// escapes. Given the reply's own text, this finds the code each such node of the library's came from, in the order
// the library meets them, using the library's own patterns. Code it cannot place is left as the library gave it.
// Pure, covered by the Markdown tests.

/** The library's backslash escape (markdown-to-jsx 7, `textEscaped`) and the unescaping it applies to code. */
const UNESCAPE = /\\([^0-9A-Za-z\s])/g
const unescape = (text: string) => text.replace(UNESCAPE, "$1")
/** Its trimming: trailing characters up to and including the space. */
const trimEnd = (text: string) => text.replace(/[\0-\x20]+$/, "")

// The library's inline code, indented code block and fenced code block, anchored where the scan stands.
const INLINE = /(`+)((?:\\`|(?!\1)`|[^`])+)\1/y
const INDENTED = /(?: {4}[^\n]+\n*)+(?:\n *)+\n?/y
const FENCED = /(?: {1,3})?(`{3,}|~{3,}) *(\S+)? *([^\n]*?)?\n([\s\S]*?)(?:\1\n?|$)/y

export type RawCode = {
  /** The code of the next inline code node whose text the library gave as ``text``. */
  inline(text: string): string
  /** The code of the next indented code block whose text the library gave as ``text``. */
  block(text: string): string
}

export function rawCode(markdown: string): RawCode {
  // As the library reads its input: one kind of line end, no form feeds, tabs as four spaces, and room at the end.
  const source =
    trimEnd(markdown.replace(/\r\n?/g, "\n").replace(/\f/g, "").replace(/\t/g, "    ")).replace(/^\n+/, "") + "\n\n"
  const lineStarts = [0]
  for (let at = source.indexOf("\n"); at >= 0 && at + 1 < source.length; at = source.indexOf("\n", at + 1)) lineStarts.push(at + 1)

  // Fenced blocks keep their code as written; nothing inside them is inline code or an indented block.
  const fences: [number, number][] = []
  for (const start of lineStarts) {
    if (fences.length && start < fences[fences.length - 1]![1]) continue
    FENCED.lastIndex = start
    const fence = FENCED.exec(source)
    if (fence) fences.push([start, start + fence[0].length])
  }
  const fenceAt = (at: number) => fences.find(([from, to]) => at >= from && at < to)

  const escaped = (at: number) => {
    let slashes = 0
    while (at - slashes - 1 >= 0 && source[at - slashes - 1] === "\\") slashes++
    return slashes % 2 === 1
  }

  let inlineFrom = 0
  let blockFrom = 0
  return {
    inline(text) {
      for (let at = source.indexOf("`", inlineFrom); at >= 0; at = source.indexOf("`", at + 1)) {
        const fence = fenceAt(at)
        if (fence) {
          at = fence[1] - 1
          continue
        }
        if (escaped(at)) continue
        INLINE.lastIndex = at
        const code = INLINE.exec(source)
        if (code && unescape(code[2]!) === text) {
          inlineFrom = at + code[0].length
          // In a table's row an escaped pipe is the pipe, code or not (GFM); the row is a line starting with one.
          const line = source.slice(source.lastIndexOf("\n", at) + 1, at)
          return /^ *\|/.test(line) ? code[2]!.replace(/\\\|/g, "|") : code[2]!
        }
      }
      return text
    },
    block(text) {
      for (const start of lineStarts) {
        if (start < blockFrom || fenceAt(start)) continue
        INDENTED.lastIndex = start
        const block = INDENTED.exec(source)
        if (!block) continue
        const code = trimEnd(block[0].replace(/^ {4}/gm, ""))
        if (unescape(code) === text) {
          blockFrom = start + block[0].length
          return code
        }
      }
      return text
    },
  }
}
