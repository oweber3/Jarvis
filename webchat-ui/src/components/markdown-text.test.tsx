// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { MarkdownContent } from "@/components/markdown-text"
import { TooltipProvider } from "@/components/ui/tooltip"

// How a reply's Markdown is drawn. A reply is model output, so it is drawn as text and elements, never as HTML, and
// nothing in it can make the page fetch anything.

const codes = (page: HTMLElement, selector = "code") => [...page.querySelectorAll(selector)].map((c) => c.textContent)

function draw(text: string): HTMLElement {
  const { container } = render(
    <TooltipProvider>
      <MarkdownContent text={text} />
    </TooltipProvider>,
  )
  return container
}

describe("a reply's Markdown", () => {
  afterEach(cleanup)

  it("shows text that looks like HTML as the text it is", () => {
    const page = draw("Hello <b>bold</b> and <script>alert(1)</script> <img src=x onerror=alert(1)>")
    expect(page.querySelector("b, script, img")).toBeNull()
    expect(page.textContent).toContain("<b>bold</b>")
    expect(page.textContent).toContain("<script>alert(1)</script>")
  })

  it("opens links outside the chat and drops unsafe ones", () => {
    const page = draw("[docs](https://example.org/docs) and [bad](javascript:alert(1))")
    const links = [...page.querySelectorAll("a")]
    const docs = links.find((a) => a.textContent === "docs")!
    expect(docs.getAttribute("href")).toBe("https://example.org/docs")
    expect(docs.getAttribute("target")).toBe("_blank")
    expect(docs.getAttribute("rel")).toContain("noreferrer")
    const bad = links.find((a) => a.textContent === "bad")
    expect(bad?.getAttribute("href") ?? null).toBeNull()
  })

  it("shows an image as a link to it, never fetching it", () => {
    const page = draw("![a chart](https://example.org/chart.png)")
    expect(page.querySelector("img")).toBeNull()
    expect(document.head.querySelector("link[rel='preload']")).toBeNull()
    const link = page.querySelector("a")!
    expect(link.textContent).toContain("a chart")
    expect(link.getAttribute("href")).toBe("https://example.org/chart.png")
  })

  it("draws a code block with its language and a copy button, and keeps the code exactly", () => {
    const page = draw("```python\nprint('<hi>')\n  indented\n```")
    const block = page.querySelector("[data-slot='aui_code-block']")!
    expect(block.textContent).toContain("python")
    expect(block.querySelector("pre")!.textContent).toBe("print('<hi>')\n  indented")
    expect(block.querySelector("button")!.textContent).toContain("Copy code")
  })

  it("colours the code of a block it knows the language of, keeping every character", () => {
    const page = draw("```python\ndef total(xs):\n    return sum(xs)  # all\n```")
    const code = page.querySelector("[data-slot='aui_code-block'] code")!
    expect(code.textContent).toBe("def total(xs):\n    return sum(xs)  # all")
    const coloured = [...code.querySelectorAll("span")].map((span) => span.textContent)
    expect(coloured).toEqual(["def", "total", "return", "sum", "# all"])
  })

  it("draws inline code, tables, task lists and struck-out text", () => {
    const page = draw("use `a < b`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n- [ ] todo\n\n~~gone~~")
    expect(page.querySelector("code")!.textContent).toBe("a < b")
    expect(page.querySelectorAll("table td")).toHaveLength(2)
    const boxes = [...page.querySelectorAll("input[type='checkbox']")] as HTMLInputElement[]
    expect(boxes.map((b) => b.checked)).toEqual([true, false])
    expect(page.querySelector("del")!.textContent).toBe("gone")
  })

  it("gives headings no ids of their own, so a reply cannot clash with the page", () => {
    const page = draw("# Title\n\nBody")
    expect(page.querySelector("h1")!.hasAttribute("id")).toBe(false)
  })

  it("gives footnotes no ids either, and keeps their text", () => {
    const page = draw("A claim.[^src]\n\n[^src]: Where it came from.")
    expect(page.querySelector("[id]")).toBeNull()
    expect(page.textContent).toContain("Where it came from.")
  })

  it("draws a GitHub alert as a quote with its label", () => {
    const page = draw("Before.\n\n> [!NOTE]\n> Jarvis keeps the conversation.\n\nAfter.")
    const quote = page.querySelector("blockquote")!
    expect(quote.textContent).toContain("NOTE")
    expect(quote.textContent).toContain("Jarvis keeps the conversation.")
    expect(page.textContent).toContain("After.")
  })

  it("links only web and mail addresses: anything else stays text", () => {
    const page = draw(
      [
        "[a](file:///C:/Windows/System32)",
        "[b](ms-msdt:/id%20PCWDiagnostic)",
        "[c](search-ms:query=report)",
        "[d](intent://scan/#Intent;scheme=zxing;end)",
        "[e](/api/chats)",
        "[f](vbscript:msgbox)",
        "<file:///C:/Users>",
        "[g](mailto:owner@example.org)",
        "[h](HTTPS://example.org/x)",
      ].join(" "),
    )
    const links = [...page.querySelectorAll("a")]
    expect(links.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["g", "mailto:owner@example.org"],
      ["h", "https://example.org/x"],
    ])
    for (const word of ["a", "b", "c", "d", "e", "f", "file:///C:/Users"]) expect(page.textContent).toContain(word)
  })

  it("shows an image at an address it will not link as its description", () => {
    const page = draw("![the chart](file:///C:/chart.png)")
    expect(page.querySelector("a, img")).toBeNull()
    expect(page.textContent).toContain("the chart")
  })

  it("keeps every backslash of inline code and indented code, and escapes outside code still work", () => {
    const page = draw("A share `\\\\server\\share`, a pattern `^\\d+\\.\\d+$`, \\*not emphasis\\*\n\n    C:\\\\Temp\\*.log\n")
    expect([...page.querySelectorAll("p code")].map((c) => c.textContent)).toEqual(["\\\\server\\share", "^\\d+\\.\\d+$"])
    expect(page.querySelector("pre")!.textContent).toBe("C:\\\\Temp\\*.log")
    expect(page.querySelector("em")).toBeNull()
    expect(page.textContent).toContain("*not emphasis*")
  })

  it("keeps code exactly in lists and links, and an escaped pipe in a table's code is the pipe", () => {
    const page = draw("- copy `\\\\nas\\backup`\n- see [`a\\*b`](https://example.org)\n\n| Command | Means |\n|---|---|\n| `x \\| y` | either |\n")
    expect([...page.querySelectorAll("li code")].map((c) => c.textContent)).toEqual(["\\\\nas\\backup", "a\\*b"])
    expect(page.querySelector("td code")!.textContent).toBe("x | y")
  })

  it("keeps a backslash at the end of inline code, as a Windows folder has", () => {
    const page = draw("Open `C:\\Users\\me\\` and then `D:\\` here.")
    expect(codes(page)).toEqual(["C:\\Users\\me\\", "D:\\"])
    expect(page.textContent).toBe("Open C:\\Users\\me\\ and then D:\\ here.")
  })

  it("draws each inline code from its own place, whatever else in the reply holds the same code", () => {
    // Each time the same code sits somewhere else first, differing only by a backslash.
    expect(codes(draw("Example:\n\n    echo `a\\*b`\n\nThen run `a*b` here."), "p code")).toEqual(["a*b"])
    const footnoted = draw("Claim.[^1]\n\n[^1]: Uses `x\\*y`.\n\nAnd then `x*y` in the body.")
    expect(codes(footnoted, "p code")).toEqual(["x*y"])
    expect(codes(footnoted, "footer code")).toEqual(["x\\*y"])
    expect(codes(draw("![see `a\\*b`](https://x.org/i.png) then `a*b`"))).toEqual(["a*b"])
    const unlinked = draw("[`a\\*b`][nope] and `a*b`")
    expect(codes(unlinked)).toEqual(["a*b"])
    expect(unlinked.textContent).toContain("[`a\\*b`][nope]")
    expect(codes(draw("Go to https://x.org/`p\\_q` and type `p_q`")).at(-1)).toBe("p_q")
    expect(codes(draw("---\nUse `a\\\\b` here\n---\nThen `a\\b` there"))).toEqual(["a\\b"])
  })

  it("an escaped pipe in a table row's code is the pipe, whether or not the row starts with one", () => {
    const page = draw("| A | B |\n|---|---|\nx | `a \\| b`\n| `c \\| d` | y |\n")
    expect(codes(page, "td code")).toEqual(["a | b", "c | d"])
  })

  it("a backslash before a backtick outside code still makes it a backtick", () => {
    const page = draw("Use \\`not code\\` here")
    expect(page.querySelector("code")).toBeNull()
    expect(page.textContent).toBe("Use `not code` here")
  })

  it("takes inline code from the reply's text, not from a code block that looks the same", () => {
    const page = draw("```\n`p\\.q`\n```\n\nThen `p.q` inline.")
    expect(page.querySelector("pre")!.textContent).toBe("`p\\.q`")
    expect(page.querySelector("p code")!.textContent).toBe("p.q")
  })
})
