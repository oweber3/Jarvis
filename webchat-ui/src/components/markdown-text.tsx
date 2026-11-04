import { useMessagePartText } from "@assistant-ui/react"
import { compiler, RuleType, type MarkdownToJSX } from "markdown-to-jsx"
import { CheckIcon, CopyIcon, ImageIcon } from "lucide-react"
import {
  Children,
  cloneElement,
  Fragment,
  isValidElement,
  memo,
  useMemo,
  type ComponentPropsWithoutRef,
  type FC,
  type JSX,
  type ReactNode,
} from "react"

import { PartBoundary } from "@/components/ErrorBoundary"
import { TooltipIconButton } from "@/components/tooltip-icon-button"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"
import { highlight, type TokenKind } from "@/highlight"
import { cn } from "@/lib/utils"
import { codePieces, holdsCode, protectCode, restoreCode } from "@/rawCode"

// A reply's Markdown, drawn as React elements (never as HTML). Written for Jarvis on markdown-to-jsx, in place of
// assistant-ui's react-markdown component, to keep the page small: the classes are those of assistant-ui's
// markdown-text (git commit a1b2540).
//
// What a reply can carry is model output, so: HTML in it is shown as text, only web and mail addresses become links
// (opening outside the chat), an image is shown as a link to it, never fetched, so a reply can make the page contact
// nothing, and nothing in it gets an id that could clash with the page's own. Code is kept exactly as written.

type Props<T extends keyof JSX.IntrinsicElements> = ComponentPropsWithoutRef<T>

/** The schemes a link in a reply may have; an image's link is a web address only. Anything else stays text. */
const LINK_SCHEMES = new Set(["http:", "https:", "mailto:"])
const IMAGE_SCHEMES = new Set(["http:", "https:"])

/** The address a link or image of a reply may point at, as the browser reads it, or null for none. */
export function safeAddress(value: string, tag: string): string | null {
  try {
    const url = new URL(value.trim())
    return (tag === "img" ? IMAGE_SCHEMES : LINK_SCHEMES).has(url.protocol) ? url.href : null
  } catch {
    return null
  }
}

