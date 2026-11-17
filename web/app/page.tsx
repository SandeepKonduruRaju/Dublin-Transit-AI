"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";

type Role = "user" | "assistant";

type Message = {
  role: Role;
  content: string;
  sources?: string[];
  kind?: "error" | "limit"; // not sent back to the agent as history
};

type ToolCall = { name: string; args?: Record<string, unknown> };

const EXAMPLES = [
  "When is the next 39A from stop 767?",
  "Is the 39A running late right now?",
  "How reliable was route 15 this week?",
  "When is the next 46A?",
];

const SOURCE_LABELS: Record<string, string> = {
  get_route_delays: "live delays",
  get_departures: "timetable",
  get_on_time_performance: "reliability",
  get_route_info: "route check",
  search_stops: "stop search",
};

const MAX_TURNS = 10; // user + assistant pairs sent as history

function sourcesFrom(toolCalls: ToolCall[]): string[] {
  const labels = toolCalls.map((c) => SOURCE_LABELS[c.name]).filter((l): l is string => Boolean(l));
  return [...new Set(labels)];
}

// The agent sometimes uses **bold**; render that, keep everything else as plain text (no HTML injection).
function withBold(text: string) {
  return text.split(/(\*\*[^*\n]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
  );
}

function historyFrom(messages: Message[]): { role: Role; content: string }[] {
  return messages
    .filter((m) => !m.kind)
    .slice(-MAX_TURNS * 2)
    .map(({ role, content }) => ({ role, content }));
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || loading) return;

    const history = historyFrom(messages);
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    setLoading(true);

    let reply: Message;
    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      if (res.status === 429) {
        const seconds = Number(res.headers.get("Retry-After"));
        const wait = Number.isFinite(seconds) && seconds > 0 ? `${seconds} seconds` : "a little while";
        reply = { role: "assistant", kind: "limit", content: `You've hit the limit; try again in ${wait}.` };
      } else if (!res.ok) {
        reply = {
          role: "assistant",
          kind: "error",
          content: `Sorry, something went wrong (HTTP ${res.status}). Please try again.`,
        };
      } else {
        const data: { answer: string; tool_calls: ToolCall[] } = await res.json();
        reply = { role: "assistant", content: data.answer, sources: sourcesFrom(data.tool_calls ?? []) };
      }
    } catch {
      reply = {
        role: "assistant",
        kind: "error",
        content: "Couldn't reach the server. Check your connection and try again.",
      };
    }
    setMessages((prev) => [...prev, reply]);
    setLoading(false);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send(input);
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>Dublin Transit AI</h1>
        <p className={styles.subtitle}>Next buses, live delays and route reliability for Dublin.</p>
      </header>

      <main className={styles.chat} aria-live="polite">
        {messages.length === 0 && (
          <div className={styles.empty}>
            <p className={styles.emptyLead}>Try one of these:</p>
            <div className={styles.examples}>
              {EXAMPLES.map((q) => (
                <button key={q} type="button" className={styles.example} onClick={() => void send(q)} disabled={loading}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? styles.rowUser : styles.rowBot}>
            <div
              className={[
                styles.bubble,
                m.role === "user" ? styles.bubbleUser : styles.bubbleBot,
                m.kind === "error" ? styles.bubbleError : "",
                m.kind === "limit" ? styles.bubbleLimit : "",
              ].join(" ")}
            >
              <p className={styles.text}>{m.role === "assistant" ? withBold(m.content) : m.content}</p>
              {m.sources && m.sources.length > 0 && (
                <ul className={styles.chips} aria-label="Sources">
                  {m.sources.map((s) => (
                    <li key={s} className={styles.chip}>
                      {s}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className={styles.rowBot}>
            <div className={`${styles.bubble} ${styles.bubbleBot} ${styles.loading}`}>Checking live data…</div>
          </div>
        )}
        <div ref={endRef} />
      </main>

      {messages.length > 0 && (
        <div className={styles.examplesInline}>
          {EXAMPLES.map((q) => (
            <button key={q} type="button" className={styles.exampleSmall} onClick={() => void send(q)} disabled={loading}>
              {q}
            </button>
          ))}
        </div>
      )}

      <form className={styles.form} onSubmit={onSubmit}>
        <input
          className={styles.input}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about a route or stop…"
          aria-label="Your question"
          maxLength={1000}
          autoComplete="off"
        />
        <button className={styles.send} type="submit" disabled={loading || !input.trim()}>
          Send
        </button>
      </form>

      <footer className={styles.footer}>
        Data: National Transport Authority (GTFS / GTFS-Realtime). Answers can be wrong; check TFI Live before you
        travel.
      </footer>
    </div>
  );
}
