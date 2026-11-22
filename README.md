# ax-orbi

ax-orbi runs Orbi and its Pi coding agent inside a Google AX sandbox. **Status: experimental; the path on a real AX cluster is not qualified yet.**

- **Orbi** selects eligible GitHub work and handles delivery. ax-orbi requests one Orbi tick at a time; it doesn't select work itself.
- **AX** hosts the sandbox, manages network access and credential injection, and checkpoints the workspace.
- **GitHub** holds the delivery record: issues, labels, branches and pull requests.

## Quick start

From the repository root, with Python 3.11+, Git, Make, and GNU coreutils `timeout` on `PATH`, copy the [example configuration](examples/minimal/ax-orbi.toml) and preview the deployment without a cluster:

```sh
cp examples/minimal/ax-orbi.toml ax-orbi.toml
bin/ax-orbi check --offline
bin/ax-orbi render
make test
```

The check validates the config and also checks for the AX CLI, so it can report valid config yet exit 1 if `ax` is missing. Rendering doesn't require `ax`; it prints a Gateway, a Workspace and a Task. The example uses placeholders, not a deployable image or credentials. Follow the [setup guide](docs/SETUP.md) to install the pinned AX CLI and replace the placeholders before deploying.

`make verify` runs `make test`, then builds the pinned image and runs the [container verifier](verify/container.sh) with Docker. To use an existing Docker image instead of building, run `make verify IMAGE="$IMAGE"` with `IMAGE` set to its tag or digest reference. Container verification exercises the runner and agent without a cluster; it does not qualify the sandbox, workspace snapshots, gateway or `ax ssh` on AX.

The existing verifier can fail in phase 5 with `Orbi rejected the generated configuration` after GitHub rejects the verifier's placeholder credentials. This is not an error in your TOML; the verifier does not read your deployment config or Secrets.

## Install the skill

Import or copy the entire [`skills/ax-orbi/`](skills/ax-orbi/) directory into your agent's skills location using its Agent Skills installation instructions. The [operator skill](skills/ax-orbi/SKILL.md) also needs shell access on the AX CLI host and a checkout of this repository; installing the skill doesn't grant AX access. Use the [setup guide](docs/SETUP.md) to configure a deployment.

## CLI

Run `bin/ax-orbi --help` for the command list. Every subcommand accepts `-c` / `--config` (default `./ax-orbi.toml`) and `--json`.

| Command | What it does |
| --- | --- |
| `check` | Checks prerequisites, version pins and the deployment configuration. |
| `build` | Builds the task image from `versions.toml` and optionally pushes it. |
| `render` | Prints the deployment's AX manifests. |
| `prepare` | Creates and starts the sandbox without starting a delivery. |
| `run` | Requests one Orbi tick and waits for the result by default. |
| `inspect` | Reports sandbox health, recent runs and their evidence. |
| `stop` | Checkpoints the sandbox, keeping state by default. |
| `recover` | Resumes a checkpointed sandbox and reports interrupted runs. |

Exit codes: `0` command succeeded (possibly with warnings), `1` operation, check or tick failed, `2` bad usage or configuration, `3` interrupted, pending or unknown outcome. `check --json` lists warnings in `unverified`; it checks digest syntax (`image-pinned`), not the running image's `image-pins`. See [Interpreting results](docs/SETUP.md#interpreting-results) for the `taskState` classes, pin verification and what `executionUnknown` means after a forced stop.

## Limits

- A tick that exits 0 does **not** mean a delivery merged. It can mean no eligible work was found. Check GitHub for the delivery outcome.
- Run only **one active Orbi deployment per repository**. Orbi's ownership locks are local files and do not coordinate across sandboxes.
- The sandbox can still use the full authority of the injected credentials on matching requests. Scope them to the disposable repository and provider account you intend to use.
- CPU and memory limits do not cap model spending. Use provider-side budgets or quotas.
- The Task sets `debug: true` so ax-orbi can reach the agent. Anyone with access to the AX atespace can run commands in the sandbox. Restrict atespace access accordingly.
