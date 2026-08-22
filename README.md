# Auto Dispatch

Auto Dispatch is an explicit-only Codex skill that uses one ephemeral
GPT-5.6 Sol Max assessment to choose a GPT-5.6 model and reasoning effort,
then starts the actual work in at most one new Codex task. It leaves the source
task active and stops before assessment when the source has an unfinished Goal,
because current task tools cannot transfer or archive one atomically.

## Requirements

- ChatGPT desktop with Codex and native task reading, project listing, and task
  creation tools
- Native Goal inspection
- Codex CLI available as `codex`
- Python 3
- A Unix-like local host with `/tmp` and `mktemp`
- Writable durable state under the current Codex profile
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

Auto Dispatch accepts only tasks with no Goal or a completed Goal. It leaves an
unfinished Goal untouched and stops before spending the assessment turn. Native
task creation, background archival, and Goal creation are separate operations;
without an atomic transfer primitive, automatically recreating a Goal could
duplicate work or refresh authorized spend.

Auto Dispatch never runs implicitly. Each invocation authorizes one Sol Max
routing assessment and at most one destination-task creation attempt. It does
not authorize source archival, merging, deployment, releases, third-party model
spend, or other external side effects.

The interruption guard uses a small SQLite database at
`${CODEX_HOME:-$HOME/.codex}/state/auto-dispatch.sqlite3`. It stores only
invocation identifiers, hashes, private temporary file paths, route/stage, and
a returned destination ID—not task content or credentials. The snapshot and
creation packet live only in owner-controlled `/tmp` directories; a resumed
turn cleans their exact recorded paths and never repeats an ambiguous external
call. Atomic reservations prevent concurrent turns from repeating either call.

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
- Uses the original assessment snapshot only within the uninterrupted dispatch
  turn; recovery fails closed and cleans it instead of reconstructing bytes.
- Validates the structured route before creating a destination task.
- Cleans only its validated assessor and creation-packet directories under
  `/tmp`, requires private owner-only directories, rejects symlinks, and requires
  the platform's no-follow cleanup path.
- Does not retry a failed paid assessment automatically.
- Leaves the source task active on every outcome.
- Keys each invocation to its exact source user-message ID so an interrupted
  source can stop safely without assessing or creating again.
- Reserves the paid assessment atomically before launch; ambiguous or interrupted
  calls remain fenced and are never repeated.
- Requires the assessor wrapper to match the current brief against the exact
  snapshot hash returned by that reservation before spending the model turn.
- Inherits the assessment lease into the assessor process so abrupt wrapper
  death cannot make an active snapshot eligible for cleanup.
- Binds the recorded route to the exact assessment snapshot hash.
- Reserves `create_thread` atomically against the exact creation-packet hash;
  any interruption after reservation permanently forbids recreation.
- Identifies its database with a fixed SQLite application ID and validates the
  full schema before any existing database is used; missing tables are never
  silently recreated.
- Creates the shared `state` directory privately only when absent and never
  changes an existing directory's permissions.
- Treats an empty `CODEX_HOME` as unset and rejects a relative non-empty value,
  so working-directory changes cannot split the reservation domain.
- Records a returned `threadId` or `clientThreadId` before reporting success.
- Treats ambiguous task creation as possibly committed, performs no unrelated
  task-history scan, and never retries it automatically.
- Stops before assessment when required context or the exact saved project
  cannot be forwarded unambiguously.
- Does not attempt cross-task Goal transfer, selection, reconciliation, or
  archival without an atomic platform primitive.

Check local prerequisites without spending a model turn:

```bash
python3 scripts/run_assessor.py --check
```

Validate the durable coordinator's schema, integrity checks, concurrency-safe
reservations, recovery behavior, restrictive permissions, and exact-directory
cleanup without spending a model turn:

```bash
python3 scripts/state.py self-test
```

## License

[MIT](LICENSE) © 2026 Cinnabar Horse

## References

- [Build skills in Codex](https://learn.chatgpt.com/docs/build-skills)
- [Choose Codex models and reasoning effort](https://learn.chatgpt.com/docs/models)
- [Using Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
