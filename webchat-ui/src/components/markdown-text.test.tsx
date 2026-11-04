// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { MarkdownContent } from "@/components/markdown-text"
import { TooltipProvider } from "@/components/ui/tooltip"

// How a reply's Markdown is drawn. A reply is model output, so it is drawn as text and elements, never as HTML, and
// nothing in it can make the page fetch anything.

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
})
