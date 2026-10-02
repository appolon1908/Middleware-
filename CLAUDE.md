## Claude Code — Codestra Middleware

Read and obey `AGENTS.md` first; its single-lane, publication and production-safety rules govern this repository.

Mandatory first command:

```bash
./scripts/agent_preflight.sh --start --branch "$(git branch --show-current)"
```

Continuation protocol (cross-repository checkpoints and handoff):
https://github.com/ingtrader21-spec/codestra/blob/main/docs/AGENT-CONTINUATION-PROTOCOL.md

Then read `.codestra-mission/*` when present. Use the active Linear issue as task authority, preserve dirty local work, do not self-assign successor work, and end with the protocol's structured checkpoint.
