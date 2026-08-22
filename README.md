# Auto Dispatch

Auto Dispatch is an explicit-only Codex skill that uses one ephemeral
GPT-5.6 Sol Max assessment to choose a GPT-5.6 model and reasoning effort,
starts the actual work in a new Codex task, and archives the source task after
the handoff completes. If the source has an active Goal, Auto Dispatch also
transfers its objective when the Goal is unbudgeted.

## Requirements

- ChatGPT desktop with Codex and native task listing, reading, creation,
  waiting, and archival tools
- Native Goal inspection; Goal creation when transferring an active Goal
- Codex CLI available as `codex`
- Python 3
- A Unix-like local host with `/tmp` and `mktemp`
- Access to the selected GPT-5.6 models

## Install

Ask Codex to install this GitHub skill:

```text
Use $skill-installer to install https://github.com/cinnabarhorse/auto-dispatch
```

Or clone it into the user skill directory:

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/cinnabarhorse/auto-dispatch.git \
  "$HOME/.agents/skills/auto-dispatch"
```

Codex normally detects skill changes automatically. Restart Codex if the skill
does not appear.

## Use

Select **Auto Dispatch** from the Skills picker, or mention it explicitly where
`$` skill mentions are supported:

```text
$auto-dispatch Fix the flaky sync test and open a pull request.
```

To transfer an existing Goal, set it first and then invoke Auto Dispatch in the
same task:

```text
/goal Reduce p95 latency below 120 ms while keeping correctness tests green
```

After the Goal is active:

```text
$auto-dispatch Transfer this active Goal to the best route.
```

Only active unbudgeted Goals transfer. Auto Dispatch stops before assessment
for an explicitly budgeted Goal because current task tools cannot atomically
read its remaining budget after source archival; recreating a stale snapshot
could refresh authorized spend. It also leaves paused, blocked, and
budget-limited Goals untouched so the user can resolve their lifecycle state.

Auto Dispatch never runs implicitly. Each invocation authorizes one Sol Max
routing assessment, creation of one destination task, and recoverable archival
of the source task. It does not authorize merging, deployment, releases,
third-party model spend, or other external side effects.

## Routing

The assessor chooses the lowest route likely to complete the task well:

| Route | Intended work |
| --- | --- |
| Luna low | Trivial, deterministic, self-contained tasks |
| Luna medium | Clear extraction, transformation, or repetitive work |
| Terra low | Narrow everyday work with simple verification |
| Terra medium | Routine implementation or analysis using several tools |
| Terra high | Non-trivial debugging or multi-file work |
| Terra extra high | Deep investigation or review needing extra checks |
| Sol medium | Bounded but ambiguous, high-value, or polish-sensitive work |
| Sol high | Complex implementation, research, or design with tradeoffs |
| Sol extra high | Difficult architecture, security, review, or long-horizon work |
| Sol max | The hardest consequential single-agent work |

Ultra is intentionally excluded because it authorizes subagents rather than
only changing single-agent reasoning effort.

## Safety behavior

- Runs the assessor read-only and ephemerally.
- Passes task content through a file instead of interpolating it into a shell
  command.
- Validates the structured route before creating a destination task.
- Cleans only its validated `/tmp/auto-dispatch.*` scratch directory.
- Does not retry a failed paid assessment automatically.
- Leaves the source task active if routing or destination creation fails.
- Keys each invocation to its exact source user-message ID so an interrupted
  handoff recovers the existing destination instead of assessing or creating
  again.
- Treats ambiguous task creation as possibly committed and never retries it
  automatically.
- Makes the destination archive the exact source and verify the archive receipt
  before activating a transferred Goal.
- Reconciles ambiguous Goal creation through destination-local Goal inspection.
- Restores and verifies the source if archival or Goal activation cannot be
  proven.
- For transfers without an active Goal, waits for the source handoff turn and
  still requires an exact archive receipt before work starts.

Check local prerequisites without spending a model turn:

```bash
python3 scripts/run_assessor.py --check
```

## License

[MIT](LICENSE) © 2026 Cinnabar Horse

## References

- [Build skills in Codex](https://learn.chatgpt.com/docs/build-skills)
- [Choose Codex models and reasoning effort](https://learn.chatgpt.com/docs/models)
- [Using Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
