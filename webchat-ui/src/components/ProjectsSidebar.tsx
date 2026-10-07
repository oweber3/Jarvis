import { ThreadListPrimitive, useAuiState } from "@assistant-ui/react"
import { ChevronDownIcon, ChevronRightIcon, FolderPlusIcon, PencilIcon, PlusIcon, SearchIcon, Trash2Icon } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type FC } from "react"
import { ThreadListItem, ThreadListNew } from "@/components/thread-list.aui"
import { TooltipIconButton } from "@/components/tooltip-icon-button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { chatTitle } from "@/conversion"
import { useJarvis } from "@/useJarvis"
import type { Project } from "@/api"

// Chats grouped by project. assistant-ui lists threads flat; this groups the same threads (by index into
// the runtime's list) under the owner's projects, with the unfiled ones below.

const COLLAPSED_KEY = "jarvis-webchat-collapsed-projects"

function readCollapsed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

function writeCollapsed(ids: Set<string>) {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...ids]))
  } catch {
    /* storage can be blocked; the groups just open again next time */
  }
}

export const ProjectsSidebar: FC = () => {
  const { library, actions } = useJarvis()
  const threadIds = useAuiState((s) => s.threads.threadIds)
  const [query, setQuery] = useState("")
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed)
  const [adding, setAdding] = useState(false)

  const toggle = (id: string) =>
    setCollapsed((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      writeCollapsed(next)
      return next
    })

  const indexById = useMemo(() => new Map(threadIds.map((id, index) => [id, index])), [threadIds])
  const needle = query.trim().toLowerCase()
  const visible = useMemo(
    () => library.chats.filter((c) => !needle || chatTitle(c).toLowerCase().includes(needle)),
    [library.chats, needle],
  )
  const unfiled = visible.filter((c) => c.project_id === null)

  return (
    <nav aria-label="Chats and projects" className="flex h-full min-h-0 flex-col gap-2 p-2">
      <ThreadListPrimitive.Root className="flex flex-col gap-1">
        <ThreadListNew />
      </ThreadListPrimitive.Root>

      <div className="relative px-0.5">
        <SearchIcon className="text-muted-foreground pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2" />
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search chats"
          placeholder="Search chats"
          className="h-8 ps-8 text-sm"
        />
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pe-1">
        {library.projects.map((project) => {
          const chats = visible.filter((c) => c.project_id === project.id)
          if (needle && chats.length === 0) return null
          const isCollapsed = collapsed.has(project.id) && !needle
          return (
            <section key={project.id} aria-label={project.name} className="flex flex-col gap-0.5">
              <ProjectHeader project={project} count={chats.length} collapsed={isCollapsed} onToggle={() => toggle(project.id)} />
              {!isCollapsed &&
                chats.map((chat) => <ChatRow key={chat.id} index={indexById.get(chat.id)} />)}
              {!isCollapsed && chats.length === 0 && (
                <p className="text-muted-foreground px-6 py-1 text-xs">No chats yet</p>
              )}
            </section>
          )
        })}

        {(unfiled.length > 0 || library.projects.length === 0) && (
          <section aria-label="Chats without a project" className="flex flex-col gap-0.5">
            {library.projects.length > 0 && (
              <h3 className="text-muted-foreground px-2.5 pt-3 pb-1 text-xs font-medium">No project</h3>
            )}
            {unfiled.map((chat) => (
              <ChatRow key={chat.id} index={indexById.get(chat.id)} />
            ))}
          </section>
        )}
        {needle && visible.length === 0 && <p className="text-muted-foreground px-2.5 py-4 text-sm">No chats found</p>}
      </div>

      <div className="border-t pt-2">
        {adding ? (
          <NameInput
            label="Project name"
            initial=""
            onSubmit={async (name) => {
              await actions.createProject(name)
              setAdding(false)
            }}
            onCancel={() => setAdding(false)}
          />
        ) : (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="text-muted-foreground hover:bg-muted hover:text-foreground flex h-8 w-full items-center gap-2 rounded-md px-2.5 text-sm transition-colors"
          >
            <FolderPlusIcon className="size-4" />
            New project
          </button>
        )}
      </div>
    </nav>
  )
}

const ChatRow: FC<{ index: number | undefined }> = ({ index }) =>
  index === undefined ? null : <ThreadListPrimitive.ItemByIndex index={index} components={{ ThreadListItem }} />

const ProjectHeader: FC<{ project: Project; count: number; collapsed: boolean; onToggle: () => void }> = ({
  project,
  count,
  collapsed,
  onToggle,
}) => {
  const { actions } = useJarvis()
  const [renaming, setRenaming] = useState(false)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    if (!confirming) return
    const timer = window.setTimeout(() => setConfirming(false), 4000)
    return () => window.clearTimeout(timer)
  }, [confirming])

  if (renaming) {
    return (
      <NameInput
        label="Project name"
        initial={project.name}
        onSubmit={async (name) => {
          if (name !== project.name) await actions.renameProject(project.id, name)
          setRenaming(false)
        }}
        onCancel={() => setRenaming(false)}
      />
    )
  }

  const Chevron = collapsed ? ChevronRightIcon : ChevronDownIcon
  return (
    <div className="group hover:bg-muted/60 flex h-8 items-center rounded-md pe-1">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex h-full min-w-0 flex-1 items-center gap-1.5 rounded-md px-2 text-start text-sm font-medium outline-none focus-visible:ring-1 focus-visible:ring-ring/50"
      >
        <Chevron className="text-muted-foreground size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.name}</span>
        <span className="text-muted-foreground ms-auto shrink-0 text-xs font-normal">{count}</span>
      </button>
      <div className={cn("flex shrink-0 items-center opacity-0 group-focus-within:opacity-100 group-hover:opacity-100", confirming && "opacity-100")}>
        <TooltipIconButton tooltip="New chat in this project" className="size-6" onClick={() => void actions.newChat(project.id)}>
          <PlusIcon />
        </TooltipIconButton>
        <TooltipIconButton tooltip="Rename project" className="size-6" onClick={() => setRenaming(true)}>
          <PencilIcon />
        </TooltipIconButton>
        {confirming ? (
          <button
            type="button"
            className="bg-destructive/15 text-destructive hover:bg-destructive/25 ms-1 h-6 rounded px-2 text-xs"
            onClick={() => void actions.deleteProject(project.id)}
          >
            Delete? Chats stay
          </button>
        ) : (
          <TooltipIconButton tooltip="Delete project" className="size-6" onClick={() => setConfirming(true)}>
            <Trash2Icon />
          </TooltipIconButton>
        )}
      </div>
    </div>
  )
}

const NameInput: FC<{
  label: string
  initial: string
  onSubmit: (name: string) => Promise<void>
  onCancel: () => void
}> = ({ label, initial, onSubmit, onCancel }) => {
  const [value, setValue] = useState(initial)
  const settled = useRef(false)

  const commit = () => {
    if (settled.current) return
    const name = value.trim()
    if (!name) return cancel()
    settled.current = true
    void onSubmit(name)
  }
  const cancel = () => {
    if (settled.current) return
    settled.current = true
    onCancel()
  }

  return (
    <Input
      autoFocus
      aria-label={label}
      placeholder={label}
      value={value}
      className="h-8 text-sm"
      onChange={(event) => setValue(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault()
          commit()
        } else if (event.key === "Escape") {
          event.preventDefault()
          cancel()
        }
      }}
    />
  )
}
