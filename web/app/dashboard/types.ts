export interface Peer {
  peer_id: string;
  name: string;
  display_name: string;
  status: "online" | "busy" | "offline";
  machine: string;
  path: string;
  tmux_session?: string;
  backend?: string;
  circle: string;
  last_seen?: string;
  description?: string;
  metadata?: {
    branch?: string;
    [key: string]: unknown;
  };
}

/** Human-readable label: project folder > session ID */
export function peerLabel(peer: Peer): string {
  if (peer.path) {
    const folder = peer.path.split("/").pop();
    if (folder) return folder;
  }
  return peer.name;
}

export interface Event {
  id: string;
  type: "query" | "response" | "notification" | "broadcast" | "status_change" | "chat_turn";
  timestamp: string;
  from?: string;
  to?: string;
  text: string;
  status?: "pending" | "success" | "error" | "blocked";
  peer?: string;
  role?: "user" | "assistant";
  new_status?: "online" | "busy" | "offline";
  query_id?: string;
  correlation_id?: string;
}
