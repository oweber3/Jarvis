import { describe, expect, it } from "vitest"
import type { CloudModel } from "@/api"
import { chatValue, cloudValue, currentValue, describeValue, effortName, localValue, modeValue, pickEffort } from "@/models"

const effort = (id: string, isDefault = false) => ({ id, is_default: isDefault, description: "" })
const model = (id: string, efforts: string[], defaultEffort?: string): CloudModel => ({
  id,
  name: id.toUpperCase(),
  is_default: false,
  efforts: efforts.map((e) => effort(e, e === defaultEffort)),
})

describe("effortName", () => {
  it("names well-known levels the way people say them", () => {
    expect(effortName("xhigh")).toBe("Extra high")
    expect(effortName("max")).toBe("Max")
    expect(effortName("low")).toBe("Low")
  })

  it("makes any other level readable", () => {
    expect(effortName("very-deep_thinking")).toBe("Very deep thinking")
  })
})

describe("pickEffort", () => {
  const sonnet = model("sonnet", ["low", "medium", "high"], "medium")

  it("keeps the current effort when the new model offers it", () => {
    expect(pickEffort(sonnet, "high")).toBe("high")
  })

  it("falls back to the model's default, then its first level", () => {
    expect(pickEffort(sonnet, "xhigh")).toBe("medium")
    expect(pickEffort(model("m", ["high", "max"]), "low")).toBe("high")
  })

  it("is undefined for a model with no effort levels or no model", () => {
    expect(pickEffort(model("haiku", []), "low")).toBeUndefined()
    expect(pickEffort(undefined, "low")).toBeUndefined()
  })
})

describe("picker values", () => {
  it("tell a local model, a cloud model and a reply mode apart", () => {
    expect(localValue("gemma4:12b")).toBe("local:gemma4:12b")
    expect(cloudValue("sonnet")).toBe("cloud:sonnet")
    expect(modeValue("claude")).toBe("mode:claude")
  })

  it("follow what is in use", () => {
    expect(currentValue("local", "gemma4:12b", null)).toBe("local:gemma4:12b")
    expect(currentValue("claude", null, { mode: "claude", model: "sonnet", effort: "low", ready: true })).toBe("cloud:sonnet")
  })

  it("stay on the mode while the bridge has not reported its models", () => {
    expect(currentValue("claude", null, { mode: "claude", model: "sonnet", effort: "low", ready: false })).toBe("mode:claude")
  })

  it("are undefined with nothing known", () => {
    expect(currentValue(undefined, null, null)).toBeUndefined()
  })

  it("remember what a chat last used, without a cloud model (only the mode is compared)", () => {
    expect(chatValue("local", "gemma4:12b")).toBe("local:gemma4:12b")
    expect(chatValue("claude", "sonnet")).toBe("mode:claude")
    expect(chatValue("", "")).toBeUndefined()
  })

  it("are described in words", () => {
    expect(describeValue("mode:codex", [])).toBe("ChatGPT (Codex)")
    expect(describeValue("local:gemma4:12b", [{ id: "gemma4:12b", name: "Gemma 4 12B" }])).toBe("Gemma 4 12B")
    expect(describeValue("local:unknown", [])).toBe("unknown")
  })
})
