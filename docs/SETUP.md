# Set up ax-orbi

Use a disposable GitHub repository for your first deployment. Run the command blocks in the same Bash session and stop if a command fails. The [README](../README.md#limits) explains what a tick's exit code means and what the sandbox does not protect against.

## Prerequisites

- A checkout of ax-orbi, Python 3.11 or later, Git, Make, Bash, GNU coreutils `timeout`, and Go 1.27.1 or later to build the pinned AX CLI.
- For the local AX stack: `curl`, a running rootful Docker engine, and a container host (or VM) with Linux kernel 6.8+ and at least 4 GiB of RAM (6 GiB or more recommended). AX can fetch `kind` and `kubectl` if they're not on `PATH`. See the pinned [AX local installer guide](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/local_installer.md) and [installation guide](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/installation.md).
- A disposable GitHub repository with at least one commit on the base branch. Use a personal access token scoped to that repo, allowing Orbi to push branches and manage issues and pull requests, and a key for your HTTPS model API. Store neither in TOML. You also need permission to create and label Kubernetes Secrets in `ax-system`.

## 1. Build the pinned AX CLI

**Not yet verified on a live cluster.** Start in the ax-orbi repository. The checkout below uses the AX commit in [`versions.toml`](../versions.toml); use that same commit for the CLI, server and image runner rather than installing a release or `@main`.

```bash
AX_ORBI_ROOT="$PWD"
cd ..
git clone https://github.com/google/ax.git ax-source
cd ax-source
git checkout --detach 2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2
export GOTOOLCHAIN=auto
go build -o "$PWD/bin/ax" ./cmd/ax
export PATH="$PWD/bin:$PATH"
ax local --help
```

## 2. Start AX

**Not yet verified on a live cluster.** In the pinned AX source checkout, use the [documented local build target](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/local_installer.md#using-locally-built-images). It starts or reuses the loopback registry on port 5001 by default, builds the matching AX server image and calls `ax local start` with that server's digest. Leave `AX_RUNNER_VOLUME_IMAGE` unset so tasks use the runner baked into the ax-orbi image.

```bash
make build-local-cluster CONTAINER_CLI=docker
ax local status
ax ctx
```

The status should show the stack ready and `ax ctx` should mark `ax-local` active. If a layer isn't ready, check `ax local status` before continuing. For a pre-existing cluster instead, follow AX's [existing-cluster instructions](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/local_installer.md#step-5-deploy-to-a-remote-gke--substrate-cluster-ax-ctx--ax-install) at this pin. That cluster needs a registry it can pull from and a credential provider policy allowing the labeled Secrets for your atespace.

## 3. Build, push and configure the task image

**Not yet verified on a live cluster.** Back in the ax-orbi checkout, build and push to the AX local registry and make your deployment config:

```bash
cd "$AX_ORBI_ROOT"
bin/ax-orbi build --engine docker --push
cp examples/minimal/ax-orbi.toml ax-orbi.toml
```

Edit `ax-orbi.toml`: set `repository.source` and `repository.base_branch` for the disposable repository, replace the all-zero `image.ref` with the digest reference printed by the build, and fill in the model provider, ID and HTTPS base URL. The example uses an OpenAI-compatible API with `Authorization: Bearer …`; match the provider's API type and header format. Keep the example Secret references for the next step. If the local registry uses a different port, or you are targeting an existing cluster, set `IMAGE_REPOSITORY` to an image repository (without a tag) your build can push to and AX can pull from, and pass `--repository "$IMAGE_REPOSITORY"` to `build`. The local AX stack pulls images anonymously. Set `ax.context` if you need to target a context other than the active one.

## 4. Create the Kubernetes Secrets

**Not yet verified on a live cluster.** The example references `ax-system/orbi-demo-github` and `ax-system/orbi-demo-model`. Use the same Kubernetes context for the Secrets and the deployment. The local installer permits the credential provider to read Secrets in `ax-system` with the label `ax.io/credential=true`; on an existing cluster the administrator must grant this explicitly as described in [AX's credential injection documentation](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/manifests.md#injecting-credentials).

The GitHub Secret's `token` is the raw PAT. Its `basic` value is the single-line Base64 encoding of `GITHUB_USERNAME:TOKEN`, without a newline or `Basic ` prefix. [GitHub uses the PAT as the HTTPS password](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#using-a-personal-access-token-on-the-command-line), and AX adds the `Basic ` prefix before the encoded value. The model Secret's `api-key` is the raw model key; AX adds the configured prefix. Base64 is encoding, not encryption: treat both forms of the PAT as secrets.

Run this in Bash. If the installer downloaded `kubectl`, add `$HOME/.ax/bin` to `PATH`. Replace `ax-local` below if using a different context. The prompts keep token values out of the command line and shell history.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  secrets=$(mktemp -d)
  trap 'rm -rf "$secrets"' EXIT
  read -rp 'GitHub username: ' github_user
  read -rsp 'GitHub PAT: ' github_token; printf '\n'
  read -rsp 'Model API key: ' model_key; printf '\n'
  printf '%s' "$github_token" > "$secrets/token"
  printf '%s:%s' "$github_user" "$github_token" | base64 | tr -d '\r\n' > "$secrets/basic"
  printf '%s' "$model_key" > "$secrets/api-key"

  kubectl --context ax-local -n ax-system create secret generic orbi-demo-github \
    --from-file=token="$secrets/token" --from-file=basic="$secrets/basic"
  kubectl --context ax-local -n ax-system label secret orbi-demo-github ax.io/credential=true
  kubectl --context ax-local -n ax-system create secret generic orbi-demo-model \
    --from-file=api-key="$secrets/api-key"
  kubectl --context ax-local -n ax-system label secret orbi-demo-model ax.io/credential=true
)
```

## 5. Check, prepare, run and inspect

**Not yet verified on a live cluster.** Run each command from the ax-orbi checkout. Continue only after each succeeds. If reusing a deployment name, inspect first: both `prepare` and `run` can resume a suspended task, and queued requests can execute on resume.

```bash
bin/ax-orbi check --json
bin/ax-orbi prepare --json
bin/ax-orbi run --json
bin/ax-orbi inspect --json
```

- `check` should exit 0, possibly with warnings. If it reports `secret:github` or `secret:model` as not found, check the Kubernetes context and the `namespace/name` in the config, then create and label the corresponding Secret. If it warns that the label is missing, add it before preparing. A Gateway 403 can also mean the credential provider policy does not grant access.
- `prepare` applies the Gateway, Workspace and Task and checks agent readiness and image pins. It does not request a new tick. If the config is missing, copy the [example](../examples/minimal/ax-orbi.toml), edit it as above and retry.
- `run` requests one tick. Set `RUN_ID` to `run.id` from its JSON, then use `bin/ax-orbi inspect --json --run "$RUN_ID" --logs` to inspect the run and its log. Check GitHub for the actual issue and PR state: a tick with `status: succeeded` can find no eligible work.

## 6. Stop and recover

**Not yet verified on a live cluster.** Wait for the sandbox to be idle (`bin/ax-orbi inspect --json`), then stop it. A normal stop requires evidence of idleness and checkpoints `/workspace` rather than deleting your state. Recover when you want to use it again:

```bash
bin/ax-orbi stop --json
bin/ax-orbi recover --json
```

`recover` resumes the sandbox and reports interrupted run IDs in `interrupted`. Interrupted ticks are kept as evidence, not rerun; the next `bin/ax-orbi run --json` lets Orbi pick up delivery from the GitHub record. Requests that were queued before the stop may run on resume.

## Unattended operation

**Demonstrated offline; not yet qualified on live AX.** Follow the prerequisites and configure the pinned image and Secrets before enabling work. Use one active deployment per repository. From the ax-orbi checkout, register the config (whose `name` is `demo` in this example) and check what watch sees:

```bash
timeout -s INT 3h bin/ax-orbi enable --config ./ax-orbi.toml
bin/ax-orbi status --json demo
```

The fleet directory defaults to `${XDG_CONFIG_HOME:-$HOME/.config}/ax-orbi/deployments`. Keep one `*.toml` file or symlink per deployment name there. Registering by path creates a symlink to the config, so keep the original file in place. For an already registered config, use `timeout -s INT 3h bin/ax-orbi enable demo`. Fleet commands accept `--fleet DIR`, which takes precedence over `AX_ORBI_FLEET` and the default. Host state defaults to `${XDG_STATE_HOME:-$HOME/.local/state}/ax-orbi` (override with `AX_ORBI_HOME`). This holds `watch.lock` and, under `deployments/<name>/`, `desired.json` (including pauses), `watch.json` (including tick IDs and backoff), and `events.jsonl` (repairs, questions and notifications). Sandbox run evidence lives separately in `/workspace/.ax-orbi`.

By default `enable` checks the host and builds the pinned AX command under the state directory if the default `ax` is missing or doesn't match. It starts a local cluster only if AX is unreachable and `[ax] local = true`; the local startup still needs the tools in [Prerequisites](#prerequisites). To skip installs, cluster startup and host checks on a host you've already prepared, use `bin/ax-orbi enable --no-install demo`. `timeout -s INT 5h bin/ax-orbi enable --build-image demo` builds and pushes the image before host setup, so the default registry (`localhost:5001`) must already be reachable. If the pushed digest differs, update `image.ref` and retry; don't overwrite an existing deployment's immutable config. Watch does not install host dependencies or build images.

### Start the user service

On a host with a systemd user manager, generate and install the unit from the checkout you intend to keep:

```bash
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR" &&
  bin/ax-orbi watch --print-unit > "$UNIT_DIR/ax-orbi-watch.service" &&
  timeout 30s systemctl --user daemon-reload &&
  timeout 30s systemctl --user enable --now ax-orbi-watch.service &&
  timeout 30s systemctl --user --no-pager status ax-orbi-watch.service
```

The generated unit uses the absolute path to this checkout. Make sure the user manager can find Python and AX (use an absolute `ax.bin` for a custom AX binary), and sees the same fleet/state directories and AX context as your shell. If you use overrides, set them in the service environment rather than only exporting them in a terminal. To run when logged out or across reboot without logging in, have the host administrator enable lingering for the service account (`timeout 30s loginctl enable-linger USER`). Do not run another watch with the same state directory.

Check `bin/ax-orbi status --json demo` after starting the service. Look for `desired: enabled`, recent watch activity, and `watch.lastTick.status` set to `succeeded`. If watch hasn't made a pass or the service isn't running, check `timeout 30s journalctl --user --user-unit=ax-orbi-watch.service --no-pager -n 50` for service errors. If it reports the watch lock is held, stop the other watch instance before starting this one.

```bash
bin/ax-orbi status
bin/ax-orbi pause demo --reason "maintenance"
bin/ax-orbi status --json demo
bin/ax-orbi resume demo
```

Pause persists across service and host restarts. It prevents new watch actions; it does not cancel a tick already requested or suspend the sandbox. `stop` also pauses an enabled deployment before checkpointing it; `recover` alone does not re-enable watch. `resume` re-enables scheduling without acting directly on AX. `status` only reads local state: `desired` is `not-enabled`, `enabled`, or `paused`, and `watch.phase` is the last observed activity, if watch has made a pass. Check `watchAgeSeconds` and the detail before treating it as current.

### Configure tick timing, notifications and the project recipe

```toml
[watch]
tick_interval = 300
# notify_command = ["/home/operator/bin/notify-orbi"]
recreate_failed = false

[project]
setup = "scripts/ax-orbi-setup.sh"
check = "scripts/ax-orbi-check.sh"
setup_timeout = 1800
```

- `tick_interval` defaults to 300 seconds (minimum 60). Watch waits this long after observing a successful tick before requesting the next one; the service normally sleeps 30 seconds between fleet passes. This is not a spending cap.
- `notify_command` is an optional argument array executed directly on the **host**, without a shell. The command receives one JSON object on stdin with `deployment` and `question` (including `id`, `cause`, `text`, `options`, `openedAt` and `notifiedAt`), plus the `AX_ORBI_DEPLOYMENT` and `AX_ORBI_QUESTION_ID` environment variables. Exit 0 marks the question notified; stdout and stderr are discarded. Failures or the 30-second timeout are logged to `events.jsonl` and may be retried on later passes, for at most three attempts per question. Use the question ID (and `openedAt` for a later recurrence) to avoid duplicate alerts if a delivery fails after sending.
- `recreate_failed` defaults to `false`. Set it to `true` only if watch may delete a failed sandbox and **all of `/workspace`**, including unpushed work and tick evidence.
- `[project]` is optional. Commit an idempotent `setup` script and, preferably, a quick `check` script that exits 0 only when dependencies are usable. For example, have setup install the repo's locked dependencies and have check verify that the expected executable can run. Paths must resolve to files inside the repository. Scripts run from the checkout with the sandbox's network rules and credentials; include needed package hosts in `[network].allow` and don't put credentials in the recipe.
- With a check, the agent probes at start and before ticks, runs setup if the check fails, and checks again before running Orbi. Without a check, setup runs once per supervisor start. `setup_timeout` defaults to 1800 seconds (60–7200); each check has a separate 300-second limit. Inspect the prepare checks and `/workspace/.ax-orbi/project-setup.log` if setup fails. Changing the `[project]` table needs a new deployment name because the Task is immutable. Pause and stop the old deployment before enabling the new one.

For a disposable recipe trial, commit these two scripts, then remove the marker
between ticks to simulate a missing dependency. For a real deployment, replace
marker creation with the installation step and test actual dependency usability.

```sh
# scripts/ax-orbi-setup.sh
set -eu
mkdir -p .orbi
: > .orbi/dependency
```

```sh
# scripts/ax-orbi-check.sh
test -f .orbi/dependency
```

These examples use non-executable script files, which the agent runs with
`sh`. Give executable scripts a shebang.

### Repair and retry policy

| Observation | What watch does |
| --- | --- |
| Task missing or suspended | Applies the deployment or resumes the checkpoint, then waits for readiness. A recorded interrupted tick is kept, not replayed. |
| AX unreachable | Backs off and checks for AX again; it does not treat the task as missing. |
| Agent remains down after the 120-second startup window, or sandbox readiness checks fail | Tries to restart by suspending then resuming. It defers restart while a live tick is active. A missing dependency with a working recipe is normally restored by the agent before the next tick. |
| Tick failed | Backs off and requests a new tick when due. A successful tick closes the question and resets backoff. |
| Failed AX task (`Failed`, `Cancelled`, `Completed`, `Terminating`) | Asks the owner unless `recreate_failed` explicitly allows bounded deletion and recreation. |
| Unrecognized AX state, invalid enabled config, or image pin mismatch | Asks the owner immediately. It does not delete for an unrecognized state or rebuild an image. |

For a recurring retryable fault, the first two failures wait 60 and 300 seconds respectively; the third opens one owner question and waits 900 seconds. For repairs with this retry budget, watch can try the repair on the first two failures, but not on the third. Later unresolved checks or tick retries use a 30-minute backoff rather than opening another question. A different cause starts a fresh budget. Read the question and its options in `status`; for example, if ticks keep failing, use `timeout 30m bin/ax-orbi inspect --config ./ax-orbi.toml --logs` (substitute the affected deployment's config), fix the provider or GitHub access, then let watch confirm recovery. Do not follow a destructive option without explicit authorization.

| `watch.phase` | Meaning |
| --- | --- |
| `preparing` | Watch applied the missing sandbox resources. |
| `resuming` | Watch requested restoration of a checkpointed sandbox. |
| `starting` | Waiting for the supervisor and its startup checks. |
| `ticking` | A tick was requested, queued or running. |
| `idle` | The sandbox is ready; waiting until the next tick is due. |
| `waiting` | No safe changing step now (for example AX is unavailable or transitioning); see `detail`. |
| `repairing` | A repair is in progress, deferred, or in backoff. |
| `needs-owner` | An owner question is open; see `watch.question.text` and `watch.question.options`. |
| `paused` | Watch is leaving the deployment alone. |
| `config-error` | Watch could not load the config; see the config error. An invalid enabled config may instead be `needs-owner`. |

## Interpreting results

Exit code `0` means the command succeeded, not necessarily that the sandbox is running or a delivery merged. `1` is a failed operation, check or tick (`check` also uses `1` for invalid config); `2` is bad usage or configuration; `3` means interrupted, still pending or unknown. Read each check's `detail` and `fix` when present. `ok: null` is a warning. `check --json` lists these warning names in `unverified`, even when it exits 0. At the pinned AX revision, `ax-server` remains unverified because AX does not report the server revision. A check skipped with `--offline` is not verified either.

`check` reports `image-pinned` for a configured digest reference. `prepare`, `run`, `inspect` (on an active task) and `recover` use `image-pins` to compare the running image's reported pins with [`versions.toml`](../versions.toml). If the current boot has no prepare report, retry inspect after startup. For missing or mismatching pins in the report, rebuild and push the image, set its new digest in `image.ref`, and prepare under a new deployment `name`. Do not leave two active deployments on the same repository.

`taskState` records what AX reported, which can be the state observed before a resume. Use a fresh inspect to see the current state.

| `taskState` | `inspect` exit | Meaning |
| --- | --- | --- |
| `Working` | `0` or `1` | Active; inspect also checks the live supervisor, readiness and image pins. |
| `Suspended` | `0` | Checkpointed; state kept, nothing running. |
| `Queued`, `Suspending` | `3` | Transitional. Prepare, run and recover wait up to their startup budget; inspect does not. |
| `Failed`, `Cancelled`, `Completed`, `Terminating` | `1` | Not usable; follow the task diagnosis in `fix`. |
| `Idle`, `RequiresAction`, `Unknown`, or an unrecognized label | `3` | State unrecognized by ax-orbi; do not treat it as checkpointed. |
| `NotFound` | `1` | Task absent in the selected context and atespace. Normal stop is a no-op. |
| `null` | `1` | The task lookup failed; check the AX control plane and context. Not proof that the task is absent. |

A normal stop exits `1` if a tick is observed running, or `3` if it cannot confirm idleness and does not know of a running tick. Inspect before retrying. A forced stop can succeed and still report `executionUnknown: true`; that means execution at the time of suspension couldn't be confirmed, not that a tick definitely stopped or failed.