const heading = (Tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6", className: string): FC<Props<"h1">> =>
  // The library gives headings an id from their text; ids from model output could collide with the page's own.
  function Heading({ className: extra, id: _id, ...props }) {
    return <Tag className={cn(className, extra)} {...props} />
  }

const Link: FC<Props<"a">> = ({ className, href, title, children, ...props }) =>
  href ? (
    <a
      {...props}
      title={restoreCode(title)}
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={cn("aui-md-a text-primary hover:text-primary/80 underline underline-offset-2", className)}
    >
      {children}
    </a>
  ) : (
    <span>{children}</span>
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
      {restoreCode(alt) || "Image"}
    </a>
  ) : alt ? (
    <span>{restoreCode(alt)}</span>
  ) : null

// The library gives each footnote an id from its name, for links to it; a reply's links never point inside the page.
const Footnotes: FC<Props<"footer">> = ({ children }) => (
  <footer className="aui-md-footnotes text-muted-foreground border-muted-foreground/20 mt-3 border-t pt-2 text-sm">
    {Children.map(children, (note) => (isValidElement<{ id?: string }>(note) ? cloneElement(note, { id: undefined }) : note))}
  </footer>
)

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

// The colours of a code block's tokens: palette colours that stay readable on the block's card in both looks
// (tests/test_webchat_theme.py checks them).
const TOKEN_COLOUR: Record<TokenKind, string> = {
  keyword: "text-[var(--hud-accent-indigo)]",
  string: "text-[var(--hud-success)]",
  number: "text-[var(--hud-warning)]",
  comment: "text-muted-foreground italic",
  fn: "text-primary",
}

const Highlighted: FC<{ code: string; language: string }> = memo(function Highlighted({ code, language }) {
  return (
    <>
      {highlight(code, language).map((token, index) =>
        token.kind ? (
          <span key={index} className={TOKEN_COLOUR[token.kind]}>
            {token.text}
          </span>
        ) : (
          token.text
        ),
      )}
    </>
  )
})

const CodeBlock: FC<Props<"pre">> = ({ children }) => {
  const { code, language } = fenced(children)
  const { isCopied, copyToClipboard } = useCopyToClipboard()
  const text = code.replace(/\n$/, "")
  return (
    <div data-slot="aui_code-block" className="aui-code-block bg-card my-3 overflow-hidden rounded-xl border">
      <div className="aui-code-header-root bg-muted/40 flex items-center justify-between border-b px-3.5 py-1 text-xs">
        <span className="aui-code-header-language text-muted-foreground font-medium lowercase">{language || "text"}</span>
        <TooltipIconButton tooltip={isCopied ? "Copied" : "Copy code"} onClick={() => !isCopied && copyToClipboard(text)}>
          {isCopied ? (
            <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
          ) : (
            <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
          )}
        </TooltipIconButton>
      </div>
      <pre className="aui-md-pre overflow-x-auto p-3.5 text-[13px] leading-relaxed">
        <code className={cn("font-mono", language && `language-${language}`)}>
          <Highlighted code={text} language={language} />
        </code>
      </pre>
    </div>
  )
}

const styled = <T extends keyof JSX.IntrinsicElements>(Tag: T, className: string): FC<Props<T>> =>
  function Styled({ className: extra, ...props }: Props<T> & { className?: string }) {
    const Element = Tag as unknown as FC<Record<string, unknown>>
    return <Element className={cn(className, extra)} {...props} />
  }

const Quote = styled("blockquote", "aui-md-blockquote border-muted-foreground/30 text-muted-foreground my-3 border-s-2 ps-4")

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

/** Drawn by the page rather than the library: code as written (``rawCode``: a token is inline code, and anywhere
 * else its span as written), and a GitHub alert ("> [!NOTE]") as a quote under its label (the library would draw
 * the label with its HTML rule, which is off). */
const renderRule: MarkdownToJSX.Options["renderRule"] = (next, node, renderChildren, state) => {
  if (node.type === RuleType.text && holdsCode(node.text))
    return (
      <Fragment key={state.key}>
        {codePieces(node.text).map((piece, index) =>
          typeof piece === "string" ? piece : <InlineCode key={index}>{piece.code}</InlineCode>,
        )}
      </Fragment>
    )
  if ((node.type === RuleType.codeBlock || node.type === RuleType.codeInline) && holdsCode(node.text))
    node.text = restoreCode(node.text)
  else if (node.type === RuleType.refLink && holdsCode(node.fallbackChildren))
    node.fallbackChildren = restoreCode(node.fallbackChildren)
  else if (node.type === RuleType.blockQuote && node.alert)
    return (
      <Quote key={state.key}>
        <span className="aui-md-alert text-foreground/80 mb-1 block text-xs font-semibold tracking-wide uppercase">
          {node.alert}
        </span>
        {renderChildren(node.children, state)}
      </Quote>
    )
  return next()
}

const OPTIONS: MarkdownToJSX.Options = {
  forceBlock: true,
  // HTML in a reply is shown as the text it is.
  disableParsingRawHTML: true,
  sanitizer: (value, tag) => safeAddress(restoreCode(value), tag),
  renderRule,
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
    blockquote: Quote,
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
    footer: Footnotes,
  },
}


const Drawn: FC<{ text: string }> = ({ text }) => useMemo(() => compiler(protectCode(text), OPTIONS), [text])

/** Markdown text, drawn once per text: a reply that has not changed is not parsed again. One that cannot be drawn
 * shows its words as plain text. */
export const MarkdownContent: FC<{ text: string; className?: string }> = memo(function MarkdownContent({ text, className }) {
  return (
    <div className={cn("aui-md", className)}>
      <PartBoundary key={text} fallback={<p className="aui-md-p leading-relaxed whitespace-pre-wrap">{text}</p>}>
        <Drawn text={text} />
      </PartBoundary>
    </div>
  )
})

/** The text part of the message it sits in, as Markdown. */
export const MarkdownText: FC = () => {
  const { text } = useMessagePartText()
  return <MarkdownContent text={text} />
}
