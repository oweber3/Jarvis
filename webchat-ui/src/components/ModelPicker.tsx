import { CheckIcon, ChevronDownIcon, CloudIcon, CpuIcon, GaugeIcon } from "lucide-react"
import { DropdownMenu } from "radix-ui"
import { useMemo, type FC, type ReactNode } from "react"
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

type Option = { id: string; name: string; description?: string; icon: ReactNode; disabled?: boolean }

const MENU_CONTENT =
  "bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 z-50 max-h-[min(24rem,var(--radix-dropdown-menu-content-available-height))] min-w-56 overflow-y-auto rounded-xl border p-1.5 shadow-lg"
const MENU_ITEM =
  "data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground flex cursor-pointer items-start gap-2 rounded-lg px-2.5 py-1.5 text-start text-sm outline-none select-none data-[disabled]:cursor-not-allowed data-[disabled]:opacity-60"
const TRIGGER =
  "text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring/50 data-[state=open]:bg-accent flex h-8 min-w-0 items-center gap-1.5 rounded-md px-2.5 text-xs whitespace-nowrap outline-none focus-visible:ring-2"

// A menu of options, one of them chosen: keyboard navigation, typeahead and the radio roles come from Radix.
const ChoiceMenu: FC<{
  label: string
  heading: string
  value: string | undefined
  options: Option[]
  trigger: ReactNode
  onChoose: (id: string) => void
}> = ({ label, heading, value, options, trigger, onChoose }) => (
  <DropdownMenu.Root>
    <DropdownMenu.Trigger aria-label={label} className={cn(TRIGGER, "max-w-56")}>
      {trigger}
      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" aria-hidden />
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content side="top" align="start" sideOffset={6} className={MENU_CONTENT}>
        <DropdownMenu.Label className="text-muted-foreground px-2.5 pt-1 pb-1.5 text-xs font-medium">{heading}</DropdownMenu.Label>
        <DropdownMenu.RadioGroup value={value ?? ""} onValueChange={(next) => next !== value && onChoose(next)}>
          {options.map((option) => (
            <DropdownMenu.RadioItem key={option.id} value={option.id} disabled={option.disabled} className={MENU_ITEM}>
              <span className="text-muted-foreground mt-0.5 shrink-0 [&_svg]:size-4" aria-hidden>
                {option.icon}
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="font-medium">{option.name}</span>
                {option.description && <span className="text-muted-foreground text-xs leading-snug">{option.description}</span>}
              </span>
              <DropdownMenu.ItemIndicator className="mt-0.5 shrink-0">
                <CheckIcon className="size-4" aria-hidden />
              </DropdownMenu.ItemIndicator>
            </DropdownMenu.RadioItem>
          ))}
        </DropdownMenu.RadioGroup>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>
)

// The models of the active mode only: the local models, or what Claude or Codex reports for this account.
const ModelMenu: FC = () => {
  const { mode, modelState, models, cloud, cloudModels, actions } = useJarvis()
  const active = mode?.mode
  const isCloud = !!active && active !== "local"

  const { options, value } = useMemo(() => {
    if (!isCloud) {
      const local: Option[] = models.map((m) => ({
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
    if (!cloud?.ready) return { options: [] as Option[], value: undefined }
    const listed: Option[] = cloudModels.map((m) => ({
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
  const current = options.find((o) => o.id === value)

  return (
    <ChoiceMenu
      label={`Model: ${current?.name ?? "none chosen"}`}
      heading={isCloud ? `${MODE_LABELS[active] ?? active} models` : "On this PC"}
      value={value}
      options={options}
      onChoose={(next) => void actions.chooseModel(next)}
      trigger={
        <>
          <span className="shrink-0 [&_svg]:size-3.5" aria-hidden>
            {current?.icon ?? <CpuIcon />}
          </span>
          <span className="truncate">{current?.name ?? "Model"}</span>
        </>
      }
    />
  )
}

// A separate control beside the model, as in most chat apps. It exists only for a cloud model that
// reports effort levels; switching model keeps the effort when the new model offers it.
const EffortMenu: FC = () => {
  const { cloud, cloudModels, actions } = useJarvis()
  const model = cloudModels.find((m) => m.id === cloud?.model)
  if (!cloud?.ready || !model || model.efforts.length === 0) return null
  const selected = pickEffort(model, cloud.effort)

  return (
    <ChoiceMenu
      label={`Effort: ${selected ? effortName(selected) : "default"}`}
      heading={`How hard ${model.name} thinks`}
      value={selected}
      options={model.efforts.map((effort) => ({
        id: effort.id,
        name: effortName(effort.id),
        ...(effort.description ? { description: effort.description } : {}),
        icon: <GaugeIcon />,
      }))}
      onChoose={(next) => void actions.chooseCloudModel(model.id, next)}
      trigger={
        <>
          <GaugeIcon className="size-3.5 shrink-0" aria-hidden />
          <span>{selected ? effortName(selected) : "Effort"}</span>
        </>
      }
    />
  )
}
