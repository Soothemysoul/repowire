"use client";

import { useState, useRef, useEffect, KeyboardEvent } from "react";
import { Paperclip, RefreshCw, Send, X } from "lucide-react";
import { cn } from "../lib/utils";
import type { Peer } from "../types";
import { peerLabel } from "../types";

interface ComposeBarProps {
  peer: Peer;
  apiBase: string;
  onSent?: () => void;
}

export function ComposeBar({ peer, apiBase, onSent }: ComposeBarProps) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"notify" | "ask">("notify");
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [response, setResponse] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [text]);

  const uploadFile = async (f: File): Promise<string | null> => {
    const formData = new FormData();
    formData.append("file", f);
    try {
      const res = await fetch(`${apiBase}/attachments`, {
        method: "POST",
        body: formData,
        credentials: "include",
      });
      if (!res.ok) {
        console.error("Upload failed:", res.status, await res.text().catch(() => ""));
        return null;
      }
      const data = await res.json();
      return data.path as string;
    } catch {
      return null;
    }
  };

  const submit = async () => {
    if ((!text.trim() && !file) || isPending) return;
    setError(null);
    setResponse(null);
    setIsPending(true);

    try {
      let msg = text.trim();

      // Hint so the agent knows dashboard sees responses automatically
      const hint = "\n(from @dashboard - reply naturally, dashboard sees your response automatically)";

      // Upload attachment if present
      if (file) {
        const path = await uploadFile(file);
        if (!path) {
          setError("Failed to upload file");
          return;
        }
        msg = msg ? `${msg}\n[Attachment: ${path}]` : `[Attachment: ${path}]`;
      }

      if (mode === "notify") {
        const res = await fetch(`${apiBase}/notify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from_peer: "dashboard", to_peer: peer.name, text: msg + hint, bypass_circle: true }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(body.detail || `Error ${res.status}`);
        } else {
          setText("");
          setFile(null);
          if (onSent) setTimeout(onSent, 1000);
        }
      } else {
        const res = await fetch(`${apiBase}/query`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from_peer: "dashboard", to_peer: peer.name, text: msg + hint, bypass_circle: true }),
        });
        const data = await res.json();
        if (data.error) {
          setError(data.error);
        } else {
          setResponse(data.text ?? null);
          setText("");
          setFile(null);
          if (onSent) setTimeout(onSent, 1000);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setIsPending(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-zinc-800 bg-zinc-950 p-2 sm:p-3 flex flex-col gap-2 shrink-0">
      {/* Controls row */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-300 font-mono truncate max-w-[10rem]" title={peer.name}>
            → {peerLabel(peer)}
          </span>
          <div className="flex rounded-md overflow-hidden border border-zinc-700">
            {(["notify", "ask"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={cn(
                  "px-2.5 py-1 text-xs transition-colors",
                  mode === m ? "bg-zinc-700 text-zinc-200" : "bg-zinc-900 text-zinc-500 hover:text-zinc-300"
                )}
              >
                {m}
              </button>
            ))}
          </div>
        </div>
        <span className="ml-auto text-[10px] text-zinc-600 hidden sm:inline">⌘↵ to send</span>
      </div>

      {/* File preview */}
      {file && (
        <div className="flex items-center gap-2 px-2 py-1.5 bg-zinc-900 border border-zinc-700 rounded-lg text-xs text-zinc-400">
          <Paperclip className="w-3 h-3 shrink-0" />
          <span className="truncate flex-1">{file.name}</span>
          <span className="text-zinc-600 shrink-0">{(file.size / 1024).toFixed(0)}KB</span>
          <button onClick={() => setFile(null)} className="p-0.5 hover:text-zinc-200">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* Textarea + attach */}
      <div className="flex gap-2 items-end">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={mode === "notify" ? `Notify ${peerLabel(peer)}...` : `Ask ${peerLabel(peer)}...`}
          rows={1}
          className="flex-1 min-w-0 bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-300 placeholder-zinc-600 resize-none focus:outline-none focus:ring-1 focus:ring-zinc-500"
        />
        <button
          onClick={() => fileRef.current?.click()}
          className="p-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors shrink-0"
          title="Attach file"
        >
          <Paperclip className="w-4 h-4" />
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*,.pdf,.txt,.json,.csv,.md"
          className="hidden"
          onChange={(e) => { if (e.target.files?.[0]) setFile(e.target.files[0]); e.target.value = ""; }}
        />
      </div>

      {/* Send button */}
      <button
        onClick={submit}
        disabled={(!text.trim() && !file) || isPending}
        className={cn(
          "w-full flex items-center justify-center gap-2 py-2 rounded-lg text-sm font-medium transition-colors",
          (text.trim() || file)
            ? "bg-emerald-600 hover:bg-emerald-500 text-white"
            : "bg-zinc-800 text-zinc-500",
          "disabled:opacity-40 disabled:cursor-not-allowed"
        )}
      >
        {isPending ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
        <span>{mode === "notify" ? "Send" : "Ask"}</span>
      </button>

      {error && (
        <div className="flex items-center gap-2">
          <p className="text-xs text-red-400 font-mono flex-1">{error}</p>
          <button
            onClick={submit}
            className="text-[10px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-400 hover:text-zinc-200 transition-colors shrink-0"
          >
            Retry
          </button>
        </div>
      )}
      {response && (
        <div className="text-xs text-zinc-400 bg-zinc-900 border border-zinc-800 rounded-lg p-2 max-h-24 overflow-y-auto font-mono whitespace-pre-wrap">
          {response}
        </div>
      )}
    </div>
  );
}
