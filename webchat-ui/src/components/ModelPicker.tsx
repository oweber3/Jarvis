import { CloudIcon, CpuIcon } from "lucide-react"
import { useMemo, type FC } from "react"
import { ModelSelector, type ModelOption } from "@/components/model-selector.aui"
import { useJarvis } from "@/useJarvis"

export const MODE_LABELS: Record<string, string> = {
  local: "Local",
  codex: "ChatGPT (Codex)",
  claude: "Claude",
}

// The picker's option ids: a reply mode ("mode:claude") or a local model on this PC ("local:gemma4:12b").
export const modeValue = (mode: string) => `mode:${mode}`
export const localValue = (model: string) => `local:${model}`

export function currentValue(mode: string | undefined, model: string | null): string | undefined {
  if (!mode) return undefined
  return mode === "local" ? (model ? localValue(model) : undefined) : modeValue(mode)
}

// What a chat last used, as a picker value (a chat that never ran has none).
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

export const ModelPicker: FC = () => {
  const { mode, modelState, models, actions } = useJarvis()

  const { options, local, cloud } = useMemo(() => {
    const localOptions: ModelOption[] = models.map((m) => ({
      id: localValue(m.id),
      name: m.name,
      description: m.installed ? "Runs on this PC" : "Not installed",
      icon: <CpuIcon />,
      disabled: !m.installed || !modelState.switchable,
    }))
    if (modelState.current && !models.some((m) => m.id === modelState.current)) {
      localOptions.unshift({
        id: localValue(modelState.current),
        name: modelState.current,
        description: "Runs on this PC",
        icon: <CpuIcon />,
        disabled: !modelState.switchable,
      })
    }
    const cloudOptions: ModelOption[] = (mode?.enabled ?? [])
      .filter((m) => m !== "local")
      .map((m) => ({
        id: modeValue(m),
        name: MODE_LABELS[m] ?? m,
        description: "Sends requests to the cloud",
        icon: <CloudIcon />,
      }))
    return { options: [...localOptions, ...cloudOptions], local: localOptions, cloud: cloudOptions }
  }, [mode, modelState, models])

  const value = currentValue(mode?.mode, modelState.current)
  if (options.length === 0) return null

  return (
    <ModelSelector.Root
      models={options}
      {...(value ? { value } : {})}
      onValueChange={(next) => void actions.chooseModel(next)}
    >
      <ModelSelector.Trigger variant="ghost" size="sm" aria-label="Model" className="max-w-56" />
      <ModelSelector.Content align="start" side="top">
        <ModelSelector.List>
          {local.length > 0 && (
            <ModelSelector.Group heading="On this PC">
              {local.map((model) => (
                <ModelSelector.Item key={model.id} model={model} />
              ))}
            </ModelSelector.Group>
          )}
          {cloud.length > 0 && (
            <>
              <ModelSelector.Separator />
              <ModelSelector.Group heading="Cloud (needs your permission in Settings)">
                {cloud.map((model) => (
                  <ModelSelector.Item key={model.id} model={model} />
                ))}
              </ModelSelector.Group>
            </>
          )}
        </ModelSelector.List>
      </ModelSelector.Content>
    </ModelSelector.Root>
  )
}
