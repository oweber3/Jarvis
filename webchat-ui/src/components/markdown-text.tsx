import { useMessagePartText } from "@assistant-ui/react"
import Markdown, { type MarkdownToJSX } from "markdown-to-jsx"
import { CheckIcon, CopyIcon, ImageIcon } from "lucide-react"
import { Fragment, isValidElement, memo, type ComponentPropsWithoutRef, type FC, type JSX, type ReactNode } from "react"

import { TooltipIconButton } from "@/components/tooltip-icon-button"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"
import { cn } from "@/lib/utils"

// A reply's Markdown, drawn as React elements (never as HTML). Written for Jarvis on markdown-to-jsx, in place of
// assistant-ui's react-markdown component, to keep the page small: the classes are those of assistant-ui's
// markdown-text (git commit a1b2540).
//
// What a reply can carry is model output, so: HTML in it is shown as text, links open outside the chat (and only
// for safe addresses, the library's sanitiser drops the rest), and an image is shown as a link to it, never fetched,
// so a reply can make the page contact nothing.

type Props<T extends keyof JSX.IntrinsicElements> = ComponentPropsWithoutRef<T>

const heading = (Tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6", className: string): FC<Props<"h1">> =>
  // The library gives headings an id from their text; ids from model output could collide with the page's own.
  function Heading({ className: extra, id: _id, ...props }) {
    return <Tag className={cn(className, extra)} {...props} />
  }

const Link: FC<Props<"a">> = ({ className, href, children, ...props }) => (
  <a
    {...props}
    {...(href ? { href, target: "_blank", rel: "noreferrer noopener" } : {})}
    className={cn("aui-md-a text-primary hover:text-primary/80 underline underline-offset-2", className)}
  >
    {children}
  </a>
)

const Image: FC<Props<"img">> = ({ src, alt }) =>
  src ? (
    <a
      href={src}
      target="_blank"
      rel="noreferrer noopener"
      className="aui-md-image text-primary inline-flex items-center gap-1 underline underline-offset-2"
    >
      <ImageIcon className="size-3.5 shrink-0" aria-hidden />
      {alt || "Image"}
    </a>
  ) : null

const InlineCode: FC<Props<"code">> = ({ className, ...props }) => (
  <code
    className={cn("aui-md-inline-code bg-muted rounded-md px-1.5 py-0.5 font-mono text-[0.85em]", className)}
    {...props}
  />
)

// The text of a fenced block and the language its fence named ("lang-python" from the library).
function fenced(children: ReactNode): { code: string; language: string } {
  const element = isValidElement<{ className?: string; children?: ReactNode }>(children) ? children : null
  const language = /(?:^|\s)lang-([\w#+.-]+)/.exec(element?.props.className ?? "")?.[1] ?? ""
  const body = element ? element.props.children : children
  return { code: typeof body === "string" ? body : Array.isArray(body) ? body.join("") : String(body ?? ""), language }
}

const CodeBlock: FC<Props<"pre">> = ({ children }) => {
  const { code, language } = fenced(children)
  const { isCopied, copyToClipboard } = useCopyToClipboard()
  const text = code.replace(/\n$/, "")
  return (
    <div data-slot="aui_code-block" className="aui-code-block my-3 overflow-hidden rounded-xl border">
      <div className="aui-code-header-root bg-muted/50 flex items-center justify-between border-b px-3.5 py-1 text-xs">
        <span className="aui-code-header-language text-muted-foreground font-medium lowercase">{language || "text"}</span>
        <TooltipIconButton tooltip={isCopied ? "Copied" : "Copy code"} onClick={() => !isCopied && copyToClipboard(text)}>
          {isCopied ? (
            <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
          ) : (
            <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
          )}
        </TooltipIconButton>
      </div>
      <pre className="aui-md-pre bg-muted/20 overflow-x-auto p-3.5 text-[13px] leading-relaxed">
        <code className={cn("font-mono", language && `language-${language}`)}>{text}</code>
      </pre>
    </div>
  )
}

const styled = <T extends keyof JSX.IntrinsicElements>(Tag: T, className: string): FC<Props<T>> =>
  function Styled({ className: extra, ...props }: Props<T> & { className?: string }) {
    const Element = Tag as unknown as FC<Record<string, unknown>>
    return <Element className={cn(className, extra)} {...props} />
  }

const Table: FC<Props<"table">> = ({ className, ...props }) => (
  <div className="aui-md-table-wrapper my-3 overflow-x-auto">
    <table className={cn("aui-md-table w-full border-separate border-spacing-0 text-sm", className)} {...props} />
  </div>
)

const TaskBox: FC<Props<"input">> = ({ checked }) => (
  <input
    type="checkbox"
    checked={!!checked}
    readOnly
    disabled
    aria-label={checked ? "Done" : "Not done"}
    className="accent-primary me-1.5 size-3.5 translate-y-0.5"
  />
)

const OPTIONS: MarkdownToJSX.Options = {
  forceBlock: true,
  // HTML in a reply is shown as the text it is.
  disableParsingRawHTML: true,
  wrapper: Fragment,
  overrides: {
    h1: heading("h1", "aui-md-h1 mt-5 mb-2 scroll-m-20 text-xl font-semibold first:mt-0 last:mb-0"),
    h2: heading("h2", "aui-md-h2 mt-5 mb-2 scroll-m-20 text-lg font-semibold first:mt-0 last:mb-0"),
    h3: heading("h3", "aui-md-h3 mt-4 mb-1.5 scroll-m-20 text-base font-semibold first:mt-0 last:mb-0"),
    h4: heading("h4", "aui-md-h4 mt-3.5 mb-1 scroll-m-20 text-base font-medium first:mt-0 last:mb-0"),
    h5: heading("h5", "aui-md-h5 mt-3 mb-1 text-sm font-semibold first:mt-0 last:mb-0"),
    h6: heading("h6", "aui-md-h6 mt-3 mb-1 text-sm font-medium first:mt-0 last:mb-0"),
    p: styled("p", "aui-md-p my-3 leading-relaxed first:mt-0 last:mb-0"),
    a: Link,
    img: Image,
    blockquote: styled("blockquote", "aui-md-blockquote border-muted-foreground/30 text-muted-foreground my-3 border-s-2 ps-4"),
    ul: styled("ul", "aui-md-ul marker:text-muted-foreground my-3 ms-5 list-disc [&>li]:mt-1"),
    ol: styled("ol", "aui-md-ol marker:text-muted-foreground my-3 ms-5 list-decimal [&>li]:mt-1"),
    li: styled("li", "aui-md-li leading-relaxed"),
    hr: styled("hr", "aui-md-hr border-muted-foreground/20 my-3"),
    table: Table,
    th: styled(
      "th",
      "aui-md-th bg-muted px-3 py-1.5 text-start font-medium first:rounded-ss-lg last:rounded-se-lg",
    ),
    td: styled("td", "aui-md-td border-muted-foreground/20 border-s border-b px-3 py-1.5 text-start last:border-e"),
    tr: styled(
      "tr",
      "aui-md-tr m-0 border-b p-0 first:border-t [&:last-child>td:first-child]:rounded-es-lg [&:last-child>td:last-child]:rounded-ee-lg",
    ),
    strong: styled("strong", "aui-md-strong font-semibold"),
    sup: styled("sup", "aui-md-sup [&>a]:text-xs [&>a]:no-underline"),
    pre: CodeBlock,
    code: InlineCode,
    input: TaskBox,
  },
}

/** Markdown text, drawn once per text: a reply that has not changed is not parsed again. */
export const MarkdownContent: FC<{ text: string; className?: string }> = memo(function MarkdownContent({ text, className }) {
  return (
    <div className={cn("aui-md", className)}>
      <Markdown options={OPTIONS}>{text}</Markdown>
    </div>
  )
})

/** The text part of the message it sits in, as Markdown. */
export const MarkdownText: FC = () => {
  const { text } = useMessagePartText()
  return <MarkdownContent text={text} />
}
