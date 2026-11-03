import { describe, expect, it } from "vitest"
import { statusParts } from "@/phone/statusLine"

const texts = (input: Parameters<typeof statusParts>[0]) => statusParts(input).map((part) => part.text)

describe("the status line", () => {
  it("shows the connection, who answers and the PC's load", () => {
    expect(texts({ reachable: true, ready: true, mode: "local", pc: { cpu: 12.4, ram: 40.6 } })).toEqual([
      "Connected",
      "Local",
      "CPU 12%",
      "RAM 41%",
    ])
  })

  it("names the cloud modes as the page does", () => {
    expect(texts({ reachable: true, ready: true, mode: "claude", pc: null })[1]).toMatch(/claude/i)
  })

  it("says so, and shows nothing else, when the PC cannot be reached", () => {
    expect(statusParts({ reachable: false, ready: true, mode: "local", pc: { cpu: 1, ram: 1 } })).toEqual([
      { text: "Can't reach the PC", tone: "warn" },
    ])
  })

  it("says when Jarvis is still starting", () => {
    expect(statusParts({ reachable: true, ready: false, mode: undefined, pc: undefined })).toEqual([
      { text: "Jarvis is starting", tone: "warn" },
    ])
  })

  it("leaves out the load when the PC does not report it", () => {
    expect(texts({ reachable: true, ready: true, mode: "local", pc: undefined })).toEqual(["Connected", "Local"])
  })
})
