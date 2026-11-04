// Colouring for the code blocks of a reply, by a small scanner of its own rather than a highlighting library: comments,
// strings, numbers, keywords and called names, for the languages a reply mostly holds. It never changes the code: the
// pieces put together are the code exactly. Pure, covered by tests.

export type TokenKind = "comment" | "string" | "number" | "keyword" | "fn"
export type Token = { text: string; kind?: TokenKind }

/** Longer code is shown without colour, to keep the page quick. */
export const MAX_HIGHLIGHT_CHARS = 20_000

type Family = { line?: string[]; block?: [string, string][]; quotes: string[]; triple?: boolean; keywords: Set<string>; caseless?: boolean }

const words = (list: string) => new Set(list.split(/\s+/).filter(Boolean))

const C_KEYWORDS = words(`
  abstract as async await break case catch class const continue default defer delete do else enum export extends
  false final finally fn for from func function go if impl implements import in instanceof interface let match mod
  mut new null nil package private protected pub public readonly return self static struct super switch this throw
  throws trait true try type typeof undefined use var void while yield int float double char bool boolean string long
  short unsigned`)
const PYTHON_KEYWORDS = words(`
  and as assert async await break case class continue def del elif else except False finally for from global if
  import in is lambda match None nonlocal not or pass raise return self True try while with yield`)
const SHELL_KEYWORDS = words(`
  if then else elif fi for while do done case esac in function return export local echo exit set unset source alias
  param foreach true false null`)
const SQL_KEYWORDS = words(`
  select from where insert into values update set delete create table drop alter join left right inner outer full on
  group by order having limit offset and or not null as distinct union all primary key foreign references index view
  is in like between case when then else end asc desc with returning`)
const JSON_KEYWORDS = words("true false null")

const C: Family = { line: ["//"], block: [["/*", "*/"]], quotes: ['"', "'", "`"], keywords: C_KEYWORDS }
const FAMILIES: Record<string, Family> = {
  python: { line: ["#"], quotes: ['"', "'"], triple: true, keywords: PYTHON_KEYWORDS },
  shell: { line: ["#"], quotes: ['"', "'"], keywords: SHELL_KEYWORDS },
  sql: { line: ["--"], block: [["/*", "*/"]], quotes: ["'", '"'], keywords: SQL_KEYWORDS, caseless: true },
  json: { quotes: ['"'], keywords: JSON_KEYWORDS },
  markup: { block: [["<!--", "-->"]], quotes: ['"', "'"], keywords: new Set() },
  css: { block: [["/*", "*/"]], quotes: ['"', "'"], keywords: new Set() },
  c: C,
  plain: { quotes: ['"', "'"], keywords: new Set() },
}

const LANGUAGES: Record<string, keyof typeof FAMILIES> = {
  py: "python", python: "python", python3: "python",
  sh: "shell", bash: "shell", zsh: "shell", shell: "shell", console: "shell", powershell: "shell", ps1: "shell",
  ps: "shell", pwsh: "shell", yaml: "shell", yml: "shell", toml: "shell", ini: "shell", dockerfile: "shell",
  ruby: "shell", rb: "shell", r: "shell",
  sql: "sql", sqlite: "sql", postgres: "sql", mysql: "sql",
  json: "json", jsonc: "c",
  html: "markup", xml: "markup", svg: "markup",
  css: "css", scss: "css", less: "css",
  js: "c", javascript: "c", jsx: "c", ts: "c", typescript: "c", tsx: "c", mjs: "c", cjs: "c", java: "c", c: "c",
  cpp: "c", "c++": "c", h: "c", hpp: "c", cs: "c", csharp: "c", "c#": "c", go: "c", golang: "c", rust: "c", rs: "c",
  swift: "c", kotlin: "c", kt: "c", php: "c", dart: "c", scala: "c",
}

function familyOf(language: string): Family {
  return FAMILIES[LANGUAGES[language.trim().toLowerCase()] ?? "plain"]!
}

const NUMBER = /^(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)/
const WORD = /^[A-Za-z_$][\w$]*/

export function highlight(code: string, language: string): Token[] {
  if (code.length > MAX_HIGHLIGHT_CHARS) return [{ text: code }]
  const family = familyOf(language)
  const tokens: Token[] = []
  let plain = ""
  const push = (text: string, kind?: TokenKind) => {
    if (!kind) {
      plain += text
      return
    }
    if (plain) tokens.push({ text: plain })
    plain = ""
    tokens.push({ text, kind })
  }

  let i = 0
  while (i < code.length) {
    const rest = code.slice(i, i + 4)
    const line = family.line?.find((start) => code.startsWith(start, i))
    if (line) {
      const end = code.indexOf("\n", i)
      const stop = end < 0 ? code.length : end
      push(code.slice(i, stop), "comment")
      i = stop
      continue
    }
    const block = family.block?.find(([start]) => code.startsWith(start, i))
    if (block) {
      const end = code.indexOf(block[1], i + block[0].length)
      const stop = end < 0 ? code.length : end + block[1].length
      push(code.slice(i, stop), "comment")
      i = stop
      continue
    }
    const quote = family.quotes.find((q) => rest.startsWith(q))
    if (quote) {
      const triple = family.triple && code.startsWith(quote.repeat(3), i) ? quote.repeat(3) : null
      const closing = triple ?? quote
      let j = i + closing.length
      while (j < code.length && !code.startsWith(closing, j)) {
        if (code[j] === "\\") j++
        else if (!triple && quote !== "`" && code[j] === "\n") break
        j++
      }
      const stop = Math.min(code.length, code.startsWith(closing, j) ? j + closing.length : j)
      push(code.slice(i, stop), "string")
      i = stop
      continue
    }
    const before = i > 0 ? code[i - 1]! : ""
    if (!/[\w$]/.test(before)) {
      const number = NUMBER.exec(code.slice(i, i + 64))
      if (number) {
        push(number[0], "number")
        i += number[0].length
        continue
      }
      const word = WORD.exec(code.slice(i, i + 128))
      if (word) {
        const text = word[0]
        const keyword = family.keywords.has(family.caseless ? text.toLowerCase() : text)
        const called = !keyword && /^\s*\(/.test(code.slice(i + text.length, i + text.length + 8))
        push(text, keyword ? "keyword" : called && family !== FAMILIES["plain"] ? "fn" : undefined)
        i += text.length
        continue
      }
    }
    push(code[i]!)
    i++
  }
  if (plain) tokens.push({ text: plain })
  return tokens
}
