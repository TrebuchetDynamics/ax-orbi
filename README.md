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

For live AX trials, see the [qualification runbook](docs/QUALIFICATION.md).

GitHub Actions runs offline tests and syntax checks on every push to `main` and every pull request, and runs container verification when image inputs change or on manual dispatch.

## Leave it working

After [configuring AX, the image and Secrets](docs/SETUP.md), register your repository's deployment and check its fleet status. The example name is `demo`:

```sh
bin/ax-orbi enable --config ./ax-orbi.toml
bin/ax-orbi status demo
```

Install the watch service using [Unattended operation](docs/SETUP.md#unattended-operation). It creates or resumes the sandbox and requests ticks without an operator command for each tick. Check status for progress and owner questions; pause the deployment before maintenance. This flow is demonstrated offline, not qualified on live AX.

## Install the skill

Import or copy the entire [`skills/ax-orbi/`](skills/ax-orbi/) directory into your agent's skills location using its Agent Skills installation instructions. The [operator skill](skills/ax-orbi/SKILL.md) also needs shell access on the AX CLI host and a checkout of this repository; installing the skill doesn't grant AX access. Use the [setup guide](docs/SETUP.md) to configure a deployment.

## CLI

Run `bin/ax-orbi --help` for the command list. Every subcommand accepts `--json`. The fleet commands (`enable`, `pause`, `resume`, `status`, `watch`) accept `--fleet DIR` to override `$AX_ORBI_FLEET` or the default deployment directory (`$XDG_CONFIG_HOME/ax-orbi/deployments`, normally `~/.config/ax-orbi/deployments`). Other commands accept `-c` / `--config` (default `./ax-orbi.toml`).

| Command | What it does |
| --- | --- |
| `check` | Checks prerequisites, version pins and the deployment configuration. |
| `build` | Builds the task image from `versions.toml` and optionally pushes it. |
| `render` | Prints the deployment's AX manifests. |
| `prepare` | Creates, starts or resumes the sandbox without requesting a new tick. |
| `run` | Requests one Orbi tick and waits for the result by default. |
| `inspect` | Reports sandbox health, recent runs and their evidence. |
| `stop` | Pauses an enabled deployment and checkpoints the sandbox, keeping state by default. |
| `recover` | Resumes a checkpointed sandbox and reports interrupted runs. |
| `enable NAME` or `enable --config PATH` | Enables a deployment for watch; `--config` also registers the file. By default, prepares the host and checks prerequisites first. `--no-install` skips host preparation and checks; `--build-image` builds and pushes the task image before host setup. |
| `pause NAME [--reason TEXT]` | Records a pause without changing the AX sandbox. |
| `resume NAME` | Re-enables a paused deployment without changing the AX sandbox. |
| `status [NAME]` | Shows fleet modes, watch activity and any open owner question with commands to run, without contacting AX. |
| `watch [--once] [--interval SECONDS] [--fleet DIR] [--print-unit]` | Keeps enabled deployments ticking and leaves paused ones alone. Sleeps 30 seconds between scans by default; `--once` scans once and `--print-unit` only prints a systemd user unit. See the [repair policy](docs/SETUP.md#repair-and-retry-policy) for retries and owner questions. |

Exit codes: `0` command succeeded (possibly with warnings), `1` operation, check or tick failed, `2` bad usage or configuration, `3` interrupted, pending or unknown outcome. `check --json` lists warnings in `unverified`; it checks digest syntax (`image-pinned`), not the running image's `image-pins`. See [Interpreting results](docs/SETUP.md#interpreting-results) for the `taskState` classes, pin verification and what `executionUnknown` means after a forced stop.

## Limits

- A tick that exits 0 does **not** mean a delivery merged. It can mean no eligible work was found. Check GitHub for the delivery outcome.
- Run only **one active Orbi deployment per repository**. Orbi's ownership locks are local files and do not coordinate across sandboxes.
- The sandbox can still use the full authority of the injected credentials on matching requests. Scope them to the disposable repository and provider account you intend to use.
- The service does not cap model spending. It requests a tick every `tick_interval` while healthy and idle, subject to the watch scan cadence. Use provider-side budgets or quotas.
- The Task sets `debug: true` so ax-orbi can reach the agent. Anyone with access to the AX atespace can run commands in the sandbox. Restrict atespace access accordingly.
