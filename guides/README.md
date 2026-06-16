# Vome Support Agent — Guides

Reference docs for how the support agent ("Vic") works and why it's built the
way it is. Start here if you're picking up this project fresh (or handing it to
a different assistant/teammate).

- **[vic-support-workflows.md](vic-support-workflows.md)** — Full reference for
  the support automation: the Vic/Sam sender model, every webhook-driven
  workflow (on-prod auto-send, needs-client-info, escalations, Zoho↔ClickUp
  status sync), the shared modules, configuration/setup, how it was tested, and
  the open follow-ups. Captures the changes made in the June 2026 build-out and
  the reasoning behind each decision.

> These guides are documentation only — they are not loaded into the agent at
> runtime. The agent's behavioral instructions live in `system_prompt.md`.
