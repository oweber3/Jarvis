// The code of a reply exactly as it was written. The Markdown library takes backslash escapes out of inline code and
// indented code blocks, and reads `\` before a backtick inside code as an escape, so `C:\Users\me\` would run on into
// the next code span; CommonMark does neither. Before the library reads a reply, each of its code spans (found as
// CommonMark finds them, on one line, outside fenced and indented code) becomes a token that carries the span as
// written, and each backslash of an indented code block becomes a marker. The page draws a token as inline code and
// puts the span back as written wherever else it lands (a code block, an address, a description), so nothing is lost.
// A reply that already holds the token characters is left to the library. Pure, covered by the Markdown tests.

const SPAN = "\uE000"
const TABLE_SPAN = "\uE002" // a span in a table's row, where an escaped pipe is the pipe (GitHub's tables)
const END = "\uE001"
const BACKSLASH = "\uE003"
// Each UTF-16 unit of a span is carried as one private-use character of plane 15 (surrogates DB80 to DBBF).
const PLANE = 0xf0000
const TOKEN = /([\uE000\uE002])((?:[\uDB80-\uDBBF][\uDC00-\uDFFF])*)\uE001/g
const OURS = /[\uE000-\uE003]|[\uDB80-\uDBBF]/
const PUNCTUATION = /[!-/:-@[-`{-~]/
// The library's angle-bracket link, which binds tighter than a code span, as CommonMark's autolinks do.
const ANGLE_LINK = /^<([^ >]+[:@/][^ >]+)>/
// The library's fenced block and table, anchored where the scan stands.
const FENCED = /(?: {1,3})?(`{3,}|~{3,}) *(\S+)? *([^\n]*?)?\n([\s\S]*?)(?:\1\n?|$)/y
const TABLE = /(\|.*)\n *\|? *[-:]+ *\|[-| :]*\n((?:.*\|.*\n)*)/y
const LIST_ITEM = /^ {0,3}(?:[*+-]|\d{1,9}[.)])(?: |$)/

function encode(text: string): string {
  let out = ""
  for (let unit = 0; unit < text.length; unit++) out += String.fromCodePoint(PLANE + text.charCodeAt(unit))
  return out
}
const decode = (body: string) => Array.from(body, (unit) => String.fromCharCode(unit.codePointAt(0)! - PLANE)).join("")

/** The reply as the library is given it: code spans as tokens, an indented block's backslashes as markers. */
export function protectCode(markdown: string): string {
  if (OURS.test(markdown)) return markdown
  // As the library reads its input: one kind of line end, no form feeds, tabs as four spaces.
  const source = markdown.replace(/\r\n?/g, "\n").replace(/\f/g, "").replace(/\t/g, "    ")
  const scan = source + "\n\n" // the library ends its input so
  const lines = source.split("\n")
  const kind = lines.map((): "text" | "fenced" | "indented" | "table" => "text")
  const starts: number[] = []
  lines.reduce((at, line) => (starts.push(at), at + line.length + 1), 0)
  const lineAt = (offset: number) => {
    let index = starts.length - 1
    while (index > 0 && starts[index]! > offset) index--
    return index
  }
  const mark = (from: number, to: number, as: (typeof kind)[number]) => {
    for (let index = from; index <= to && index < lines.length; index++) kind[index] = as
  }

  for (let index = 0; index < lines.length; index++) {
    if (kind[index] !== "text") continue
    FENCED.lastIndex = starts[index]!
    const fence = FENCED.exec(scan)
    if (fence) {
      mark(index, lineAt(starts[index]! + fence[0].length - 1), "fenced")
      continue
    }
    TABLE.lastIndex = starts[index]!
    const table = lines[index]!.startsWith("|") ? TABLE.exec(scan) : null
    if (table) mark(index, lineAt(starts[index]! + table[0].length - 1), "table")
  }
  // An indented block where the library sees one for sure: at the start, or after a blank line below a block with no
  // indented line and no list item (a list keeps the indented lines after it). Elsewhere the library is left to it.
  for (let index = 0; index < lines.length; index++) {
    if (kind[index] !== "text" || !lines[index]!.startsWith("    ") || !lines[index]!.trim()) continue
    let above = index - 1
    while (above >= 0 && !lines[above]!.trim()) above--
    let first = above
    while (first > 0 && lines[first - 1]!.trim()) first--
    const block = above < 0 ? [] : lines.slice(first, above + 1)
    const opens = above < 0 || (above < index - 1 && !block.some((line) => line.startsWith(" ") || LIST_ITEM.test(line)))
    if (!opens) continue
    for (let line = index; line < lines.length && kind[line] === "text"; line++) {
      if (lines[line]!.trim() && !lines[line]!.startsWith("    ")) break
      kind[line] = "indented"
    }
  }

  return lines
    .map((line, index) => {
      if (kind[index] === "fenced") return line
      if (kind[index] === "indented") return line.replaceAll("\\", BACKSLASH)
      return protectLine(line, kind[index] === "table")
    })
    .join("\n")
}

