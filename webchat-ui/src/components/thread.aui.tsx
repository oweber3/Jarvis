"use client";

import {
  AttachmentNotice,
  ComposerAddAttachment,
  ComposerAttachments,
  ComposerScreenshot,
  UserMessageAttachments,
  useAttachmentGate,
} from "@/components/attachment.aui";
import { MarkdownText } from "@/components/markdown-text";
import { DayDivider, MessageTime } from "@/components/MessageTime";
import { JarvisMark } from "@/components/JarvisMark";
import { ToolStepGroup, ToolStepRow } from "@/components/ToolSteps";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  ComposerPrimitive,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  ThreadPrimitive,
  type TextMessagePartComponent,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  AudioLinesIcon,
  CheckIcon,
  CopyIcon,
  InfoIcon,
  MicIcon,
  PencilIcon,
  PhoneIcon,
  RefreshCwIcon,
  SquareIcon,
} from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  type ComponentType,
  type FC,
} from "react";
import { useJarvis } from "@/useJarvis";
import { messageSelector, scrollTarget, useScrollTarget } from "@/scrollTarget";
import { dayDividerLabel, startsNewDay } from "@/time";

/**
 * Optional component overrides for the thread. `AssistantMessage` and
 * `Welcome` replace whole sections; `ComposerLeading` fills the left of the
 * composer's action row (the model picker).
 */
export type ThreadComponents = {
  AssistantMessage?: ComponentType | undefined;
  Welcome?: ComponentType | undefined;
  ComposerLeading?: ComponentType | undefined;
  ComposerTrailing?: ComponentType | undefined;
  /** Shown just above the composer, inside the sticky footer (the phone app's confirmation, toast and voice indicator). */
  ComposerAbove?: ComponentType | undefined;
  /** The Screenshot button; `null` leaves it out (the phone app has no screen of its own to share). */
  ComposerScreenshotButton?: ComponentType | null | undefined;
};

export type ThreadProps = {
  components?: ThreadComponents | undefined;
  autoFocus?: boolean | undefined;
};

const EMPTY_COMPONENTS: ThreadComponents = {};

const ThreadComponentsContext =
  createContext<ThreadComponents>(EMPTY_COMPONENTS);

// Startup exposes a loading placeholder thread; treat it as a new chat so
// the composer mounts centered. Loads after startup keep the docked layout.
const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  (!s.thread.isLoading || s.threads.isLoading);

// A switched thread that is still fetching its history: skeleton, not welcome.
const isHistoryLoadingView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  s.thread.isLoading &&
  !s.thread.isDisabled &&
  !s.threads.isLoading;

const ThreadHistorySkeleton: FC = () => (
  <div
    data-slot="aui_thread-history-skeleton"
    role="status"
    className="animate-in fade-in fill-mode-both flex flex-col gap-y-6 [animation-delay:150ms] [animation-duration:200ms]"
  >
    <span className="sr-only">Loading conversation</span>
    <Skeleton className="ml-auto h-9 w-2/5 rounded-xl motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-11/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-4/5 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-3/5 motion-reduce:animate-none" />
    </div>
    <Skeleton className="ml-auto h-9 w-1/3 rounded-xl motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-10/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-2/3 motion-reduce:animate-none" />
    </div>
  </div>
);

export const Thread: FC<ThreadProps> = ({
  components = EMPTY_COMPONENTS,
  autoFocus = true,
}) => {
  const isEmpty = useAuiState(isNewChatView);

  return (
    <ThreadComponentsContext.Provider value={components}>
      {/* A file dropped anywhere on the thread is attached to the next message. */}
      <ComposerPrimitive.AttachmentDropzone className="aui-dropzone data-[dragging=true]:ring-primary/50 h-full rounded-lg data-[dragging=true]:ring-2 data-[dragging=true]:ring-inset">
        <ThreadRoot isEmpty={isEmpty} autoFocus={autoFocus} />
      </ComposerPrimitive.AttachmentDropzone>
    </ThreadComponentsContext.Provider>
  );
};

