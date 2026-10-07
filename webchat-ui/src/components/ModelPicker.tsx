import { CheckIcon, ChevronDownIcon, CloudIcon, CpuIcon, GaugeIcon } from "lucide-react"
import { useMemo, useState, type FC } from "react"
import { ModelSelector, type ModelOption } from "@/components/model-selector.aui"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { MODE_LABELS, MODE_ORDER, MODE_SHORT_LABELS, cloudValue, effortName, localValue, modeValue, pickEffort } from "@/models"
import { cn } from "@/lib/utils"
import { useJarvis } from "@/useJarvis"

// The pickers in the message box, left to right: who answers (this PC, Codex or Claude), which model of
// that one, and for a cloud model that has effort levels, how hard it thinks.

export const ModelPicker: FC = () => (
  <>
    <ModeSwitch />
    <ModelMenu />
    <EffortMenu />
  </>
)

const ModeSwitch: FC = () => {
  const { mode, actions } = useJarvis()
  const active = mode?.mode
  const modes = MODE_ORDER.filter((m) => m === "local" || (mode?.enabled ?? []).includes(m))
  if (!active || modes.length < 2) return null

  return (
    <div role="radiogroup" aria-label="Who answers" className="bg-muted/60 flex items-center gap-0.5 rounded-lg p-0.5">
      {modes.map((m) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={m === active}
          title={m === "local" ? "Answers on this PC" : `${MODE_LABELS[m] ?? m} (sends requests to the cloud)`}
          onClick={() => {
            if (m !== active) void actions.chooseModel(modeValue(m))
          }}
          className={cn(
            "focus-visible:ring-ring/50 flex h-7 items-center gap-1 rounded-md px-2.5 text-xs font-medium whitespace-nowrap outline-none focus-visible:ring-1",
            m === active
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {m === "local" ? <CpuIcon className="size-3.5" aria-hidden /> : <CloudIcon className="size-3.5" aria-hidden />}
          {MODE_SHORT_LABELS[m] ?? m}
        </button>
      ))}
    </div>
  )
}

// The models of the active mode only: the local models, or what Claude or Codex reports for this account.
const ModelMenu: FC = () => {
  const { mode, modelState, models, cloud, cloudModels, actions } = useJarvis()
  const active = mode?.mode
  const isCloud = !!active && active !== "local"

  const { options, value } = useMemo(() => {
    if (!isCloud) {
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
      return { options: local, value: modelState.current ? localValue(modelState.current) : undefined }
    }
    if (!cloud?.ready) return { options: [] as ModelOption[], value: undefined }
    const listed: ModelOption[] = cloudModels.map((m) => ({
      id: cloudValue(m.id),
      name: m.name,
      ...(m.description ? { description: m.description } : {}),
      icon: <CloudIcon />,
    }))
    if (cloud.model && !cloudModels.some((m) => m.id === cloud.model)) {
      listed.unshift({ id: cloudValue(cloud.model), name: cloud.model, icon: <CloudIcon />, disabled: true })
    }
    return { options: listed, value: cloud.model ? cloudValue(cloud.model) : undefined }
  }, [cloud, cloudModels, isCloud, modelState, models])

  if (isCloud && !cloud?.ready) {
    return (
      <span className="text-muted-foreground flex h-8 items-center gap-1.5 px-2.5 text-xs" aria-live="polite">
        Checking the {MODE_LABELS[active] ?? active} models…
      </span>
    )
  }
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
          <ModelSelector.Group heading={isCloud ? `${MODE_LABELS[active] ?? active} models` : "On this PC"}>
            {options.map((model) => (
              <ModelSelector.Item key={model.id} model={model} />
            ))}
          </ModelSelector.Group>
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
      <PopoverContent align="start" side="top" sideOffset={6} className="w-64 rounded-xl p-1.5">
        <div role="menu" aria-label="Effort" className="flex flex-col gap-0.5">
          <p className="text-muted-foreground px-2.5 pt-1 pb-1.5 text-xs font-medium">How hard {model.name} thinks</p>
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
