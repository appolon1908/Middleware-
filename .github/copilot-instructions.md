# Codestra Middleware agent instructions

Read and obey `/AGENTS.md` before changing anything.

Mandatory first command:

```bash
./scripts/agent_preflight.sh --start --branch "$(git branch --show-current)"
```

Do not create another implementation lane, alternate route/port/header contract, direct provider/database/SMTP path, or production-effect shortcut. Reuse existing authority and preserve unrelated worktrees as read-only evidence.

Before any commit/push handoff, run the repository's mission tests plus:

```bash
./scripts/agent_preflight.sh --certify --branch "$(git branch --show-current)"
```

A failed gate is a STOP condition, not permission to weaken or bypass the gate.

Continuation protocol (cross-repository checkpoints and handoff):
https://github.com/appolon1908/codestra/blob/main/docs/AGENT-CONTINUATION-PROTOCOL.md
`/AGENTS.md` governs wherever the two differ.
