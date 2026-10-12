"use client";

import { AttachmentPrimitive, ComposerPrimitive, MessagePrimitive, useAui, useAuiState } from "@assistant-ui/react";
import { AlertCircleIcon, FileTextIcon, ImageIcon, Loader2Icon, MonitorIcon, PaperclipIcon, XIcon } from "lucide-react";
import { useEffect, useState, type FC } from "react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { cloudNote, formatSize, kindOfType, screenshotRequestFile, sendGate, type SendGate } from "@/attachments";
import { cn } from "@/lib/utils";
import { useJarvis } from "@/useJarvis";

// Based on assistant-ui's attachment components (git commit a1b2540), trimmed to a chip with a thumbnail or an
// icon: there is no preview dialog, because a sent message keeps only the kind and size of its attachments.

const THUMBNAIL_PX = 40;

// A small preview of an image the owner chose. It is drawn from the bytes in the page, so nothing is
// fetched and no object URL is needed.
const useThumbnail = (file: File | undefined): string | undefined => {
  const [shown, setShown] = useState<{ file: File; url: string } | undefined>(undefined);
  useEffect(() => {
    if (!file || !file.type.startsWith("image/") || file.size === 0) return;
    let cancelled = false;
    void (async () => {
      try {
        const bitmap = await createImageBitmap(file);
        const side = Math.min(bitmap.width, bitmap.height);
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = THUMBNAIL_PX * 2;
        canvas.getContext("2d")?.drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, canvas.width, canvas.height);
        bitmap.close();
        if (!cancelled) setShown({ file, url: canvas.toDataURL("image/jpeg", 0.7) });
      } catch {
        /* an image the browser cannot draw keeps its icon */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [file]);
  return shown && shown.file === file ? shown.url : undefined;
};

const ChipIcon: FC<{ type: string }> = ({ type }) => {
  const className = "text-muted-foreground size-5 stroke-[1.5]";
  if (type === "image") return <ImageIcon className={className} />;
  if (type === "screenshot") return <MonitorIcon className={className} />;
  return <FileTextIcon className={className} />;
};

const AttachmentChip: FC = () => {
  const aui = useAui();
  const inComposer = aui.attachment.source !== "message";
  const { cloud } = useJarvis();
  const type = useAuiState((s) => s.attachment.type);
  const name = useAuiState((s) => s.attachment.name);
  const file = useAuiState((s) => s.attachment.file);
  const statusType = useAuiState((s) => s.attachment.status.type);
  const errorMessage = useAuiState((s) =>
    s.attachment.status.type === "incomplete" ? (s.attachment.status.message ?? "Upload failed") : undefined,
  );
  const thumbnail = useThumbnail(inComposer ? file : undefined);
  const uploading = statusType === "running";
  const failed = statusType === "incomplete";
  const detail = failed
    ? errorMessage
    : uploading
      ? "Uploading…"
      : inComposer
        ? [file && file.size > 0 ? formatSize(file.size) : null, cloudNote(cloud?.mode)].filter(Boolean).join(" · ")
        : null;

  return (
    <AttachmentPrimitive.Root
      data-slot="aui_attachment"
      className={cn(
        "aui-attachment-root bg-muted/60 text-foreground relative flex max-w-60 min-w-0 items-center gap-2 rounded-lg py-1 ps-1 pe-2 text-xs",
        failed && "ring-destructive/60 max-w-80 ring-1",
        inComposer && "pe-7",
      )}
    >
      <div className="bg-background/60 relative flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md">
        {thumbnail ? <img src={thumbnail} alt="" className="size-full object-cover" /> : <ChipIcon type={type} />}
        {uploading && (
          <div aria-hidden className="bg-background/60 absolute inset-0 flex items-center justify-center">
            <Loader2Icon className="text-muted-foreground size-4 animate-spin motion-reduce:animate-none" />
          </div>
        )}
        {failed && (
          <div aria-hidden className="bg-background/70 absolute inset-0 flex items-center justify-center">
            <AlertCircleIcon className="text-destructive size-4" />
          </div>
        )}
      </div>
      <div className="flex min-w-0 flex-col">
        <span className="truncate font-medium" title={name}>
          {name}
        </span>
        {detail ? (
          <span className={cn(failed ? "text-destructive" : "text-muted-foreground truncate")} title={detail}>
            {detail}
          </span>
        ) : null}
      </div>
      {inComposer && (
        <AttachmentPrimitive.Remove asChild>
          <TooltipIconButton
            tooltip={`Remove ${name}`}
            side="top"
            className="aui-attachment-remove absolute end-1 top-1 size-5 rounded-full"
          >
            <XIcon className="size-3 stroke-[2.5]" />
          </TooltipIconButton>
        </AttachmentPrimitive.Remove>
      )}
    </AttachmentPrimitive.Root>
  );
};

export const UserMessageAttachments: FC = () => (
  <div className="aui-user-message-attachments col-span-full col-start-1 row-start-1 flex w-full flex-row flex-wrap justify-end gap-2 empty:hidden">
    <MessagePrimitive.Attachments>{() => <AttachmentChip />}</MessagePrimitive.Attachments>
  </div>
);

export const ComposerAttachments: FC = () => (
  <div className="aui-composer-attachments flex w-full flex-row flex-wrap items-center gap-2 px-1 empty:hidden">
    <ComposerPrimitive.Attachments>{() => <AttachmentChip />}</ComposerPrimitive.Attachments>
  </div>
);

export const ComposerAddAttachment: FC = () => (
  <ComposerPrimitive.AddAttachment asChild>
    <TooltipIconButton
      tooltip="Attach an image, a text or code file, or a PDF"
      side="bottom"
      variant="ghost"
      size="icon"
      type="button"
      className="text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 size-7 rounded-full active:scale-[0.96] motion-reduce:transition-none"
      aria-label="Attach a file"
    >
      <PaperclipIcon className="size-4" />
    </TooltipIconButton>
  </ComposerPrimitive.AddAttachment>
);

const SCREENSHOT_WHY: Record<string, string> = {
  screen_awareness_off: "Screen awareness is off. Turn it on in Settings to attach a screenshot.",
  screenshot_unavailable: "Screenshots aren't available here.",
};

export const ComposerScreenshot: FC = () => {
  const aui = useAui();
  const { attachmentSupport } = useJarvis();
  const blocked = attachmentSupport !== null && !attachmentSupport.screenshot.ok;
  const why = blocked ? (SCREENSHOT_WHY[attachmentSupport.screenshot.reason ?? ""] ?? SCREENSHOT_WHY["screenshot_unavailable"]) : undefined;
  return (
    <TooltipIconButton
      tooltip={why ?? "Attach a screenshot of your screen"}
      side="bottom"
      variant="ghost"
      size="icon"
      type="button"
      disabled={blocked}
      onClick={() => void aui.composer.addAttachment(screenshotRequestFile()).catch(() => undefined)}
      className="text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 size-7 rounded-full active:scale-[0.96] motion-reduce:transition-none"
      aria-label="Attach a screenshot"
    >
      <MonitorIcon className="size-4" />
    </TooltipIconButton>
  );
};

// Whether the message can go and what to tell the owner, from what is attached and what the route can take.
export const useAttachmentGate = (): SendGate => {
  const { attachmentSupport } = useJarvis();
  const summary = useAuiState((s) => s.composer.attachments.map((a) => `${a.type}:${a.status.type}`).join("|"));
  const hasText = useAuiState((s) => s.composer.text.trim().length > 0);
  const items = summary ? summary.split("|").map((item) => item.split(":")) : [];
  const kinds = items.map(([type]) => kindOfType(type ?? ""));
  const uploading = items.some(([, status]) => status === "running");
  return sendGate(attachmentSupport, kinds, { uploading, hasText });
};

export const AttachmentNotice: FC<{ gate: SendGate }> = ({ gate }) =>
  gate.message ? (
    <p role="status" className={cn("px-2.5 text-xs", gate.problem ? "text-destructive" : "text-muted-foreground")}>
      {gate.message}
    </p>
  ) : null;
