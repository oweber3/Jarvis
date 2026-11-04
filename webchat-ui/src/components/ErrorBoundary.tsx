import { RotateCwIcon, TriangleAlertIcon } from "lucide-react"
import { Component, type ErrorInfo, type ReactNode } from "react"

// A part of the page that breaks is replaced by a plain explanation and a way to reload, never a blank page. The
// conversation is Jarvis's, not the page's, so reloading loses nothing that was sent. Nothing is reported anywhere.

type State = { broken: boolean }

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { broken: false }

  static getDerivedStateFromError(): State {
    return { broken: true }
  }

  override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // Kept on this PC: the browser's own console has the details for whoever looks.
  }

  override render(): ReactNode {
    if (!this.state.broken) return this.props.children
    return (
      <div role="alert" className="bg-background text-foreground flex h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
        <TriangleAlertIcon aria-hidden className="text-warning size-8" />
        <div className="flex max-w-sm flex-col gap-1.5">
          <p className="text-lg font-semibold">The chat page hit a problem</p>
          <p className="text-muted-foreground text-sm">
            Nothing you sent is lost: Jarvis keeps the conversation. Reloading the page usually puts it right.
          </p>
        </div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="bg-primary text-primary-foreground focus-visible:ring-ring/50 inline-flex h-9 items-center gap-2 rounded-lg px-4 text-sm font-medium outline-none focus-visible:ring-2"
        >
          <RotateCwIcon aria-hidden className="size-4" />
          Reload the page
        </button>
      </div>
    )
  }
}
