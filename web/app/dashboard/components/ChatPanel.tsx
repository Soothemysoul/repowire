"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronRight } from "lucide-react";
import { cn } from "../lib/utils";
import { peerLabel } from "../types";
import type { Peer, Event } from "../types";

interface ChatPanelProps {
  peer: Peer;
  events: Event[];
}

function ToolCallsList({ toolCalls }: { toolCalls: { name: string; input: string }[] }) {
  const [expanded, setExpanded] = useState(false);
  if (toolCalls.length === 0) return null;

  return (
    <div className="mt-2 border-t border-zinc-700/50 pt-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-[11px] text-zinc-500 hover:text-zinc-400 transition-colors"
      >
        <ChevronRight className={cn("w-3 h-3 transition-transform", expanded && "rotate-90")} />
        <span>{toolCalls.length} tool call{toolCalls.length > 1 ? "s" : ""}</span>
      </button>
      {expanded && (
        <div className="mt-1.5 space-y-1 pl-4">
          {toolCalls.map((tc, i) => (
            <div key={i} className="flex items-baseline gap-2 text-[11px] font-mono">
              <span className="text-blue-400 shrink-0">{tc.name}</span>
              <span className="text-zinc-600 truncate">{tc.input}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel({ peer, events }: ChatPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const id = peer.peer_id;
    return events
      .filter((e) => {
        if (e.type === "chat_turn") return e.peer_id === id;
        return e.from_peer_id === id || e.to_peer_id === id;
      })
      .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  }, [peer.peer_id, events]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [filtered.length]);

  if (filtered.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-zinc-600 text-sm">
        No activity for {peerLabel(peer)} yet
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 p-4 overflow-y-auto h-full">
      {filtered.map((event) => {
        if (event.type === "chat_turn") {
          const isUser = event.role === "user";
          return (
            <div key={event.id} className={cn("flex flex-col gap-1", isUser ? "items-end" : "items-start")}>
              <span className="text-[10px] text-zinc-500 font-mono px-1">
                {isUser ? "user" : peerLabel(peer)}
              </span>
              <div
                className={cn(
                  "max-w-[95%] sm:max-w-[80%] rounded-xl px-3 sm:px-4 py-2 sm:py-3 text-sm",
                  isUser
                    ? "bg-zinc-700 text-zinc-200"
                    : "bg-zinc-800/50 text-zinc-300"
                )}
              >
                {isUser ? (
                  <p className="whitespace-pre-wrap">{event.text}</p>
                ) : (
                  <>
                    <div className="prose prose-invert prose-sm max-w-none prose-p:my-1 prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700 prose-code:text-emerald-300 prose-ul:list-disc prose-ul:pl-4 prose-li:my-0.5 prose-table:border-collapse prose-th:border prose-th:border-zinc-700 prose-th:px-3 prose-th:py-1.5 prose-th:bg-zinc-900 prose-td:border prose-td:border-zinc-700 prose-td:px-3 prose-td:py-1.5">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{event.text}</ReactMarkdown>
                    </div>
                    {event.tool_calls && <ToolCallsList toolCalls={event.tool_calls} />}
                  </>
                )}
              </div>
              <span className="text-[10px] text-zinc-600 font-mono tabular-nums px-1">
                {new Date(event.timestamp).toLocaleTimeString()}
              </span>
            </div>
          );
        }

        // Repowire trace row
        const label =
          event.type === "query"
            ? `⇢ query ${event.from} → ${event.to}`
            : event.type === "response"
            ? `⇢ response ${event.from} → ${event.to}`
            : event.type === "notification"
            ? `⇢ notify ${event.from} → ${event.to}`
            : `⇢ broadcast from ${event.from}`;

        return (
          <div key={event.id} className="flex items-start gap-2 text-xs font-mono text-zinc-600">
            <span className="shrink-0 text-zinc-700">{new Date(event.timestamp).toLocaleTimeString()}</span>
            <span className="text-zinc-500">{label}</span>
            <span className="truncate text-zinc-600">{event.text}</span>
          </div>
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
