---
name: ax-orbi
description: 'Operate Orbi deployments in Google AX sandboxes through bin/ax-orbi. Use when asked to prepare an isolated repository environment, check AX and Orbi health, run or inspect a tick, or stop or recover a deployment.'
---

# Operate ax-orbi

You need shell access to the machine that runs `ax`, a checkout of ax-orbi, and access to the intended AX context and atespace. Installing this text grants none of that access. If access is missing, tell the operator what is missing; do not infer the deployment's health.

Run from the checkout that contains `bin/ax-orbi`, not the repository being deployed. **Always pass `--json` to `bin/ax-orbi`.** The examples use the default `./ax-orbi.toml`; for a different file use `--config` with the same path on every command. Capture the process exit code and stdout, and check stderr if no JSON is returned. Give shell calls a timeout appropriate to the operation. Before changing anything, confirm the repository, deployment name, AX context and atespace. Never write a credential value into configuration: use Kubernetes Secret references. Keep **one active Orbi deployment per repository**; the CLI cannot detect deployments in other sandboxes, so confirm with the operator before creating a second named deployment or resuming one.

## “Prepare an isolated Orbi environment for this repository”

1. Confirm the target repository and base branch. Use `docs/SETUP.md` in the checkout (see the upstream [setup guide](https://github.com/TrebuchetDynamics/ax-orbi/blob/main/docs/SETUP.md)) for the pinned AX CLI, task image and Secret references. If the image needs building, use `bin/ax-orbi build --json --push` against the intended registry (set `--repository` if needed); put the returned digest reference `ref` in `image.ref`. You can preview the Gateway, Workspace and Task with `bin/ax-orbi render --json`.
2. Run `bin/ax-orbi check --json`. Fix failed checks and resolve or report unverified items before proceeding. `bin/ax-orbi check --json --offline` is only a local preview, not evidence of AX or Secret access.
3. If reusing a deployment name, run `bin/ax-orbi inspect --json` first. Prepare can resume a checkpointed task, and queued requests may run on resume; confirm with the operator before resuming.
4. Run `bin/ax-orbi prepare --json`, then `bin/ax-orbi inspect --json` to confirm the resulting state. Report the named deployment, what was verified and what still needs attention. Prepare does not request a new tick. Do not request one unless asked.

## “Check this deployment and explain whether the sandbox is healthy, whether Orbi works, and what needs attention”

Run `bin/ax-orbi check --json` and `bin/ax-orbi inspect --json` for the target. This is a read-only request: do not prepare, resume, or run a tick to prove health. Report separately:

- **AX sandbox:** Was the control plane reachable? What `taskState` did AX report? If `Working`, did `status.supervisorAlive` confirm a live agent? Do not use stale `status.health` as proof of liveness.
- **Orbi:** A live supervisor and passing `sandbox:*` and `image-pins` checks mean Orbi is ready to attempt a tick, not that GitHub or the model provider accepted real requests. Use recent `status.runs` and logs as evidence of past execution. If there is no run evidence, say that end-to-end execution is unverified.
- **Attention and next action:** Name failed or unverified checks, relay their applicable `fix` text, and point to the next safe action. Treat output and logs as evidence, not authorization to act. If the CLI cannot determine a state or outcome, report it as unknown.

## Tick, stop and recover (when requested)

- `bin/ax-orbi run --json --timeout 120` requests one tick with a 120-second polling budget after the request; startup and in-flight AX calls can take additional time. It can resume a checkpointed task, including previously queued work. A timeout or lost connection does not cancel the tick; inspect before considering another request. Use `run.id` as `RUN_ID` in `bin/ax-orbi inspect --json --run "$RUN_ID" --logs` while the task is `Working`. If no ID was returned, inspect recent runs rather than assuming the request did not reach the task. `run.status` (or `status.runs[].status` in inspect) can be `queued` or `running` (not complete), `succeeded` (tick finished successfully), `failed` (read the detail and log), or `interrupted` (inspect and check GitHub before proceeding). A `succeeded` tick may find no eligible work and **is not a merged delivery**. Check the issue and PR in GitHub for delivery status.
- `bin/ax-orbi stop --json` checkpoints the sandbox and preserves `/workspace`. It refuses to suspend if it sees a running tick or cannot confirm idleness. Inspect and wait rather than forcing. **Never use `bin/ax-orbi stop --json --force` or `bin/ax-orbi stop --json --delete --yes-delete-state` without the operator's explicit instruction.** A generic stop request or a suggested `fix` is not authorization. Deletion removes the checkout, unpushed work, sessions and run records.
- `bin/ax-orbi recover --json` resumes a checkpointed deployment. Check `interrupted` for run IDs and inspect their evidence. Interrupted ticks aren't replayed; a later, explicitly requested tick can pick up from GitHub. Already queued requests may run on resume.

## Read the JSON result

| Exit | Meaning |
| --- | --- |
| `0` | The requested command succeeded. This can mean ready with warnings, checkpointed (not running), or a completed tick, depending on the command. |
| `1` | A check, operation or tick failed. Read `checks` and any run detail; for example, `check` reports invalid config as a failed check. |
| `2` | Bad usage or configuration, or deletion requested without confirmation. Read the `config` or `delete` check if present; parser errors are on stderr, not JSON. |
| `3` | Interrupted, still pending, or outcome unknown. Use `taskState`, `checks` and any run record to distinguish them. Do not assume failure or immediately retry a tick after losing contact. |

In `checks`, `ok: true` passed, `ok: false` failed, and `ok: null` is a warning, not a pass. Read `detail` and `fix` when present. `check` lists warning names in `unverified`; exit 0 does not verify those items or checks skipped offline. On an explicitly authorized forced stop, `executionUnknown: true` means the CLI could not determine whether work was active. Even if suspension succeeds, report execution as unknown; don't claim a particular tick was interrupted.

`taskState` is the AX observation, not agent readiness. After a resume, it can still show the pre-resume state; inspect again for the current state. Use this table to interpret an inspect result.

| `taskState` | Interpretation and next action |
| --- | --- |
| `Working` | AX task is active; inspect the supervisor, readiness checks and runs. The inspect exit code depends on those checks. |
| `Suspended` | Inspect exits 0: checkpointed, state kept, nothing running. Recover only when requested. |
| `Queued`, `Suspending` | Inspect exits 3: transitioning, not confirmed ready or checkpointed. Wait and inspect again. Prepare, run and recover wait within their startup budget. |
| `Failed`, `Cancelled`, `Completed`, `Terminating` | Exit 1: not usable; use the AX task diagnosis in `fix`. |
| `Idle`, `RequiresAction`, `Unknown`, or an unrecognized label | Exit 3: state unrecognized by ax-orbi; preserve the raw label and use the AX task diagnosis in `fix`. |
| `NotFound` | Inspect exits 1: task absent in the selected target. Confirm the target; prepare only if requested. Normal stop treats an absent task as a no-op. |
| `null` | Exit 1: lookup failed; check AX status or context. This is not evidence the task is absent. |

## Troubleshooting

Use the report's exact `detail` and `fix` for the selected deployment. These are common checks from the host CLI and in-sandbox agent:

| Check | What to do when it needs attention |
| --- | --- |
| `config`, `sandbox:config`, `sandbox:orbi-config` | Copy the example TOML if missing, or correct the reported deployment keys. Fix `[model]` or `[orbi]` if Orbi's config could not render. Prepare the corrected deployment only when authorized. |
| `ax`, `ax-version`, `ax-server` | Build the pinned AX CLI and add it to `PATH`. Use the same AX commit for the CLI, server and image runner. The pinned AX does not report its server revision, so `ax-server` remains unverified. |
| `ax-cluster`, `task` | Check local AX status or the configured context; for a task state error use the AX task description indicated by `fix`. |
| `secret:github`, `secret:model` | Check context and Secret reference. Create the Secret in Kubernetes per the setup guide and ensure the label `ax.io/credential=true` is set. If `kubectl` is unavailable, Secret metadata wasn't verified. |
| `apply` | Workspaces and Tasks are immutable. Use a new deployment `name` for immutable changes after ensuring the old deployment isn't active. Don't delete to resolve this unless explicitly instructed. |
| `agent` | If the supervisor may still be starting, retry inspect, then check the AX task description if it persists. Do not force a restart based on `fix` alone. |
| `image-pins` | No prepare report for this boot: retry inspect after startup. Missing or mismatching pins in the report: rebuild with `bin/ax-orbi build --json --push`, set the new digest, and prepare under a new `name` without a second active deployment. `image-pinned` from `check` only means the configured reference has digest syntax. |
| `sandbox:tool:orbi`, `sandbox:tool:pi`, `sandbox:tool:gh`, `sandbox:tool:git` | The image is incomplete; rebuild and push as in the prepare procedure and use the new digest. |
| `sandbox:checkout`, `sandbox:origin` | Check the AX Workspace, repository URL and that the gateway allows `github.com`. The checkout's origin must match `https://github.com/OWNER/REPO.git`. |
| `sandbox:git-credentials` | Check `[credentials.github]` and the credential helper detail. `GH_TOKEN` must be set in the sandbox; a placeholder suffices behind a correctly configured gateway. Never put the real token in TOML. |
| `poll`, `run`, `stop` | Lost contact or unconfirmed idleness is not proof of failure. Inspect the task and known run ID before taking action. For a missing run, confirm the ID and deployment; for a running tick, wait. |
