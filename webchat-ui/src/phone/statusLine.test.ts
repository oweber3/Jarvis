import { describe, expect, it } from "vitest"
import { MODE_LABELS } from "@/models"
import { connection, statusDetails } from "@/phone/statusLine"

const base = { reachable: true, ready: true, busy: false }

describe("the connection dot", () => {
  it("is green when the PC is connected and free", () => {
    expect(connection(base)).toEqual({ tone: "good", label: "Connected" })
  })

  it("is amber while Jarvis is busy or still starting", () => {
    expect(connection({ ...base, busy: true }).tone).toBe("warn")
    expect(connection({ ...base, ready: false }).tone).toBe("warn")
    expect(connection({ ...base, busy: true }).label).not.toBe(connection({ ...base, ready: false }).label)
  })

  it("is red when the PC cannot be reached, whatever else was last heard", () => {
    expect(connection({ reachable: false, ready: true, busy: true })).toEqual({ tone: "bad", label: "Can't reach the PC" })
  })
})

describe("the status details", () => {
  it("show the connection, who answers and the PC's load", () => {
    expect(statusDetails({ ...base, mode: "local", pc: { cpu: 12.4, ram: 40.6 } })).toEqual([
      { term: "Connection", text: "Connected" },
      { term: "Replies", text: MODE_LABELS["local"] },
      { term: "PC CPU", text: "12%" },
      { term: "PC memory", text: "41%" },
    ])
  })

  it("name the cloud modes as the page does", () => {
    expect(statusDetails({ ...base, mode: "claude", pc: null })[1]).toEqual({ term: "Replies", text: MODE_LABELS["claude"] })
  })

  it("show only the connection when the PC cannot be reached", () => {
    expect(statusDetails({ ...base, reachable: false, mode: "local", pc: { cpu: 1, ram: 1 } })).toEqual([
      { term: "Connection", text: "Can't reach the PC" },
    ])
  })

  it("say when Jarvis is busy or still starting", () => {
    expect(statusDetails({ ...base, busy: true, mode: "local", pc: undefined })[0]!.text).toBe(connection({ ...base, busy: true }).label)
    expect(statusDetails({ ...base, ready: false, mode: undefined, pc: undefined })).toEqual([
      { term: "Connection", text: connection({ ...base, ready: false }).label },
    ])
  })

  it("leave out the load when the PC does not report it", () => {
    expect(statusDetails({ ...base, mode: "local", pc: undefined }).map((row) => row.term)).toEqual(["Connection", "Replies"])
  })
})
