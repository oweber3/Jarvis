import { describe, expect, it } from "vitest"
import type { ToolStep } from "@/api"
import { expandedLines, stepEnding, stepFromArgs, stepTone, stepsSummary } from "@/steps"

const step = (over: Partial<ToolStep> = {}): ToolStep => ({
  tool: "systemVolume",
  status: "done",
  label: "Set the volume to 30%",
  summary: null,
  ...over,
})

describe("expandedLines", () => {
  it("says how the step ended, then what it found", () => {
    expect(expandedLines(step({ summary: "3 results" }))).toEqual(["Done", "3 results"])
  })

  it("always has something to show when opened", () => {
    expect(expandedLines(step())).toEqual(["Done"])
  })

  it.each([
    ["failed", "Didn't work"],
    ["declined", "Not allowed"],
    ["waiting", "Waiting for your confirmation"],
  ] as const)("names a %s step", (status, text) => {
    expect(expandedLines(step({ status }))[0]).toBe(text)
  })

  it("does not break on a status from a newer Jarvis", () => {
    expect(expandedLines(step({ status: "paused" as ToolStep["status"] }))).toEqual(["paused"])
  })
})

describe("stepTone", () => {
  it("calls a failed or refused step a problem, a waiting step pending, the rest fine", () => {
    expect(stepTone(step({ status: "done" }))).toBe("ok")
    expect(stepTone(step({ status: "failed" }))).toBe("problem")
    expect(stepTone(step({ status: "declined" }))).toBe("problem")
    expect(stepTone(step({ status: "waiting" }))).toBe("pending")
  })
})

describe("stepFromArgs", () => {
  it("reads back what the conversion put in a tool-call part", () => {
    expect(stepFromArgs({ label: "Opened Word", status: "done", summary: "ok" })).toEqual({
      label: "Opened Word",
      status: "done",
      summary: "ok",
    })
  })

  it("survives missing or odd arguments", () => {
    expect(stepFromArgs(undefined)).toEqual({ label: "Step", status: "done", summary: null })
    expect(stepFromArgs({ label: 3, summary: "   " })).toEqual({ label: "Step", status: "done", summary: null })
  })
})

describe("stepEnding", () => {
  it("says nothing for a step that worked: its tick says it", () => {
    expect(stepEnding(step({ status: "done" }))).toBeNull()
  })

  it.each([
    ["failed", "Didn't work"],
    ["declined", "Not allowed"],
    ["waiting", "Waiting for your confirmation"],
  ] as const)("names a %s step beside its label", (status, text) => {
    expect(stepEnding(step({ status }))).toBe(text)
  })
})

describe("stepsSummary", () => {
  it("counts the steps of a reply", () => {
    expect(stepsSummary([step(), step(), step(), step()])).toBe("4 steps")
  })

  it("says when something did not go as asked", () => {
    expect(stepsSummary([step(), step({ status: "failed" }), step({ status: "declined" }), step()])).toBe(
      "4 steps · 2 didn't work or weren't allowed",
    )
    expect(stepsSummary([step(), step(), step(), step({ status: "waiting" })])).toBe(
      "4 steps · waiting for your confirmation",
    )
  })
})
