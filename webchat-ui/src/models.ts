import type { CloudModel, CloudState } from "@/api"

// Pure helpers for the model picker: what the options are called and which one is in use.

export const MODE_LABELS: Record<string, string> = {
  local: "Local",
  codex: "ChatGPT (Codex)",
  claude: "Claude",
}

const WELL_KNOWN_EFFORTS: Record<string, string> = { xhigh: "Extra high", max: "Max", minimal: "Minimal", none: "None" }

// The runtimes name their levels with ids ("xhigh"); this is what the owner reads.
export function effortName(id: string): string {
  const known = WELL_KNOWN_EFFORTS[id.toLowerCase()]
  if (known) return known
  const spaced = id.replace(/[-_]+/g, " ").trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

// The effort to use on a model: the current one when the model offers it, else its default, else its first.
export function pickEffort(model: CloudModel | undefined, preferred: string | undefined): string | undefined {
  if (!model || model.efforts.length === 0) return undefined
  return (
    model.efforts.find((e) => e.id === preferred)?.id ??
    model.efforts.find((e) => e.is_default)?.id ??
    model.efforts[0]?.id
  )
}

// The picker's option ids: a local model on this PC, a model of the active cloud mode, or a reply mode to switch to.
export const localValue = (model: string) => `local:${model}`
export const cloudValue = (model: string) => `cloud:${model}`
export const modeValue = (mode: string) => `mode:${mode}`

export function currentValue(
  mode: string | undefined,
  localModel: string | null,
  cloud: CloudState | null,
): string | undefined {
  if (!mode) return undefined
  if (mode === "local") return localModel ? localValue(localModel) : undefined
  return cloud && cloud.ready && cloud.model ? cloudValue(cloud.model) : modeValue(mode)
}

// What a chat last used. Only the reply mode (and a local model) is compared, never a cloud model.
export function chatValue(lastMode: string, lastModel: string): string | undefined {
  if (!lastMode) return undefined
  return lastMode === "local" ? (lastModel ? localValue(lastModel) : undefined) : modeValue(lastMode)
}

export function describeValue(value: string, models: readonly { id: string; name: string }[]): string {
  const [kind, ...rest] = value.split(":")
  const target = rest.join(":")
  if (kind === "mode") return MODE_LABELS[target] ?? target
  return models.find((m) => m.id === target)?.name ?? target
}
