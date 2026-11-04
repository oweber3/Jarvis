import { describe, expect, it } from "vitest"
import { highlight, MAX_HIGHLIGHT_CHARS } from "@/highlight"

// Code in a reply is coloured by a small scanner of its own: comments, strings, numbers, keywords and called names.

const kinds = (code: string, language: string) =>
  highlight(code, language)
    .filter((t) => t.kind)
    .map((t) => `${t.kind}:${t.text}`)

describe("highlight", () => {
  it("never changes the code: the pieces put together are the code exactly", () => {
    const code = "def total(values):\n    # skip the gaps\n    return sum(v for v in values if v is not None)  # 'done'\n"
    expect(highlight(code, "python").map((t) => t.text).join("")).toBe(code)
  })

  it("finds Python's keywords, strings, comments, numbers and called names", () => {
    expect(kinds("def f(x):\n    return 'a' + 2  # note", "python")).toEqual([
      "keyword:def",
      "fn:f",
      "keyword:return",
      "string:'a'",
      "number:2",
      "comment:# note",
    ])
  })

  it("reads the C family's comments and template strings", () => {
    expect(kinds("const a = `x ${1}` // done\n/* block */ if (a) run()", "ts")).toEqual([
      "keyword:const",
      "string:`x ${1}`",
      "comment:// done",
      "comment:/* block */",
      "keyword:if",
      "fn:run",
    ])
  })

  it("does not take a # for a comment in a language where it is none", () => {
    expect(kinds("const colour = '#fff'; a #b", "js")).toEqual(["keyword:const", "string:'#fff'"])
  })

  it("colours JSON's values", () => {
    expect(kinds('{"on": true, "n": 3.5, "x": null}', "json")).toEqual([
      'string:"on"',
      "keyword:true",
      'string:"n"',
      "number:3.5",
      'string:"x"',
      "keyword:null",
    ])
  })

  it("knows SQL's keywords whatever their case, and its comments", () => {
    expect(kinds("SELECT name FROM t -- all\nwhere id = 1", "sql")).toEqual([
      "keyword:SELECT",
      "keyword:FROM",
      "comment:-- all",
      "keyword:where",
      "number:1",
    ])
  })

  it("leaves a language it does not know with strings and numbers only", () => {
    expect(kinds("set x to \"hello\" and 42 # maybe", "")).toEqual(['string:"hello"', "number:42"])
  })

  it("does not colour very long code, to keep the page quick", () => {
    const code = "x = 1\n".repeat(Math.ceil(MAX_HIGHLIGHT_CHARS / 6) + 1)
    expect(highlight(code, "python")).toEqual([{ text: code }])
  })
})
