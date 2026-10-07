import { CheckIcon, ChevronDownIcon, CloudIcon, CpuIcon, GaugeIcon } from "lucide-react"
import { useMemo, useState, type FC } from "react"
import { ModelSelector, type ModelOption } from "@/components/model-selector.aui"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { MODE_LABELS, cloudValue, currentValue, effortName, localValue, modeValue, pickEffort } from "@/models"
import { cn } from "@/lib/utils"
import { useJarvis } from "@/useJarvis"

// The picker in the message box: which model answers (on this PC, or Claude or ChatGPT in the cloud) and,
// for a cloud model that has effort levels, how hard it thinks.

export const ModelPicker: FC = () => (
  <>
    <ModelMenu />
    <EffortMenu />
  </>
)

const ModelMenu: FC = () => {
  const { mode, modelState, models, cloud, cloudModels, actions } = useJarvis()

  const { options, groups } = useMemo(() => {
    const local: ModelOption[] = models.map((m) => ({
      id: localValue(m.id),
      name: m.name,
      description: m.installed ? "Runs on this PC" : "Not installed",
      icon: <CpuIcon />,
      disabled: !m.installed || !modelState.switchable,
    }))
    if (modelState.current && !models.some((m) => m.id === modelState.current)) {
      local.unshift({
        id: localValue(modelState.current),
        name: modelState.current,
        description: "Runs on this PC",
        icon: <CpuIcon />,
        disabled: !modelState.switchable,
      })
    }

    const active = mode?.mode && mode.mode !== "local" ? mode.mode : undefined
    const activeLabel = active ? (MODE_LABELS[active] ?? active) : ""
    const activeModels: ModelOption[] = []
    if (active && cloud?.ready) {
      for (const m of cloudModels) activeModels.push({ id: cloudValue(m.id), name: m.name, icon: <CloudIcon /> })
      if (cloud.model && !cloudModels.some((m) => m.id === cloud.model)) {
        activeModels.unshift({ id: cloudValue(cloud.model), name: cloud.model, icon: <CloudIcon />, disabled: true })
      }
    } else if (active) {
      activeModels.push({
        id: modeValue(active),
        name: activeLabel,
        description: "Checking the available models…",
        icon: <CloudIcon />,
        disabled: true,
      })
    }

    const others: ModelOption[] = (mode?.enabled ?? [])
      .filter((m) => m !== "local" && m !== active)
      .map((m) => ({
        id: modeValue(m),
        name: MODE_LABELS[m] ?? m,
        description: "Switch to it. Sends requests to the cloud",
        icon: <CloudIcon />,
      }))

    return {
      options: [...local, ...activeModels, ...others],
      groups: [
        { heading: "On this PC", items: local },
        { heading: `${activeLabel} (cloud)`, items: activeModels },
        { heading: "Other cloud modes (allowed in Settings)", items: others },
      ].filter((g) => g.items.length > 0),
    }
  }, [cloud, cloudModels, mode, modelState, models])

  const value = currentValue(mode?.mode, modelState.current, cloud)
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
          {groups.map((group, index) => (
            <div key={group.heading}>
              {index > 0 && <ModelSelector.Separator />}
              <ModelSelector.Group heading={group.heading}>
                {group.items.map((model) => (
                  <ModelSelector.Item key={model.id} model={model} />
                ))}
              </ModelSelector.Group>
            </div>
          ))}
        </ModelSelector.List>
      </ModelSelector.Content>
    </ModelSelector.Root>
  )
}

// A separate control beside the model, as in most chat apps. It exists only for a cloud model that
// reports effort levels; switching model keeps the effort when the new model offers it.
const EffortMenu: FC = () => {
  const { cloud, cloudModels, actions } = useJarvis()
  const [open, setOpen] = useState(false)
  const model = cloudModels.find((m) => m.id === cloud?.model)
  if (!cloud?.ready || !model || model.efforts.length === 0) return null
  const selected = pickEffort(model, cloud.effort)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        aria-label={`Effort: ${selected ? effortName(selected) : "default"}`}
        className="text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring/50 flex h-8 items-center gap-1.5 rounded-md px-2.5 text-xs whitespace-nowrap outline-none focus-visible:ring-1"
      >
        <GaugeIcon className="size-3.5" aria-hidden />
        <span>{selected ? effortName(selected) : "Effort"}</span>
        <ChevronDownIcon className="size-3.5 opacity-50" aria-hidden />
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-60 rounded-xl p-1.5">
        <div role="menu" aria-label="Effort" className="flex flex-col gap-0.5">
          <p className="text-muted-foreground px-2.5 pt-1 pb-1.5 text-xs font-medium">How hard it thinks</p>
          {model.efforts.map((effort) => (
            <button
              key={effort.id}
              type="button"
              role="menuitemradio"
              aria-checked={effort.id === selected}
              onClick={() => {
                setOpen(false)
                void actions.chooseCloudModel(model.id, effort.id)
              }}
              className={cn(
                "hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent flex items-start gap-2 rounded-lg px-2.5 py-1.5 text-start text-sm outline-none",
                effort.id === selected && "bg-accent/60",
              )}
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="font-medium">{effortName(effort.id)}</span>
                {effort.description && <span className="text-muted-foreground text-xs leading-snug">{effort.description}</span>}
              </span>
              {effort.id === selected && <CheckIcon className="mt-0.5 size-4 shrink-0" aria-hidden />}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}
