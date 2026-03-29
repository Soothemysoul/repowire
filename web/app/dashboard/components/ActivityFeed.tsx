"use client";

import { useState, useMemo } from "react";
import { Check, AlertCircle, RefreshCw, ChevronRight } from "lucide-react";
import { cn } from "../lib/utils";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Event } from "../types";

interface Conversation {
  id: string;
  from: string;
  to: string;
  query: Event;
  response?: Event;
  timestamp: string;
  status: "pending" | "success" | "error";
}

interface ActivityFeedProps {
  events: Event[];
  peerFilter?: string; // peer_id to filter events by
  peerName?: string; // display name for empty state
}

function ConversationCard({
  conversation,
  isExpanded,
  onToggle,
}: {
  conversation: Conversation;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const statusIcon =
    conversation.status === "success" ? (
      <Check className="w-3.5 h-3.5 text-emerald-500" />
    ) : conversation.status === "pending" ? (
      <RefreshCw className="w-3.5 h-3.5 text-blue-400 animate-spin" />
    ) : (
      <AlertCircle className="w-3.5 h-3.5 text-red-400" />
    );

  return (
    <div
      className={cn(
        "border rounded-lg overflow-hidden transition-all",
        conversation.status === "pending"
          ? "border-blue-500/20 bg-blue-500/[0.02]"
          : conversation.status === "error"
          ? "border-red-500/20 bg-red-500/[0.02]"
          : "border-zinc-800/50 bg-zinc-800/10"
      )}
    >
      <button
        onClick={onToggle}
        className="w-full px-4 py-3 flex items-center gap-3 hover:bg-zinc-800/30 transition-colors"
      >
        <div className={cn("transition-transform", isExpanded && "rotate-90")}>
          <ChevronRight className="w-4 h-4 text-zinc-500" />
        </div>

        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium text-zinc-300">@{conversation.from}</span>
          <ChevronRight className="w-3 h-3 text-zinc-600" />
          <span className="font-medium text-zinc-300">@{conversation.to}</span>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {statusIcon}
          <span className="text-[10px] text-zinc-600 font-mono tabular-nums">
            {new Date(conversation.timestamp).toLocaleTimeString()}
          </span>
        </div>
      </button>

      {!isExpanded && (
        <div className="px-4 pb-3 pl-11">
          <p className="text-sm text-zinc-500 truncate">
            Q: {conversation.query.text}
          </p>
        </div>
      )}

      {isExpanded && (
        <div className="px-4 pb-4 pl-11 space-y-3">
          <div className="space-y-1">
            <div className="text-[10px] uppercase text-blue-400 font-bold">Query</div>
            <div className="bg-zinc-950 border border-zinc-800/50 rounded-lg p-3">
              <div className="text-sm text-zinc-300 prose prose-invert prose-sm max-w-none prose-p:my-1 prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700 prose-code:text-blue-300 prose-ul:list-disc prose-ul:pl-4 prose-li:my-0.5">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {conversation.query.text}
                </ReactMarkdown>
              </div>
            </div>
          </div>

          {conversation.response ? (
            <div className="space-y-1">
              <div className="text-[10px] uppercase text-emerald-400 font-bold">Response</div>
              <div className="bg-zinc-950 border border-emerald-500/10 rounded-lg p-3">
                <div
                  className={cn(
                    "text-sm prose prose-invert prose-sm max-w-none prose-p:my-1 prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700 prose-code:text-emerald-300 prose-ul:list-disc prose-ul:pl-4 prose-li:my-0.5",
                    conversation.status === "error" ? "text-red-400" : "text-zinc-300"
                  )}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {conversation.response.text}
                  </ReactMarkdown>
                </div>
              </div>
            </div>
          ) : conversation.status === "pending" ? (
            <div className="flex items-center gap-2 text-blue-400 text-xs">
              <RefreshCw className="w-3 h-3 animate-spin" />
              <span>Awaiting response...</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function NotificationRow({ event, isExpanded, onToggle }: { event: Event; isExpanded: boolean; onToggle: () => void }) {
  const isBroadcast = event.type === "broadcast";
  return (
    <div className="border border-zinc-800/50 rounded-lg overflow-hidden bg-zinc-800/10">
      <button
        onClick={onToggle}
        className="w-full px-4 py-3 flex items-center gap-3 hover:bg-zinc-800/30 transition-colors"
      >
        <div className={cn("transition-transform", isExpanded && "rotate-90")}>
          <ChevronRight className="w-4 h-4 text-zinc-500" />
        </div>
        <div className="flex items-center gap-2 text-sm min-w-0">
          <span className="font-medium text-zinc-300">@{event.from || "?"}</span>
          {!isBroadcast && (
            <>
              <ChevronRight className="w-3 h-3 text-zinc-600 shrink-0" />
              <span className="font-medium text-zinc-300">@{event.to || "?"}</span>
            </>
          )}
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-500 font-mono shrink-0">
            {isBroadcast ? "broadcast" : "notify"}
          </span>
        </div>
        <span className="ml-auto text-[10px] text-zinc-600 font-mono tabular-nums shrink-0">
          {new Date(event.timestamp).toLocaleTimeString()}
        </span>
      </button>
      {!isExpanded && (
        <div className="px-4 pb-3 pl-11">
          <p className="text-sm text-zinc-500 truncate">{event.text}</p>
        </div>
      )}
      {isExpanded && (
        <div className="px-4 pb-4 pl-11">
          <div className="bg-zinc-950 border border-zinc-800/50 rounded-lg p-3">
            <div className="text-sm text-zinc-300 prose prose-invert prose-sm max-w-none prose-p:my-1 prose-pre:bg-zinc-900 prose-code:text-emerald-300">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{event.text}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export function ActivityFeed({ events, peerFilter, peerName }: ActivityFeedProps) {
  const { conversations, notifications } = useMemo(() => {
    const responseById = new Map<string, Event>();
    const queryEvents: Event[] = [];
    const notifEvents: Event[] = [];

    for (const e of events) {
      const matchesPeer = !peerFilter
        || e.from_peer_id === peerFilter || e.to_peer_id === peerFilter;

      if (e.type === "query") {
        if (matchesPeer) queryEvents.push(e);
      } else if (e.type === "response" && e.correlation_id) {
        responseById.set(e.correlation_id, e);
      } else if (e.type === "notification" || e.type === "broadcast") {
        if (matchesPeer) notifEvents.push(e);
      }
    }

    const conversations = queryEvents
      .map((query) => {
        const response = responseById.get(query.id);
        return {
          id: query.id,
          from: query.from || "unknown",
          to: query.to || "unknown",
          query,
          response,
          timestamp: query.timestamp,
          status: (query.status === "error" ? "error" : response ? "success" : "pending") as Conversation["status"],
        };
      });

    return { conversations, notifications: notifEvents };
  }, [events, peerFilter]);

  // Merge and sort all activity by timestamp (newest first)
  type ActivityItem =
    | { kind: "conversation"; data: Conversation }
    | { kind: "notification"; data: Event };

  const allActivity: ActivityItem[] = useMemo(() => {
    const items: ActivityItem[] = [
      ...conversations.map((c) => ({ kind: "conversation" as const, data: c })),
      ...notifications.map((n) => ({ kind: "notification" as const, data: n })),
    ];
    items.sort((a, b) => {
      const ta = "timestamp" in a.data ? a.data.timestamp : "";
      const tb = "timestamp" in b.data ? b.data.timestamp : "";
      return tb.localeCompare(ta);
    });
    return items;
  }, [conversations, notifications]);

  const [expandedItems, setExpandedItems] = useState<Set<string>>(new Set());

  const toggleItem = (id: string) => {
    setExpandedItems((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (allActivity.length === 0) {
    return (
      <div className="text-center py-12 text-zinc-600">
        <p className="text-sm">
          {peerFilter ? `No activity involving ${peerName || peerFilter}` : "No activity yet"}
        </p>
        <p className="text-xs mt-1">Send a message to get started</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {allActivity.map((item) =>
        item.kind === "conversation" ? (
          <ConversationCard
            key={item.data.id}
            conversation={item.data}
            isExpanded={expandedItems.has(item.data.id)}
            onToggle={() => toggleItem(item.data.id)}
          />
        ) : (
          <NotificationRow
            key={item.data.id}
            event={item.data}
            isExpanded={expandedItems.has(item.data.id)}
            onToggle={() => toggleItem(item.data.id)}
          />
        )
      )}
    </div>
  );
}
