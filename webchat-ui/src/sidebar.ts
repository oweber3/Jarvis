import type { Chat } from "@/api"
import { groupByAge } from "@/time"

// The chat list of one place (a project, or the chats without one): pinned chats on top, the others
// grouped by when they were last active. ``chats`` arrive newest first, as the library delivers them.
export function arrangeChats(chats: readonly Chat[], now: Date = new Date()) {
  return {
    pinned: chats.filter((c) => c.pinned),
    groups: groupByAge(
      chats.filter((c) => !c.pinned),
      now,
    ),
  }
}
