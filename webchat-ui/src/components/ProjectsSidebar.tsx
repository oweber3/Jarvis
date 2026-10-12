import { ThreadListPrimitive, useAuiState } from "@assistant-ui/react"
import { ChevronDownIcon, ChevronRightIcon, FolderPlusIcon, PencilIcon, PinIcon, PlusIcon, SearchIcon, Trash2Icon } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type FC, type ReactNode } from "react"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { ThreadListItem, ThreadListNew } from "@/components/thread-list.aui"
import { TooltipIconButton } from "@/components/tooltip-icon-button"
import { Input } from "@/components/ui/input"
import { CONFIRM_WORD } from "@/confirm"
import { cn } from "@/lib/utils"
import { chatTitle } from "@/conversion"
import { MIN_SEARCH_CHARS, useMessageSearch } from "@/hooks/use-message-search"
import { highlightParts, searchRows, type SearchRow } from "@/search"
import { scrollTarget } from "@/scrollTarget"
import { arrangeChats } from "@/sidebar"
import { useJarvis } from "@/useJarvis"
import type { Chat, Project } from "@/api"

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
  const { hits, searching } = useMessageSearch(query)
  const rows = useMemo(() => searchRows(library.chats, query, hits), [library.chats, query, hits])
  const pinned = useMemo(() => library.chats.filter((c) => c.pinned), [library.chats])
  const unfiled = useMemo(() => library.chats.filter((c) => c.project_id === null), [library.chats])

  return (
    <nav aria-label="Chats and projects" className="flex h-full min-h-0 flex-col gap-2 p-2">
      <ThreadListPrimitive.Root className="flex flex-col gap-1">
        <ThreadListNew />
      </ThreadListPrimitive.Root>

      <div className="relative px-0.5">
        <SearchIcon className="text-muted-foreground pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2" />
        <Input
          type="search"
          data-chat-search
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return
            // Esc clears the search, and leaves the box when it is already empty.
            event.preventDefault()
            if (query) setQuery("")
            else event.currentTarget.blur()
          }}
          aria-label="Search chats"
          placeholder="Search chats and messages"
          title="Search chats and messages (Ctrl+K)"
          className="h-8 ps-8 text-sm"
        />
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pe-1">
        {needle ? (
          <SearchResults rows={rows} query={query} searching={searching} />
        ) : (
          <>
            {pinned.length > 0 && (
              <section aria-label="Pinned chats" className="flex flex-col gap-0.5">
                <GroupHeading icon={<PinIcon className="size-3" aria-hidden />}>Pinned</GroupHeading>
                {pinned.map((chat) => (
                  <ChatRow key={chat.id} index={indexById.get(chat.id)} />
                ))}
              </section>
            )}

            {library.projects.map((project) => {
              const all = library.chats.filter((c) => c.project_id === project.id)
              const isCollapsed = collapsed.has(project.id)
              return (
                <section key={project.id} aria-label={project.name} className="flex flex-col gap-0.5">
                  <ProjectHeader project={project} count={all.length} collapsed={isCollapsed} onToggle={() => toggle(project.id)} />
                  {!isCollapsed && <AgeGroups chats={all} indexById={indexById} />}
                  {!isCollapsed && all.length === 0 && (
                    <p className="text-muted-foreground px-6 py-1 text-xs">No chats yet</p>
                  )}
                </section>
              )
            })}

            {(unfiled.some((c) => !c.pinned) || library.projects.length === 0) && (
              <section aria-label="Chats without a project" className="flex flex-col gap-0.5">
                {library.projects.length > 0 && (
                  <h3 className="text-muted-foreground px-2.5 pt-3 pb-1 text-xs font-medium">No project</h3>
                )}
                <AgeGroups chats={unfiled} indexById={indexById} />
              </section>
            )}
          </>
        )}
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
        <DeleteEverything />
      </div>
    </nav>
  )
}

// Wipes every chat and project. Deliberately plain and a long way from "New chat", and it asks twice:
// a dialog that spells out what goes, and a word to type.
const DeleteEverything: FC = () => {
  const { library, actions } = useJarvis()
  const [open, setOpen] = useState(false)
  const chats = library.chats.length
  const projects = library.projects.length
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive focus-visible:text-destructive flex h-8 w-full items-center gap-2 rounded-md px-2.5 text-sm transition-colors"
      >
        <Trash2Icon className="size-4" />
        Delete everything
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete everything?"
        description={
          <>
            <p>
              This deletes all {chats} {chats === 1 ? "chat" : "chats"}
              {projects > 0 ? ` and ${projects} ${projects === 1 ? "project" : "projects"}` : ""}, with every message in
              them, and empties the current conversation. This cannot be undone.
            </p>
            <p>Jarvis's diary of past conversations is kept.</p>
          </>
        }
        confirmLabel="Delete everything"
        requireWord={CONFIRM_WORD}
        onConfirm={() => actions.clearHistory()}
      />
    </>
  )
}

// A group's small heading: "Pinned", "Today", "Yesterday" ...
const GroupHeading: FC<{ children: string; icon?: ReactNode }> = ({ children, icon }) => (
  <h4 className="text-muted-foreground flex items-center gap-1.5 px-2.5 pt-2 pb-0.5 text-[11px] font-medium">
    {icon}
    {children}
  </h4>
)

// The non-pinned chats of one place, under Today / Yesterday / Previous 7 days / Previous 30 days / Older.
const AgeGroups: FC<{ chats: Chat[]; indexById: Map<string, number> }> = ({ chats, indexById }) => {
  const { groups } = arrangeChats(chats)
  return (
    <>
      {groups.map((group) => (
        <div key={group.group} role="group" aria-label={group.label} className="flex flex-col gap-0.5">
          <GroupHeading>{group.label}</GroupHeading>
          {group.items.map((chat) => (
            <ChatRow key={chat.id} index={indexById.get(chat.id)} />
          ))}
        </div>
      ))}
    </>
  )
}

// Chats found by title or by what was said in them. Opening one scrolls to the message that matched.
const SearchResults: FC<{ rows: SearchRow[]; query: string; searching: boolean }> = ({ rows, query, searching }) => {
  const { actions } = useJarvis()
  if (rows.length === 0) {
    return (
      <p role="status" className="text-muted-foreground px-2.5 py-4 text-sm">
        {searching ? "Searching" : query.trim().length < MIN_SEARCH_CHARS ? "No chat titles match" : "No chats found"}
      </p>
    )
  }
  return (
    <ul aria-label="Search results" className="flex flex-col gap-0.5">
      {rows.map(({ chat, messageId, snippet }) => (
        <li key={chat.id}>
          <button
            type="button"
            onClick={() => {
              void actions.openChat(chat.id)
              scrollTarget.set(messageId)
            }}
            className="hover:bg-muted focus-visible:bg-muted flex w-full flex-col items-start gap-0.5 rounded-md px-2.5 py-1.5 text-start outline-none"
          >
            <span className="w-full truncate text-sm">{chatTitle(chat)}</span>
            {snippet && (
              <span className="text-muted-foreground line-clamp-2 w-full text-xs wrap-break-word">
                {highlightParts(snippet, query).map((part, i) =>
                  part.match ? (
                    <mark key={i} className="bg-primary/20 text-foreground rounded-sm">
                      {part.text}
                    </mark>
                  ) : (
                    <span key={i}>{part.text}</span>
                  ),
                )}
              </span>
            )}
          </button>
        </li>
      ))}
    </ul>
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
