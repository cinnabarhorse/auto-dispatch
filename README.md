# Auto Dispatch

Auto Dispatch is an explicit-only Codex skill that uses one ephemeral
GPT-5.6 Sol Max assessment to choose a GPT-5.6 model and reasoning effort,
starts the actual work in a new Codex task, and archives the source task after
the handoff completes.

## Requirements

- ChatGPT desktop with Codex and native task creation, waiting, and archival
  tools
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
- Archives the source task only after its handoff turn finishes; it never
  deletes the task.

Check local prerequisites without spending a model turn:

```bash
python3 scripts/run_assessor.py --check
```

## License

[MIT](LICENSE) © 2026 Cinnabar Horse

## References

- [Build skills in Codex](https://learn.chatgpt.com/docs/build-skills)
- [Choose Codex models and reasoning effort](https://learn.chatgpt.com/docs/models)