const ThreadRoot: FC<{ isEmpty: boolean; autoFocus: boolean }> = ({
  isEmpty,
  autoFocus,
}) => {
  const { Welcome = ThreadWelcome, ComposerAbove } = useContext(ThreadComponentsContext);

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background @container flex h-full flex-col"
      style={{
        ["--thread-max-width" as string]: "44rem",
        ["--composer-bg" as string]: "var(--color-card)",
        ["--composer-radius" as string]: "1.25rem",
        ["--composer-padding" as string]: "8px",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll"
      >
        <ScrollToSearchHit />
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
            isEmpty && "justify-center",
          )}
        >
          <AuiIf condition={isNewChatView}>
            <Welcome />
          </AuiIf>
          <AuiIf condition={isHistoryLoadingView}>
            <ThreadHistorySkeleton />
          </AuiIf>

          <ThreadLoadEarlier />
          <div
            data-slot="aui_message-group"
            className="mb-14 flex flex-col gap-y-7 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
          </div>

          <ThreadPrimitive.ViewportFooter
            className={cn(
              "aui-thread-viewport-footer bg-background flex flex-col gap-4 overflow-visible pb-4 md:pb-6",
              !isEmpty &&
                "sticky bottom-0 mt-auto rounded-t-(--composer-radius)",
            )}
          >
            <ThreadScrollToBottom />
            <ThreadBackToLatest />
            {ComposerAbove ? <ComposerAbove /> : null}
            <Composer autoFocus={autoFocus} />
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

// A long chat is loaded a page at a time: earlier messages arrive as the top of the conversation comes into view
// (the button does the same from the keyboard). The runtime keeps the rows in place while a page is put in front.
const ThreadLoadEarlier: FC = () => {
  const aui = useAui();
  const visible = useAuiState(
    (s) => s.thread.hasEarlier || s.thread.isLoadingEarlier,
  );
  const loading = useAuiState((s) => s.thread.isLoadingEarlier);
  const sentinel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = sentinel.current;
    if (!visible || !element) return;
    const root = element.closest("[data-slot='aui_thread-viewport']");
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void aui.thread().loadEarlier();
      },
      { root, rootMargin: "600px 0px 0px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [aui, visible]);

  return (
    <div ref={sentinel} data-slot="aui_thread-load-earlier" className="flex justify-center">
      <span role="status" className="sr-only">
        {loading ? "Loading earlier messages" : ""}
      </span>
      {visible && (
        <ThreadPrimitive.LoadEarlier asChild>
          <Button
            variant="ghost"
            size="sm"
            className="aui-thread-load-earlier text-muted-foreground mb-6 rounded-full"
          >
            {loading ? "Loading earlier messages…" : "Load earlier messages"}
          </Button>
        </ThreadPrimitive.LoadEarlier>
      )}
    </div>
  );
};

// After scrolling far back in a long chat the page lets go of the newest turns; this brings them back.
const ThreadBackToLatest: FC = () => {
  const { history, actions } = useJarvis();
  if (!history.hasLater) return null;
  return (
    <div
      data-slot="aui_thread-back-to-latest"
      role="status"
      className="bg-popover text-popover-foreground border-border flex items-center justify-between gap-3 rounded-xl border px-3 py-2 text-sm shadow-sm"
    >
      <span className="min-w-0">You're reading earlier messages.</span>
      <Button size="sm" variant="secondary" className="shrink-0" onClick={() => void actions.jumpToLatest()}>
        Back to the latest
      </Button>
    </div>
  );
};

const ThreadMessage: FC = () => {
  const { AssistantMessage: AssistantMessageComponent = AssistantMessage } =
    useContext(ThreadComponentsContext);
  const role = useAuiState((s) => s.message.role);
  const isSpoken = useAuiState((s) => s.message.metadata.modality === "voice");
  const isNotice = useAuiState((s) => s.message.metadata.custom?.["notice"] === true);
  const isEditing = useAuiState((s) => s.message.composer.isEditing);

  return (
    <>
      <DayStart />
      {isEditing ? (
        <EditComposer />
      ) : isNotice ? (
        <NoticeLine />
      ) : isSpoken ? (
        <SpokenMessage />
      ) : role === "user" ? (
        <UserMessage />
      ) : (
        <AssistantMessageComponent />
      )}
    </>
  );
};

const dayOf = (date: Date | undefined) => (date ? date.getTime() / 1000 : undefined);

// A divider above the first message of each day. A pending message, which has no stored time yet, counts as now.
const DayStart: FC = () => {
  const label = useAuiState((s) => {
    const own = dayOf(s.message.createdAt);
    if (own === undefined) return null;
    const previous = dayOf(s.thread.messages[s.message.index - 1]?.createdAt);
    return startsNewDay(previous, own) ? dayDividerLabel(own) : null;
  });
  return label ? <DayDivider label={label} /> : null;
};

// Opening a search result scrolls to the message it found and marks it for a moment.
const ScrollToSearchHit: FC = () => {
  const target = useScrollTarget();

  useEffect(() => {
    if (target === null) return;
    let cancelled = false;
    let element: Element | null = null;
    const timers: number[] = [];
    const started = performance.now();
    const show = () => element?.scrollIntoView({ block: "center" });
    const look = () => {
      if (cancelled) return;
      element = document.querySelector(messageSelector(target));
      if (!element) {
        if (performance.now() - started < 4000) timers.push(window.setTimeout(look, 80));
        else scrollTarget.set(null);
        return;
      }
      element.setAttribute("data-search-hit", "");
      show();
      // The thread settles to the bottom while a chat loads; look again once it has.
      timers.push(window.setTimeout(show, 250), window.setTimeout(show, 700));
      timers.push(
        window.setTimeout(() => {
          element?.removeAttribute("data-search-hit");
          scrollTarget.set(null);
        }, 2500),
      );
    };
    look();
    return () => {
      cancelled = true;
      timers.forEach((t) => window.clearTimeout(t));
      element?.removeAttribute("data-search-hit");
    };
  }, [target]);

  return null;
};

// A local line from Jarvis (busy, stopped, could not answer). It is shown here but never stored.
const NoticeLine: FC = () => {
  const text = useAuiState((s) => {
    const part = s.message.parts.find((p) => p.type === "text");
    return part && part.type === "text" ? part.text : "";
  });
  return (
    <div data-slot="aui_notice" role="status" className="flex justify-center px-2">
      <span className="bg-muted/60 text-muted-foreground border-border inline-flex max-w-full items-center gap-1.5 rounded-full border px-3 py-1 text-xs">
        <InfoIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="min-w-0">{text}</span>
      </span>
    </div>
  );
};

type VoiceRunPosition = "single" | "start" | "middle" | "end";

const useVoiceRunPosition = (): VoiceRunPosition =>
  useAuiState((s) => {
    const own = dayOf(s.message.createdAt);
    const sameDay = (other: Date | undefined) =>
      own !== undefined &&
      dayOf(other) !== undefined &&
      !startsNewDay(own, dayOf(other)!);
    const previous = s.thread.messages[s.message.index - 1];
    const next = s.thread.messages[s.message.index + 1];
    const before =
      previous?.metadata.modality === "voice" && sameDay(previous.createdAt);
    const after =
      next?.metadata.modality === "voice" && sameDay(next.createdAt);
    if (before) return after ? "middle" : "end";
    return after ? "start" : "single";
  });

const SpokenText: TextMessagePartComponent = ({ text }) => (
  <p className="aui-spoken-message-text m-0">{text}</p>
);

const SpokenMessage: FC = () => {
  const role = useAuiState((s) => s.message.role);
  const position = useVoiceRunPosition();
  const opensExchange = position === "start" || position === "single";

  return (
    <MessagePrimitive.Root
      data-slot="aui_spoken-message-root"
      data-role={role}
      data-voice-run={position}
      className={cn(
        "aui-spoken-message bg-muted/40 mx-2 px-3 py-1.5 [contain-intrinsic-size:auto_48px] [content-visibility:auto]",
        position === "single" && "rounded-xl py-2",
        position === "start" && "rounded-t-xl pt-2",
        position === "middle" && "-mt-6",
        position === "end" && "-mt-6 rounded-b-xl pb-2",
      )}
    >
      {opensExchange && (
        <div
          data-slot="aui_spoken-exchange-header"
          className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs"
        >
          <PhoneIcon className="size-3" aria-hidden />
          <span>Voice conversation</span>
        </div>
      )}
      <div
        data-slot="aui_spoken-message-content"
        className="text-foreground flex items-start gap-2 text-sm leading-relaxed"
      >
        <span className="text-muted-foreground mt-1 shrink-0" aria-hidden>
          {role === "user" ? (
            <MicIcon className="size-3.5" />
          ) : (
            <AudioLinesIcon className="size-3.5" />
          )}
        </span>
        <span className="sr-only">
          {role === "user" ? "You said" : "Assistant said"}
        </span>
        <div className="min-w-0 flex-1 wrap-break-word">
          <MessagePrimitive.Parts
            components={{ Text: SpokenText, tools: { Fallback: ToolStepRow } }}
          />
        </div>
        <MessageTime className="mt-0.5" />
        <SpokenActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const SpokenActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      autohide="never"
      className="aui-spoken-action-bar aui-message-actions text-muted-foreground flex shrink-0 gap-0.5"
    >
      <CopyAction className="size-6" />
      <EditAction className="size-6" />
      <AskAgainAction className="size-6" />
    </ActionBarPrimitive.Root>
  );
};

// -- what can be done with a message ------------------------------------------------------------------------
// The actions stay in the page, so the keyboard reaches them; they are drawn on the last message and on any message
// the pointer is over or that holds focus, and always on a touch screen (index.css, .aui-message-actions).

const isStored = (id: string) => /^m\d+$/.test(id);
// The owner's own message: one stored in the chat, or one that was not answered (shown with its notice).
const canEdit = (s: AssistantState) =>
  !s.thread.isRunning &&
  !s.thread.isDisabled &&
  s.message.role === "user" &&
  (isStored(s.message.id) || s.message.id.endsWith("-request"));
// The latest reply, to a stored message of the owner's that carried nothing attached (attachments are not kept).
const canAskAgain = (s: AssistantState) => {
  if (s.thread.isRunning || s.thread.isDisabled || !s.message.isLast || s.message.role !== "assistant") return false;
  if (!isStored(s.message.id)) return false;
  const asked = s.thread.messages[s.message.index - 1];
  return !!asked && asked.role === "user" && isStored(asked.id) && (asked.attachments?.length ?? 0) === 0;
};

const CopyAction: FC<{ className?: string }> = ({ className }) => (
  <ActionBarPrimitive.Copy asChild>
    <TooltipIconButton tooltip="Copy" className={className}>
      <AuiIf condition={(s) => s.message.isCopied}>
        <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
      </AuiIf>
      <AuiIf condition={(s) => !s.message.isCopied}>
        <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
      </AuiIf>
    </TooltipIconButton>
  </ActionBarPrimitive.Copy>
);

const EditAction: FC<{ className?: string }> = ({ className }) => (
  <AuiIf condition={canEdit}>
    <ActionBarPrimitive.Edit asChild>
      <TooltipIconButton tooltip="Edit" className={className}>
        <PencilIcon />
      </TooltipIconButton>
    </ActionBarPrimitive.Edit>
  </AuiIf>
);

const AskAgainAction: FC<{ className?: string }> = ({ className }) => (
  <AuiIf condition={canAskAgain}>
    <ActionBarPrimitive.Reload asChild>
      <TooltipIconButton tooltip="Ask again" className={className}>
        <RefreshCwIcon />
      </TooltipIconButton>
    </ActionBarPrimitive.Reload>
  </AuiIf>
);

// Editing a message the owner sent: Enter sends, Esc leaves it as it was. What sending does is said beside the box,
// because a stored message takes everything after it with it.
const EditComposer: FC = () => {
  const note = useAuiState((s) => {
    if (!isStored(s.message.id)) return "Sends the new wording as a new message.";
    const after = s.thread.messages.length - 1 - s.message.index;
    const kept = (s.message.attachments?.length ?? 0) > 0 ? " Its attachments are not sent again." : "";
    if (after <= 1) return `Jarvis answers the new wording in place of its last answer.${kept}`;
    return `Jarvis forgets the ${after} messages after this one and answers the new wording.${kept}`;
  });
  return (
    <MessagePrimitive.Root
      data-slot="aui_edit-composer-wrapper"
      className="flex flex-col px-2"
    >
      <ComposerPrimitive.Root className="aui-edit-composer-root border-ring/40 focus-within:border-ring ms-auto flex w-full max-w-[85%] cursor-text flex-col gap-1 rounded-(--composer-radius) border bg-(--composer-bg) transition-[border-color]">
        <ComposerPrimitive.Input
          className="aui-edit-composer-input text-foreground min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base outline-none"
          aria-label="Edit message"
          autoFocus
        />
        <p className="text-muted-foreground px-4 text-xs">{note}</p>
        <div className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
          <ComposerPrimitive.Cancel asChild>
            <Button variant="ghost" size="sm" className="h-8 px-3">
              Cancel
            </Button>
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send asChild>
            <Button size="sm" className="h-8 px-3">
              Send
            </Button>
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="aui-thread-scroll-to-bottom border-border bg-popover text-popover-foreground hover:bg-accent absolute -top-12 z-10 size-9 self-center rounded-full shadow-(--shadow-raised) disabled:invisible"
      >
        <ArrowDownIcon />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: FC = () => {
  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col px-2">
      <p className="aui-thread-welcome-message-inner fade-in slide-in-from-bottom-1 animate-in fill-mode-both text-2xl font-medium tracking-tight duration-200">
        How can I help you today?
      </p>
    </div>
  );
};

const Composer: FC<{ autoFocus: boolean }> = ({ autoFocus }) => {
  const gate = useAttachmentGate();
  return (
    <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
      <div
        data-slot="aui_composer-shell"
        className="border-border focus-within:border-ring/60 bg-card flex w-full cursor-text flex-col gap-2 rounded-(--composer-radius) border p-(--composer-padding) transition-[border-color,box-shadow] duration-150"
      >
        <ComposerAttachments />
        <ComposerPrimitive.Input
          id="message-box"
          placeholder="Message Jarvis…"
          className="aui-composer-input caret-primary placeholder:text-muted-foreground max-h-48 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none focus-visible:outline-none"
          rows={1}
          autoFocus={autoFocus}
          enterKeyHint="send"
          aria-label="Message input"
          // Enter must not slip past what disables the Send button, or the message and its attachments are lost.
          onKeyDown={(event) => {
            if (gate.blocked && event.key === "Enter" && !event.shiftKey) event.preventDefault();
          }}
        />
        <AttachmentNotice gate={gate} />
        <ComposerAction blocked={gate.blocked} />
      </div>
      <p className="aui-composer-hint text-muted-foreground mt-2 text-center text-[11px] @max-[32rem]:hidden">
        <kbd className="font-sans">Enter</kbd> to send · <kbd className="font-sans">Shift</kbd>+<kbd className="font-sans">Enter</kbd>{" "}
        for a new line · <kbd className="font-sans">?</kbd> for shortcuts
      </p>
    </ComposerPrimitive.Root>
  );
};

const ComposerAction: FC<{ blocked: boolean }> = ({ blocked }) => {
  const { ComposerLeading, ComposerTrailing, ComposerScreenshotButton = ComposerScreenshot } = useContext(ThreadComponentsContext);

  return (
    <div className="aui-composer-action-wrapper relative flex items-center justify-between">
      <div className="flex min-w-0 items-center gap-1.5">
        <ComposerAddAttachment />
        {ComposerScreenshotButton ? <ComposerScreenshotButton /> : null}
        {ComposerLeading ? <ComposerLeading /> : null}
      </div>
      <div className="flex items-center gap-1.5">
        {ComposerTrailing ? <ComposerTrailing /> : null}
        <AuiIf condition={(s) => !s.composer.canCancel}>
          <ComposerPrimitive.Send asChild>
            <TooltipIconButton
              tooltip="Send message"
              side="bottom"
              type="button"
              variant="default"
              size="icon"
              disabled={blocked}
              className="aui-composer-send size-7 rounded-full"
              aria-label="Send message"
            >
              <ArrowUpIcon className="aui-composer-send-icon size-4" />
            </TooltipIconButton>
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(s) => s.composer.canCancel}>
          <ComposerPrimitive.Cancel asChild>
            <Button
              type="button"
              variant="default"
              size="icon"
              className="aui-composer-cancel size-7 rounded-full"
              aria-label="Stop generating"
            >
              <SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" />
            </Button>
          </ComposerPrimitive.Cancel>
        </AuiIf>
      </div>
    </div>
  );
};

const MessageError: FC = () => {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="aui-message-error-root border-destructive bg-destructive/10 text-destructive dark:bg-destructive/5 mt-2 rounded-md border p-3 text-sm dark:text-red-200">
        <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

// A reply's tool steps come before its text; consecutive ones are one group (the thinking indicator still arrives
// as a part of its own).
const stepGroups = groupPartByType({ "tool-call": ["group-steps"] });

const AssistantMessage: FC = () => {
  const isLast = useAuiState((s) => s.message.isLast);
  // A reply that follows another one (the outcome of a confirmed action) continues it: no second mark.
  const continues = useAuiState((s) => s.thread.messages[s.message.index - 1]?.role === "assistant");
  // A reply still being worked on has no time and nothing to copy yet.
  const running = useAuiState((s) => s.message.status?.type === "running");
  const ACTION_BAR_PT = "pt-1.5";
  // Keep the action bar inside the contained root's paint box, then cancel its reserved space in flow.
  const ACTION_BAR_HEIGHT = `min-h-7.5 ${ACTION_BAR_PT}`;

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      data-last-message={isLast ? "" : undefined}
      className="fade-in slide-in-from-bottom-1 animate-in relative -mb-7.5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 pb-7.5 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] @max-[32rem]:grid-cols-1"
    >
      <div className="row-span-2 w-7 pt-0.5 @max-[32rem]:hidden" aria-hidden>
        {!continues && <JarvisMark className="size-7" />}
      </div>
      <div
        data-slot="aui_assistant-message-content"
        className="text-foreground min-w-0 leading-relaxed wrap-break-word"
      >
        <MessagePrimitive.GroupedParts groupBy={stepGroups}>
          {({ part, children }) => {
            switch (part.type) {
              case "group-steps":
                return <ToolStepGroup indices={part.indices}>{children}</ToolStepGroup>;
              case "tool-call":
                return <ToolStepRow args={part.args} />;
              case "text":
                return <MarkdownText />;
              case "indicator":
                return <ThinkingIndicator />;
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>

      <div
        data-slot="aui_assistant-message-footer"
        className={cn("flex items-center gap-2", ACTION_BAR_HEIGHT)}
      >
        {!running && (
          <>
            <MessageTime />
            <AssistantActionBar />
          </>
        )}
      </div>
    </MessagePrimitive.Root>
  );
};

// Jarvis is working on the reply. The dots move only when motion is welcome.
const ThinkingIndicator: FC = () => (
  <span
    data-slot="aui_assistant-message-indicator"
    role="status"
    className="text-muted-foreground inline-flex items-center gap-2 py-1 text-sm"
  >
    <span aria-hidden className="flex items-center gap-1">
      {[0, 1, 2].map((dot) => (
        <span
          key={dot}
          className="bg-primary size-1.5 rounded-full motion-safe:animate-bounce"
          style={{ animationDelay: `${dot * 140}ms` }}
        />
      ))}
    </span>
    <span>Thinking</span>
  </span>
);

const AssistantActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      autohide="never"
      className="aui-assistant-action-bar-root aui-message-actions text-muted-foreground -ms-1 flex gap-0.5"
    >
      <CopyAction />
      <AskAgainAction />
    </ActionBarPrimitive.Root>
  );
};

const UserActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      autohide="never"
      className="aui-user-action-bar-root aui-message-actions text-muted-foreground flex gap-0.5"
    >
      <EditAction />
      <CopyAction />
    </ActionBarPrimitive.Root>
  );
};

const UserMessage: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      className="fade-in slide-in-from-bottom-1 animate-in grid auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <UserMessageAttachments />
      <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
        <div className="aui-user-message-content peer bg-muted text-foreground px-4 py-2.5 leading-relaxed wrap-break-word empty:hidden">
          <MessagePrimitive.Parts />
        </div>
        <div className="mt-1 flex items-center justify-end gap-1.5">
          <UserActionBar />
          <MessageTime className="block text-end" />
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};
