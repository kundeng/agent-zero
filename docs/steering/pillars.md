# Project Pillars: HyperAgent Zero

## Pillars

| Pillar | Current State | Done Criteria |
|--------|---------------|---------------|
| **MVP / Ship** | Foundation shipped (01, 03, 04, 06, 07). Channel provisioning UX (08) shipped: Settings → Channels tab + `haz channel` CLI wire Slack/Telegram/Discord end-to-end with no JSON editing. | Bot live in a real workspace from a fresh install in ≤5 min through the Web UI. Per-project channel binding routes per-thread. |
| **Test / Examples** | 155 channel + haz tests passing. Spec 08 ships 101 new tests across registry, contexts, all three provisioners, adapter hardening, and CLI. | Cross-channel E2E with a real bot under load. Live-workspace smoke harness behind a flag. |
| **Design / Arch** | 5 specs drafted. CLAUDE.md seeded. Steering docs created. | Specs match code after implementation. No stale claims in specs or steering. Reference docs generated. |
| **Documentation** | Upstream docs in `docs/`. No v2-specific docs. | Updated README reflecting v2 capabilities. Installation guide for host-mode. Configuration guide for providers. |
