import type { NextConfig } from "next";

// Local dev only: the browser calls /chat on its own origin and Next forwards it to the agent.
// In production Caddy routes /chat to the agent before it ever reaches Next.js (no CORS either way).
// Rewrites are fixed at build time, so the Docker build passes AGENT_URL=http://agent:8001.
const agentUrl = process.env.AGENT_URL ?? "http://localhost:8001";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [{ source: "/chat", destination: `${agentUrl}/chat` }];
  },
};

export default nextConfig;