// The code spans of one line, as CommonMark finds them: a run of backticks not escaped, closed by the next run of the
// same length; inside, a backslash is just a backslash.
function protectLine(line: string, inTable: boolean): string {
  let out = ""
  let at = 0
  while (at < line.length) {
    const char = line[at]!
    if (char === "\\" && PUNCTUATION.test(line[at + 1] ?? "")) {
      out += line.slice(at, at + 2)
      at += 2
      continue
    }
    if (char === "<") {
      const link = ANGLE_LINK.exec(line.slice(at))
      if (link) {
        out += link[0]
        at += link[0].length
        continue
      }
    }
    if (char !== "`") {
      out += char
      at++
      continue
    }
    const run = runAt(line, at)
    let close = -1
    for (let next = at + run; next < line.length; ) {
      if (line[next] !== "`") {
        next++
        continue
      }
      const length = runAt(line, next)
      if (length === run) {
        close = next
        break
      }
      next += length
    }
    if (close < 0) {
      out += line.slice(at, at + run)
      at += run
      continue
    }
    out += (inTable ? TABLE_SPAN : SPAN) + encode(line.slice(at, close + run)) + END
    at = close + run
  }
  return out
}

function runAt(line: string, at: number): number {
  let length = 0
  while (line[at + length] === "`") length++
  return length
}

/** ``text`` with every token back to its span as written and every marker back to a backslash. */
export function restoreCode(text: string): string
export function restoreCode(text: string | undefined): string | undefined
export function restoreCode(text: string | undefined): string | undefined {
  if (!text || !OURS.test(text)) return text
  return text.replace(TOKEN, (_, _kind: string, body: string) => decode(body)).replaceAll(BACKSLASH, "\\")
}

export type CodePiece = string | { code: string }

/** The text of a reply's text node in pieces: plain text, and inline code for each token in it. */
export function codePieces(text: string): CodePiece[] {
  const pieces: CodePiece[] = []
  let from = 0
  for (const token of text.matchAll(TOKEN)) {
    if (token.index > from) pieces.push(restoreCode(text.slice(from, token.index)))
    pieces.push({ code: spanCode(decode(token[2]!), token[1] === TABLE_SPAN) })
    from = token.index + token[0].length
  }
  if (from < text.length) pieces.push(restoreCode(text.slice(from)))
  return pieces
}

/** The code a span shows (CommonMark): without its backticks, and without one space each side when both sides have
 * one and it is not all spaces. */
function spanCode(span: string, inTable: boolean): string {
  const ticks = runAt(span, 0)
  let code = span.slice(ticks, span.length - ticks)
  if (inTable) code = code.replaceAll("\\|", "|")
  if (code.length > 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim()) code = code.slice(1, -1)
  return code
}

/** Whether ``text`` holds a token or marker (so a node needs drawing by the page rather than by the library). */
export const holdsCode = (text: string | undefined): boolean => !!text && OURS.test(text)
