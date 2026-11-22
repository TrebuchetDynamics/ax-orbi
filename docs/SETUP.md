# Set up ax-orbi

Use a disposable GitHub repository for your first deployment. Run the command blocks in the same Bash session and stop if a command fails. The [README](../README.md#limits) explains what a tick's exit code means and what the sandbox does not protect against.

## Prerequisites

- A checkout of ax-orbi, Python 3.11 or later, Git, Make, Bash and Go 1.27.1 or later to build the pinned AX CLI.
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

**Not yet verified on a live cluster.** Run each command from the ax-orbi checkout. Continue only after each succeeds.

```bash
bin/ax-orbi check --json
bin/ax-orbi prepare --json
bin/ax-orbi run --json
bin/ax-orbi inspect --json
```

- `check` should exit 0, possibly with warnings. If it reports `secret:github` or `secret:model` as not found, check the Kubernetes context and the `namespace/name` in the config, then create and label the corresponding Secret. If it warns that the label is missing, add it before preparing. A Gateway 403 can also mean the credential provider policy does not grant access.
- `prepare` applies the Gateway, Workspace and Task and checks agent readiness and image pins. It does not start a delivery. If the config is missing, copy the [example](../examples/minimal/ax-orbi.toml), edit it as above and retry.
- `run` requests one tick. Set `RUN_ID` to `run.id` from its JSON, then use `bin/ax-orbi inspect --json --run "$RUN_ID" --logs` to inspect the run and its log. Check GitHub for the actual issue and PR state: a tick with `status: succeeded` can find no eligible work.

## 6. Stop and recover

**Not yet verified on a live cluster.** Wait for the sandbox to be idle (`bin/ax-orbi inspect --json`), then stop it. A normal stop requires evidence of idleness and checkpoints `/workspace` rather than deleting your state. Recover when you want to use it again:

```bash
bin/ax-orbi stop --json
bin/ax-orbi recover --json
```

`recover` resumes the sandbox and reports interrupted run IDs in `interrupted`. Interrupted ticks are kept as evidence, not rerun; the next `bin/ax-orbi run --json` lets Orbi pick up delivery from the GitHub record. Requests that were queued before the stop may run on resume.

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
