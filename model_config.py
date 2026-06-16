"""
model_config.py

Single source of truth for which Claude models the support agent uses.

Every Anthropic call site imports from here instead of hardcoding a model ID.
To upgrade to a newer model in the future, change the env var (or the default
below) in ONE place — no code edits across the handlers.

Why this exists: the old pinned snapshot `claude-sonnet-4-20250514` was retired
by Anthropic and started returning 404s in production, breaking ticket intake
and drafting. Centralizing the ID means the next model retirement/upgrade is a
one-line change, not a scavenger hunt across ~18 call sites.

Notes on the model IDs:
  * `claude-sonnet-4-6` is the current Sonnet (Sonnet 4.6). There is no
    "Sonnet 4.7" — 4.7/4.8 are Opus versions; the Sonnet line's latest is 4.6.
  * Use the bare alias (no date suffix). Anthropic resolves the alias to the
    current snapshot; appending a date pins you to a snapshot that will
    eventually be retired (exactly what broke us before).
  * There is intentionally no evergreen "latest" alias — Anthropic pins model
    IDs for output stability, so an env-backed constant is the right way to
    stay one edit away from any future upgrade.
"""

import os

# Primary model: classification, drafting, reasoning-heavy review calls.
# Override per-environment with SUPPORT_MODEL (e.g. when a newer Sonnet ships).
SUPPORT_MODEL = os.environ.get("SUPPORT_MODEL", "claude-sonnet-4-6")

# Fast/cheap model: lightweight classifiers (no-action detection, intake
# triage) where latency and cost matter more than peak reasoning.
SUPPORT_MODEL_FAST = os.environ.get("SUPPORT_MODEL_FAST", "claude-haiku-4-5")
