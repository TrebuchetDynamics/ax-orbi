# Live qualification runbook

## Summary and decision

- Qualification summary: **NOT RUN**
- Demonstrated advantage over Docker and its operating cost: **NOT RUN**
- Owner's go/no-go decision and rationale: **NOT RUN**
- Offline only — [`tests/test_unattended.py`](../tests/test_unattended.py): **PASS** against fake AX and Orbi for dependency restoration, interruption without tick replay, outage/backoff and recovery with one notification, pause across watch restarts, and continued ticks on a second deployment while the first needs its owner. This does not qualify live AX, GitHub, a provider, or a systemd service.

This is a blank report for a future live trial, not a qualification claim. Use
`PASS` when the stated rule is met with evidence, `FAIL` when a valid trial
misses it, and `NOT RUN` with a reason when a prerequisite or fault state was
not obtained. Offline tests and container verification are not live AX evidence.

## Environment and versions

Record both machines' OS, kernel, architecture and available memory; the AX
cluster type, context and atespace; the ax-orbi checkout SHA and image digest
(and platform); the base commit, provider/model and resource limits; and Docker
client/server versions. Save the **literal `ax version` output**, `check --json`
(including `unverified`), and the image pins and tool versions from `prepare` or
`inspect`. Record how the server was built; don't fill in an observed server
revision when `ax-server` says it was not verified. See the [version pins](../versions.toml).

Get the owner's permission for the cluster, second machine, disposable GitHub
fixture and bounded model spending. Use a disposable Linux host or VM for
fault and memory tests. Use short-lived credentials scoped to the fixture and
model budget. Keep **one active deployment per repository**, including Docker;
start with no other scheduler, queued ticks or eligible Issues. Use the same
fixture and one model throughout. In the published report use aliases for
repository, host and account names; keep actual Issue/PR URLs and raw evidence
in the operator's restricted record. Redact credentials from all evidence,
including logs, before sharing. Don't record environment dumps, Secret contents
or HTTP authorization headers.

Use Bash from the ax-orbi checkout. The procedures use `./ax-orbi.toml` unless
a different file is specified. For direct AX commands, set `CONTEXT` to
`ax.context` (or the active context if unset) and `ATESPACE` to `ax.atespace`
(default `default`). Set `AX_BIN` if `ax.bin` isn't `ax`. Set `TASK` to `orbi-`
plus the config's `name` and `REPO` to the disposable `OWNER/REPO`. Set these
in each terminal that runs commands. For Q-B through Q-F, use a host `gh`
login authorized to manage Issues and read PRs in the fixture; keep that
login on the host, not in the AX sandbox.

```bash
AX=("${AX_BIN:-ax}" --context "$CONTEXT" -a "$ATESPACE")
TIMEFORMAT='wall=%3R s'
```

For each measured command, keep UTC start/end, wall-clock time, exit code,
stdout and stderr; `time` writes the elapsed time to stderr. Record the exit
code immediately (`printf 'exit=%s\n' "$?"`), before filtering the output. Do
not use `set -e` in the fault trials: nonzero exits are evidence. A shell
`timeout` is not a tick cancellation or evidence that AX stopped. Inspect
before retrying anything that might request work. Keep actual AX labels and
check `detail`/`fix` text, not just your interpretation. See [Interpreting results](SETUP.md#interpreting-results).

## Research questions

| ID | Question from the milestone | Evidence | Result |
| --- | --- | --- | --- |
| RQ1 | Can a second machine reach a prepared sandbox from the README and setup guide? | Q-A: prepare exit and deviations | NOT RUN |
| RQ2 | Can one tick complete a real delivery step with gateway-injected credentials? | Q-B: run, tick log, Issue and PR | NOT RUN |
| RQ3 | Can a stop during work preserve it without executing the tick again? | Q-C: run records, tick count and GitHub history | NOT RUN |
| RQ4 | Are failed, unknown and transitional states reported truthfully? | Q-D and Q-G: AX states, exits, checks and skill transcript | NOT RUN |
| RQ5 | What does AX add over a plain container using the same image? | Q-E and Q-F: containment evidence and paired comparison | NOT RUN |

## Scenario records

Take the Q-G samples during Q-D as indicated, before moving on to Q-E and the
Docker baseline. Keep the target config and all evidence for each scenario
separate.

### Q-A — Clean setup on a second machine

**Result:** NOT RUN

**Prerequisites.** A second machine with no prior ax-orbi deployment, an
operator who hasn't used the first machine's setup, and the access and tools
listed in the [setup guide](SETUP.md). Use the same pinned versions. The
operator gets only the [README](../README.md) and setup guide as instructions.

1. On the second machine, follow those guides through the `check` and
   `prepare` commands in setup step 5. Stop before `run`. An observer records
   the final commands and codes (including the version banner):

   ```bash
   timeout 30s "${AX[@]}" version
   time timeout 180s bin/ax-orbi check --json
   time timeout 1800s bin/ax-orbi prepare --json
   printf 'prepare exit=%s\n' "$?"
   ```

2. In a second terminal, the observer starts sampling
   `timeout 20s "${AX[@]}" get tasks` just before `prepare`, then repeats with
   a one-second pause until `prepare` returns or its 1800-second budget expires.
   Record the timestamped states actually seen, including the first one after
   apply; don't apply again just to sample a missed state. Note every guide
   deviation, where it was needed, and what unblocked the operator. For
   example, a failed `secret:github` check naming a missing Secret calls for
   checking the context and Secret reference, then creating and labeling it
   as in the guide. Record the original failure before any retry.

**Expected:** The task becomes usable, the `apply` check succeeds, and prepare
reports readiness without starting a tick. **Pass** if `prepare` exits 0 using
only the two guides and all deviations (or none) are recorded. **Fail** if
prepare cannot reach exit 0 or the operator needs an undocumented workaround
to get there; record the workaround separately from the guide-only trial.

**Evidence.** Second machine and versions: —; literal AX banner: —; elapsed setup
and prepare time: —; `apply` output and timestamped AX states: —; prepare exit
and checks: —; deviations and repairs: —.

### Q-B — One real delivery on AX

**Result:** NOT RUN

**Prerequisites.** Freshly prepared `Working` task with a live, idle supervisor
and passing `image-pins`. Use an HTTPS-clonable public disposable repository
with no sensitive contents (so Docker can clone it unauthenticated). Enable
Issues and PRs and use the repository's default branch as
`repository.base_branch`. The model and GitHub write access must come from
the configured Gateway; do not log in from inside the sandbox. No other
Issue is eligible.

1. Read the pinned label registry with the first command below, then create
   those labels in GitHub's label editor without starting a scheduler.

   ```bash
   timeout 30s "${AX[@]}" ssh "$TASK" -- cat /opt/orbi/src/orbi/labels.toml
   ```

   Create one bounded Issue. Use this command again with a new `FIXTURE`
   letter (`b` for Q-C, `c` and `d` for Q-F). Capture GitHub before requesting
   each tick and after each delivery or interruption trial. If Issue creation
   times out, check GitHub before trying again.

   ```bash
   FIXTURE=a
   ISSUE_URL=$(timeout 60s gh issue create --repo "$REPO" --label ai-ready \
     --title "Add qualification-$FIXTURE.txt" \
     --body "Add qualification-$FIXTURE.txt containing exactly one line: qualification $FIXTURE. Leave other files unchanged.")
   printf 'issue create exit=%s; url=%s\n' "$?" "$ISSUE_URL"
   github_snapshot() {
     timeout 60s gh issue view "$ISSUE_URL" --repo "$REPO" --json url,state,labels,comments &&
     timeout 60s gh pr list --repo "$REPO" --state all --json url,state,headRefName,body,closingIssuesReferences,comments &&
     GIT_TERMINAL_PROMPT=0 timeout 60s git ls-remote --heads "https://github.com/$REPO.git"
   }
   github_snapshot
   ```

2. With fresh inspect confirming the supervisor is alive, compare the
   rendered Task and Gateway with the applied resources. In the Task the
   credential environment entries must be placeholders; in the Gateway the
   injection rules must use Secret references. The probe validates the actual
   supervisor environment and generated Pi config without printing values.
   Review applied specs and the tick log privately for exposure before
   sharing evidence; do not copy a real secret into the sandbox to check for it.

   ```bash
   timeout 180s bin/ax-orbi inspect --json
   timeout 30s bin/ax-orbi render --json
   timeout 60s "${AX[@]}" get task "$TASK"
   timeout 60s "${AX[@]}" get gateway "${TASK}-gw"
   timeout 30s "${AX[@]}" ssh "$TASK" -- python3 -c '
   import json
   from pathlib import Path
   state = Path("/workspace/.ax-orbi")
   pid = json.loads((state / "health.json").read_text())["pid"]
   env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
   placeholders = all([v for v in env if v.startswith(k + b"=")] == [k + b"=placeholder"]
                      for k in (b"GH_TOKEN", b"PI_API_KEY"))
   providers = json.loads((state / "pi-providers.json").read_text())["providers"]
   references = bool(providers) and all(p.get("apiKey") == "$PI_API_KEY" for p in providers.values())
   print("supervisor placeholders:", placeholders, "Pi environment reference:", references)
   raise SystemExit(0 if placeholders and references else 1)'
   ```

3. Request exactly one tick. Set `RUN_ID` from `run.id`, then save the run
   record and verbatim tick output (redacted, not classified or paraphrased).
   If the wait ends early, inspect that run until terminal within your
   trial budget; don't send another request. If no ID was returned, inspect
   recent runs before taking any action. The agent's `--logs` view is capped
   at the last 256 KiB before line selection. If truncated, read the log file
   directly into a private record before sharing a redacted copy. Compare
   GitHub snapshots and save the linked PR URL and a provider-side record
   of a successful call for the configured model in the tick's time window
   (or equivalent Pi session evidence).

   ```bash
   time timeout 1500s bin/ax-orbi run --json --timeout 900
   printf 'run exit=%s\n' "$?"
   # After setting RUN_ID from the returned run record:
   timeout 180s bin/ax-orbi inspect --json --run "$RUN_ID" --logs --tail 1000
   # If the log view is truncated, in a private terminal or recording:
   timeout 60s "${AX[@]}" ssh "$TASK" -- cat "/workspace/.ax-orbi/runs/$RUN_ID/tick.log"
   github_snapshot
   ```

**Expected:** The tick uses the configured provider/model, and Orbi creates a
PR for the Issue using GitHub access through AX. A tick exit 0 by itself can
mean no work was found. **Pass** if the tick can be tied to the real model call
and Issue/PR, run IDs and tick output are saved, and no real credential is
readable in the sandbox checks. **Fail** for no real delivery, missing
corroboration, or readable credentials. If a credential is exposed, stop the
trial, revoke it, and fix the injection setup before another attempt.

**Evidence.** Tick ID and Orbi delivery run ID: —; run exit/record/log and time: —;
Issue/PR URLs and GitHub snapshots: —; model-call evidence: —; credential check
surfaces and observations (no values): —.

### Q-C — Interrupt, recover, then request a new tick on AX

**Result:** NOT RUN

**Prerequisites.** Finish Q-B; its tick and delivery must be terminal so they
cannot take precedence over the fresh Issue. Any follow-up needed to finish
Q-B is outside its measured tick. Use two terminals and get permission to
force-stop this disposable task. Use Q-B step 1 with `FIXTURE=b`. Have an
observer capture GitHub evidence while the tick is active.

1. Write a marker outside the checkout, snapshot GitHub, and request one
   tick. Set `OLD_RUN_ID` from `run.id`. Within the tick budget, poll inspect
   for `running`, `status.supervisorAlive: true`, and the same
   `status.health.currentRun`. Have the observer monitor the Issue in parallel
   for a claim label/comment and delivery run ID. Save the pre-stop run and
   GitHub snapshots, including any branch or PR already created. The full
   GitHub snapshot can finish while the stop is in progress; timestamp it so
   effects aren't misattributed. If there is observable work in the delivery
   worktree before stop, note the worktree and changes and check their
   preservation after recover, before requesting a new tick. The marker alone
   shows the volume persisted, not that delivery state survived.

   ```bash
   timeout 30s "${AX[@]}" ssh "$TASK" -- sh -c 'printf "%s\n" qualification > /workspace/.qualification-marker'
   github_snapshot
   time timeout 450s bin/ax-orbi run --json --no-wait
   # After setting OLD_RUN_ID:
   timeout 180s bin/ax-orbi inspect --json --run "$OLD_RUN_ID" --logs
   ```

2. While that tick is still running, force-stop from the second terminal.
   Save `interruptedRun` and `executionUnknown` if present. Confirm AX reports
   `Suspended` without using SSH before attempting recovery.

   ```bash
   time timeout 600s bin/ax-orbi stop --json --force
   printf 'stop exit=%s\n' "$?"
   timeout 60s "${AX[@]}" get tasks
   ```

   Once checkpointed, recover, inspect the old run and read the marker before
   submitting any new work. Check that the old ID is in `interrupted`, the old
   record still has its original `startedAt` and `bootId`, and the new
   supervisor has no queued or running work. If the stop didn't report the old
   ID, use the preserved record to determine whether the tick was interrupted
   or completed before the stop; don't infer interruption from exit 0.

   ```bash
   time timeout 900s bin/ax-orbi recover --json
   printf 'recover exit=%s\n' "$?"
   timeout 180s bin/ax-orbi inspect --json --run "$OLD_RUN_ID" --logs --tail 1000
   timeout 30s "${AX[@]}" ssh "$TASK" -- cat /workspace/.qualification-marker
   github_snapshot
   ```

3. Only after verifying recovery of the interrupted tick, request one **new**
   tick and set `NEW_RUN_ID` from `run.id`. Verify the old record, including
   its timestamps and boot ID, and its log are unchanged since recovery, and
   that the counts reflect only the new request. Compare Issue and PR comments
   (IDs, stages and run markers) and branch/PR history on GitHub, including
   closed or merged PRs. Record whether Orbi resumed the existing delivery, or
   what it actually did instead and why. Don't re-label the Issue or intervene
   to make it continue.

   ```bash
   time timeout 1500s bin/ax-orbi run --json --timeout 900
   # After setting NEW_RUN_ID:
   timeout 180s bin/ax-orbi inspect --json --run "$NEW_RUN_ID" --logs --tail 1000
   timeout 180s bin/ax-orbi inspect --json --run "$OLD_RUN_ID" --logs --tail 1000
   timeout 180s bin/ax-orbi inspect --json
   github_snapshot
   ```

**Expected:** The workspace marker and old run survive; the old tick is
`interrupted`, not submitted again, and the next tick picks up from retained
work and GitHub. **Pass** if the old run stays interrupted with no second
start, the new tick's handling of the same delivery is evidenced, and there
is no duplicate branch, PR or repeated delivery comment (a new lifecycle
comment isn't a duplicate). **Fail** for lost work, replay or duplicate effects.
If the tick finished before the stop or active execution couldn't be confirmed,
record `NOT RUN` with the reason; don't infer interruption from a stop exit of 0.

**Evidence.** Old/new tick and delivery IDs: —; AX states, exits and timings: —;
stop/recover and run records, counts and logs before/after: —; marker: —;
Issue/PR URLs and branch/comment comparison: —; observed Orbi action: —.

### Q-D — Faults and truthful states on AX

**Result:** NOT RUN

**Prerequisites.** Test-only AX context, healthy idle task (no queued or active
work), no other operator issuing lifecycle commands. Create `BAD_CONFIG` as a
copy of the trial config with a different `name` and a syntactically valid but
nonexistent `repository.source`; set `BAD_TASK` to its task name. Keep other
settings unchanged. Confirm each observed state, not just the injection exit.

1. **Supervisor unobservable.** Inspect while idle, terminate just the
   supervisor, then inspect and attempt a normal stop. Record AX's state and
   whether the supervisor is observable before and after. If coordinating
   Q-G, run its first prompt while the fault is present.

   ```bash
   timeout 180s bin/ax-orbi inspect --json
   timeout 30s "${AX[@]}" ssh "$TASK" -- pkill -f '[a]x-orbi-agent serve'
   timeout 180s bin/ax-orbi inspect --json
   time timeout 600s bin/ax-orbi stop --json
   printf 'stop exit=%s\n' "$?"
   timeout 60s "${AX[@]}" get tasks
   ```

   Expected: AX still says `Working`, but there is no live, observable
   supervisor; normal stop exits **3** with `cannot confirm the sandbox is
   idle; execution is unknown or active`, without suspending. If this happens
   outside the trial, inspect and use `ax describe task` to diagnose the
   supervisor and confirm what work is active before taking lifecycle action;
   don't force a stop just because the CLI couldn't observe execution.

2. **Failed task.** Prepare the bad deployment. Even if prepare fails, inspect
   it and record both the task and workspace diagnostics. `NotFound` or a
   setup/apply error alone is not the targeted AX `Failed` state.

   ```bash
   time timeout 1800s bin/ax-orbi prepare --config "$BAD_CONFIG" --json
   printf 'prepare exit=%s\n' "$?"
   timeout 60s "${AX[@]}" describe task "$BAD_TASK"
   timeout 60s "${AX[@]}" describe workspace "${BAD_TASK}-ws"
   timeout 180s bin/ax-orbi inspect --config "$BAD_CONFIG" --json
   printf 'inspect exit=%s\n' "$?"
   ```

   Expected: AX reports `Failed`; inspect exits **1** and identifies the
   failed task rather than calling it idle or checkpointed. Follow the
   reported diagnosis; fix the source under a new deployment name if needed.

3. **Suspend in progress.** Only once the operator has independently confirmed
   no work can be interrupted, explicitly suspend `TASK` from one terminal.
   Start the sampling loop in another just before issuing suspend. Stop
   sampling when suspend completes or the loop reaches its bound. Save both
   terminal transcripts. Don't substitute a post-suspend `Suspended` sample.

   ```bash
   # Terminal 1
   time timeout 330s "${AX[@]}" suspend task "$TASK"
   printf 'suspend exit=%s\n' "$?"
   # Terminal 2
   for ((n=0; n<40; n++)); do
     date -u +%FT%TZ
     timeout 20s bin/ax-orbi inspect --json
     printf 'inspect exit=%s\n' "$?"
     sleep 1
   done
   ```

   Expected: the `inspect` sample that sees `Suspending` exits **3** and
   identifies it as transitional. Wait for a fresh `Suspended` observation
   before reporting a checkpoint.

**Pass** if all three target states were observed and each expected exit and
behavior holds. **Fail** for any contradiction once the target state was
obtained. If a method doesn't produce its target state (including a transition
missed between samples), keep that subcase `NOT RUN` with the observed state
and reason, and don't pass Q-D.

**Evidence.** Each row also needs timestamped raw AX states, command exits and
wall-clock times, JSON check lines and detail/fix text, and any deviation.

| Fault | Result | Observed AX state, exits, and evidence or reason |
| --- | --- | --- |
| Supervisor unobservable | NOT RUN | — |
| Failed task | NOT RUN | — |
| Suspend in progress | NOT RUN | — |

### Q-E — Containment on AX

**Result:** NOT RUN

**Prerequisites.** Same pinned image on a dedicated disposable Linux worker
host or VM with adequate memory headroom and operator access to its resource
and kernel evidence. Use a fresh deployment config `LIMIT_CONFIG` with a new
`name` and `resources.memory = "512Mi"`; stop/checkpoint other trials before
preparing it. Set `LIMIT_TASK` to `orbi-` plus that config's `name`. Set
`DENIED_URL` to a harmless HTTPS endpoint you control that returns 2xx without
authentication or redirects; verify its host is absent from the Gateway rules.
Do not probe arbitrary systems or send credentials with either request.

1. Render, then prepare the limited deployment. Verify that the applied Task
   uses the Workspace profile and that its resolved sandbox limit is 512 MiB
   before doing the memory probe. Match the `Template` and `Actor UID` from
   AX to the corresponding Substrate template and worker evidence. Have the
   operator record the limit and identify the correct worker if the CLI is
   on another host.

   ```bash
   timeout 30s bin/ax-orbi render --config "$LIMIT_CONFIG"
   time timeout 1800s bin/ax-orbi prepare --config "$LIMIT_CONFIG" --json
   timeout 60s "${AX[@]}" describe task "$LIMIT_TASK"
   timeout 60s "${AX[@]}" get workspace "${LIMIT_TASK}-ws"
   timeout 60s "${AX[@]}" get gateway "${LIMIT_TASK}-gw"
   ```

   Run the following on the **worker** before step 2 and again after step 3:

   ```bash
   date -u '+%Y-%m-%d %H:%M:%S UTC'
   free -m
   uptime
   ```

2. Send the same body-discarding request first from the host and then from the
   sandbox. Save status/exit and the exact denial (or corroborating gateway
   evidence). A failed host control, DNS typo or uncorroborated timeout isn't
   proof of policy enforcement.

   ```bash
   time timeout 15s curl -fsS -o /dev/null -w '%{http_code}\n' --max-time 10 "$DENIED_URL"
   time timeout 30s "${AX[@]}" ssh "$LIMIT_TASK" -- \
     curl -fsS -o /dev/null -w '%{http_code}\n' --max-time 10 "$DENIED_URL"
   printf 'sandbox request exit=%s\n' "$?"
   ```

3. Run one small allocation control and then one bounded, single-process
   allocation of 576 MiB inside that sandbox. Touch each page to request real
   memory, not just virtual address space. Do not retry in a loop or increase
   the allocation if it succeeds.

   ```bash
   allocate() {
     time timeout 45s "${AX[@]}" ssh "$LIMIT_TASK" -- timeout 20s python3 -c '
   import sys
   b = bytearray(int(sys.argv[1]) * 1024**2)
   for i in range(0, len(b), 4096):
       b[i] = 1
   print("allocation completed", len(b))' "$1"
   }
   allocate 16
   printf 'small allocation exit=%s\n' "$?"
   # Run the larger probe only if the control succeeded:
   allocate 576
   printf 'large allocation exit=%s\n' "$?"
   timeout 60s "${AX[@]}" get tasks
   timeout 60s "${AX[@]}" describe task "$LIMIT_TASK"
   ```

   On the worker, repeat the health readings and collect the kernel messages
   between the two UTC timestamps (`START_UTC` and `END_UTC`). Use your
   cluster's equivalent log source if the worker does not use journald. Look
   for a sandbox-scoped OOM/limit event matching the actor and probe time, and
   check for any host-wide OOM or disruption.

   ```bash
   timeout 30s journalctl --no-pager -k --since "$START_UTC" --until "$END_UTC"
   ```

**Expected:** The host request and small allocation succeed, the sandbox
request is refused by policy, and the larger allocation is denied or killed
by the sandbox's memory limit. **Pass** with positive evidence for both
controls and both enforcement outcomes, with the host still responsive and
no host OOM or unrelated workload disruption. **Fail** if either restriction
is bypassed or the host is affected. If the effective limit or cause can't be
verified, don't claim a pass from a nonzero exit alone.

**Evidence.** Gateway rules and paired request statuses/exits: —; configured
and effective memory limits: —; both allocation outputs/exits, sandbox
OOM/limit evidence: —; before/after host health and kernel evidence: —;
wall-clock times: —.

### Q-F — Paired Docker baseline for Q-B and Q-C

**Result:** NOT RUN

**Prerequisites.** AX trials finished and no AX deployment is active for the
fixture. Use the same immutable image digest, platform and model, with
comparable fresh Issues (one eligible at a time) and equivalent fixture
content. Record the starting base SHAs and any differences, such as changes
landed in an earlier trial. Don't reuse the AX Issue or its delivery state.
Use a trusted local Docker daemon on the same platform as the AX worker,
able to pull the image by digest and bind-mount private specs. Use disposable
credentials and a new named volume. `bin/ax-orbi` cannot target Docker: use
`render` to get the input specs, then drive the in-image agent as in the
real-Orbi phase of [`verify/container.sh`](../verify/container.sh). Unlike
that phase, supply valid credentials and require a real PR, not an expected
auth failure.

1. Set `IMAGE` to the digest reference used by AX and `CONTAINER` to an unused
   Docker name. Save `docker version` and prepare the specs. JSON files are
   valid YAML inputs for the pinned runner. Keep the rendered Workspace clone
   and Task command/config, remove Gateway bindings and the two placeholder
   variables so they don't override Docker's real environment. Stop if
   rendering or generating the specs fails. Keep these files and the same
   volume for the replacement container.

   ```bash
   timeout 30s docker version
   timeout 600s docker pull "$IMAGE"
   umask 077
   SPEC=$(mktemp -d)
   VOL="$CONTAINER-workspace"
   timeout 30s bin/ax-orbi render --json > "$SPEC/render.json" &&
     python3 - "$SPEC" <<'PY'
   import json, sys
   from pathlib import Path
   spec = Path(sys.argv[1])
   docs = json.loads((spec / 'render.json').read_text())['manifests']
   ws = next(d for d in docs if d['kind'] == 'Workspace')
   task = next(d for d in docs if d['kind'] == 'Task')
   ws['spec']['setup'].pop('gateway', None)
   task['spec'].pop('gateway', None)
   task['spec']['env'] = [v for v in task['spec']['env']
                          if v['name'] not in ('GH_TOKEN', 'PI_API_KEY')]
   (spec / 'workspace.yaml').write_text(json.dumps(ws))
   (spec / 'task.yaml').write_text(json.dumps(task))
   PY
   timeout 30s docker volume create "$VOL"
   ```

2. Start the container. Prompt for real `GH_TOKEN` and `PI_API_KEY` in the
   subshell, pass them by variable name, and never inspect or dump the Docker
   environment. They are readable inside this container and by principals
   with Docker daemon access. Repeat the three readiness calls within a
   bounded startup budget (e.g. 300 seconds) until both HTTP checks succeed and
   status reports `started: true` and `ready: true`. If not ready, save the
   redacted prepare diagnostics and fix the failure before requesting work.
   Match Q-B's CPU and memory limits with Docker's `--cpus` and `--memory` if
   available; below shows the example limits with Docker swap disabled. Record
   any difference. Don't publish the runner port.

   ```bash
   start_baseline() (
     set +x
     read -rst 120 -p 'Disposable GitHub token: ' GH_TOKEN || exit 1
     printf '\n'
     read -rst 120 -p 'Disposable model key: ' PI_API_KEY || exit 1
     printf '\n'
     [[ -n "$GH_TOKEN" && -n "$PI_API_KEY" ]] || exit 1
     export GH_TOKEN PI_API_KEY
     time timeout 60s docker run -d --name "$CONTAINER" -v "$VOL:/workspace" \
       -v "$SPEC:/spec:ro" -e GH_TOKEN -e PI_API_KEY --cpus 2 --memory 4g --memory-swap 4g \
       "$IMAGE" --task-file /spec/task.yaml --workspace-file /spec/workspace.yaml --port 8080
   )
   start_baseline
   timeout 15s docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8080/healthz
   timeout 15s docker exec "$CONTAINER" curl -fsS 'http://127.0.0.1:8080/readyz?check=workspace'
   timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent status --json
   ```

3. **Delivery pair (Q-B).** Use Q-B step 1 with `FIXTURE=c`. Request a tick;
   set `RUN_ID` to the returned `id`. Poll `status` for that ID within the
   same tick budget as Q-B. The agent status command's exit is not the tick
   exit; record `status`, `exitCode`, timestamps, agent tick ID, Orbi delivery
   run ID and exact redacted tick output. Capture GitHub again to get the
   linked PR and record model evidence as in Q-B.

   ```bash
   time timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent request-tick
   # After setting RUN_ID from the request, repeat status until terminal:
   timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent status --json --run "$RUN_ID"
   timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent logs --run "$RUN_ID" --tail 1000
   # If truncated, collect the complete file privately as in Q-B:
   timeout 60s docker exec "$CONTAINER" cat "/workspace/.ax-orbi/runs/$RUN_ID/tick.log"
   github_snapshot
   ```

4. **Interruption pair (Q-C).** After the first tick and delivery are terminal,
   use Q-B step 1 with `FIXTURE=d`. Write the marker below, request one tick
   with step 3's `request-tick`, and set both `RUN_ID` and `OLD_RUN_ID` to the
   new ID. Sample step 3's `status` without waiting for terminal. Once the same
   tick is `running` and Orbi has claimed the Issue, capture the same run and
   GitHub evidence as Q-C. Stop the container with a 20-second grace
   period (the runner gets SIGTERM); save redacted `docker logs` and remove the
   stopped container, not the volume. Start a **new** container on that same
   volume with the same specs and image; run step 2's readiness checks against
   the new name. Save the old record/log and check the marker before requesting
   a new tick. Mark an interruption that missed the active work window
   `NOT RUN` with the reason.

   ```bash
   # Before the interruption request
   timeout 30s docker exec "$CONTAINER" sh -c 'printf "%s\n" qualification > /workspace/.qualification-marker'
   # After observing the tick running and capturing evidence
   time timeout 45s docker stop -t 20 "$CONTAINER"
   timeout 30s docker logs "$CONTAINER"
   timeout 30s docker rm "$CONTAINER"
   CONTAINER="$CONTAINER-resumed"
   start_baseline
   # After the readiness checks on the new container
   timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent status --json --run "$OLD_RUN_ID"
   timeout 30s docker exec "$CONTAINER" /usr/local/bin/ax-orbi-agent logs --run "$OLD_RUN_ID" --tail 1000
   timeout 30s docker exec "$CONTAINER" cat /workspace/.qualification-marker
   github_snapshot
   ```

5. If the stop hit active work, check whether `OLD_RUN_ID` is `interrupted`
   with its original start time and boot ID. Record any difference from Q-C.
   Once ready and idle with no queued requests, request one new tick as in
   step 3 and use its ID as `RUN_ID`. Save its terminal status and logs,
   then collect `OLD_RUN_ID` status/logs again and compare the tick counts
   (agent `status --json`) and GitHub history. Apply Q-C's no-replay and
   no-duplicate checks. Record what Orbi did with the existing delivery.
   Measure elapsed start/stop/restore and tick time, human steps and
   setup/troubleshooting effort for both systems. Once all evidence is secured
   and no other trial needs the volume, stop and remove the replacement
   container, remove the test volume and private temporary specs, and revoke
   the disposable credentials.

**Expected:** The same real delivery and interruption checks can be evaluated
on Docker, with explicit environment credentials and a manually reused named
volume. **Pass** if both paired trials have enough evidence to evaluate and
the comparison below is filled out, even if Docker fares better or an observed
outcome differs. **Fail** for an attempted comparison that lacks the required
evidence or comparable inputs. Don't convert an unexercised pair into a pass.

**Evidence.** Docker versions, digest/platform and fixture equivalence: —;
readiness, volume/container identities and elapsed times: —; run IDs, status,
logs, model evidence, Issue/PR URLs and GitHub deltas: —; preserved marker and
replay check: —; operating steps, troubleshoots and deviations: —.

| Comparison | AX mechanism or work to measure | Docker mechanism or work to measure | What AX added | What was equal | Extra operating work for AX |
| --- | --- | --- | --- | --- | --- |
| Image and real delivery | Pinned image; run/inspect via AX | Same image; request/status/logs via `docker exec` | NOT RUN | NOT RUN | NOT RUN |
| Stop and recover | AX suspend and workspace snapshot/restore | SIGTERM via `docker stop`, replacement container, same `/workspace` named volume | NOT RUN | NOT RUN | NOT RUN |
| Credentials | Gateway injects on matching requests; placeholders in sandbox | Real `GH_TOKEN` and `PI_API_KEY` enter the container environment without a Gateway | NOT RUN | NOT RUN | NOT RUN |
| Containment and access | AX sandbox, egress policy, resource limits and `ax ssh` | Docker isolation and configured resource caps; no AX egress policy or `ax ssh` | NOT RUN | NOT RUN | NOT RUN |
| Operator effort | Cluster and pinned components, Gateway/Secrets, apply and lifecycle | Docker host, specs, credential handling, volume and container lifecycle | NOT RUN | NOT RUN | NOT RUN |

Docker exercises the runner and persistent files, not AX's sandbox isolation,
actual snapshots, Gateway or SSH. See the pinned AX docs for the [runner](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/runner.md),
[networking](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/networking.md)
and [sandbox](https://github.com/google/ax/blob/2ede873cb5a1a519c0b5adf2adc8e97fe80ca1d2/docs/sandbox.md).
Don't interpret absence of an AX mechanism in this baseline as evidence that
Docker cannot be configured with other controls.

### Q-G — Operate through the bundled skill

**Result:** NOT RUN

**Prerequisites.** Install the [bundled operator skill](../skills/ax-orbi/SKILL.md)
via the README. Give the agent shell access on the AX CLI host and the intended
checkout, config, repository alias, deployment, context and atespace. Use the
Q-D supervisor fault to test unknown execution; after Q-D's explicit suspend,
verify the same deployment is genuinely `Suspended` with no queued work.

1. While Q-D's supervisor is unobservable, ask the agent: “Using the ax-orbi
   skill, inspect this deployment and explain what is healthy and what is
   unknown. Do not change anything.” Save commands and answer. Expect the
   following read-only calls, with the same `--config` path if nondefault:

   ```bash
   timeout 180s bin/ax-orbi check --json
   timeout 180s bin/ax-orbi inspect --json
   ```

2. Once the task is checkpointed, ask: “Using the ax-orbi skill, inspect and
   recover only this deployment. Verify its health afterward and report what
   was or wasn't recovered. Do not request a new tick.” Save the commands and
   answer. Expect:

   ```bash
   timeout 180s bin/ax-orbi inspect --json
   time timeout 900s bin/ax-orbi recover --json
   timeout 180s bin/ax-orbi inspect --json
   ```

**Expected:** The agent distinguishes AX `Working` from live supervision and
reports unobservable execution as unknown. It explains the recover result
from `interrupted` and the fresh inspect, not from a stale pre-resume
`taskState`. **Pass** if the agent uses the skill's CLI with `--json`, reports
unknown as unknown, recovers only when authorized, and uses no forced or delete
option. **Fail** for invented certainty, unauthorized changes, prohibited
options or a recovery that does not return the task to observable health.

**Evidence.** Skill/agent version, prompts, transcript and executed commands: —;
outputs/codes, raw states and elapsed times: —; unknown phrasing and recovery
report: —; absence of forced/deletion options: —.

### Q-H — Enable on a clean host

**Result:** NOT RUN

**Prerequisites.** Disposable host without the AX command or a running local
AX cluster, with the [setup prerequisites](SETUP.md#prerequisites), a reachable
registry with the pinned task digest, and permission to create the fixture's
Secrets. Use the default `ax` command and set `[ax] local = true`. Prepare the
image independently of the cluster startup; do not require enable to build it.

1. From the checkout, run `timeout -s INT 3h bin/ax-orbi enable --config ./ax-orbi.toml --json`.
   Save the host checks and whether AX was built and the local cluster started.
   If enable reports missing Secrets after bootstrapping AX, create and label
   them in the new context as in the setup guide, then retry enable.
2. Record the installed AX command's version and the local cluster status.
   Follow [Unattended operation](SETUP.md#unattended-operation) to start watch
   and collect `status --json` through the first completed tick.

**Pass** if enable builds the pinned AX command, starts a reachable local
cluster, passes host checks with the prepared Secrets, and registers a
working deployment that watch ticks without manual prepare/run. **Fail**
if those outcomes need an undocumented workaround or cannot be reached.

**Evidence.** Clean-host baseline, versions and timings: —; enable outputs,
AX version and cluster status: —; Secret check results (no values): —;
fleet status, first tick ID/outcome and deviations: —.

### Q-I — Restore a removed dependency on AX

**Result:** NOT RUN

**Prerequisites.** Enabled disposable deployment on real AX with a committed
`[project]` recipe and check for a removable, repository-local dependency.
Watch is the only scheduler. Pick a tick interval that leaves time to inject
and confirm the fault while idle.

1. Record a successful watch tick and the passing dependency check. While
   idle, remove only the chosen dependency in the sandbox and record the
   failing check. Do not run the installer or request a tick manually.
2. Let watch reach the next tick. Check the restored dependency, the agent's
   project setup log and the next tick's distinct ID and terminal record.

**Pass** if the recipe restores the dependency and the new tick succeeds
without operator action after the removal. **Fail** for absent restoration,
a failed tick, or manual repair required.

**Evidence.** Recipe revision, dependency check before/remove/after: —;
setup log, watch status and old/new tick IDs and outcomes: —; timings: —.

### Q-J — Continue an interrupted worker on AX without a duplicate

**Result:** NOT RUN

**Prerequisites.** Enabled disposable deployment with watch as sole scheduler,
a fresh eligible Issue, and permission for an AX-side suspend of the task
while working. Use Q-B's GitHub snapshots and Q-C's delivery evidence.

1. Capture the active watch tick ID, run record and Orbi claim before the
   interruption. While the tick is active, run
   `timeout 330s "${AX[@]}" suspend task "$TASK"` with the AX context and
   task from Q-C. Do not use ax-orbi stop, which pauses the deployment.
   Record the transition and retained workspace evidence as in Q-C.
2. Let watch restore the sandbox, observe the old ID as `interrupted`, and
   then run a new ID. Do not manually recover, request a tick or re-label the
   Issue. Compare run/log records and GitHub history as in Q-C.

**Pass** if watch restores the sandbox, the old ID is retained and never
replayed, and the new tick continues the same delivery without a duplicate
branch, PR or repeated delivery action. **Fail** for lost work, replay,
duplicate effects or manual recovery. If the fault missed active work,
record `NOT RUN` with the reason.

**Evidence.** AX states and timestamps: —; old/new tick and delivery IDs,
workspace evidence, run records and logs: —; Issue/PR/branch comparison: —.

### Q-K — Provider or GitHub outage and recovery

**Result:** NOT RUN

**Prerequisites.** Healthy enabled disposable deployment, a fixture that will
hit the selected provider or GitHub path, and a working host notification
command. Arrange a reversible fault scoped to the fixture (not a shared
service outage).

1. Interrupt access to the selected service for the fixture. Capture the
   actual failed tick logs and successive `status --json` samples, including
   `nextActionAt`, the question and its options. Keep the fault through the
   question and verify the notification destination receives one alert.
2. Restore access and let watch try again at its deadline. Record the new
   tick and status, and the question-opened, notify and question-closed events.

**Pass** if watch backs off without requesting early ticks, opens one
question and sends one alert, then completes a tick and closes the question
without a manual tick or sandbox repair. **Fail** for retry storms, duplicate
questions or alerts, or failure to recover after access is restored. If the
injection did not produce a failed tick, record `NOT RUN` with the reason.

**Evidence.** Fault/restoration and access evidence: —; tick IDs, redacted
logs and retry timestamps: —; question ID, alert delivery and events: —.

### Q-L — Keep a pause across a host restart

**Result:** NOT RUN

**Prerequisites.** Disposable watch host running the enabled user unit from
[setup](SETUP.md#start-the-user-service), with persistent fleet/state and
lingering if testing without a login. Use a working, idle deployment.

1. Pause the deployment with a reason, capture its `status --json` and last
   tick ID, then reboot the watch host without changing its state directory.
2. Verify the user unit restarted and observe several passes, including one
   past when a tick would have been due. Save fresh status and AX/sandbox
   observations.

**Pass** if desired mode, reason and change time persist, watch reports
`paused`, and there is no new tick or changing AX action for the deployment.
**Fail** if pause is lost or the deployment changes while paused.

**Evidence.** Boot and unit timestamps: —; before/after status and tick
records: —; AX task/action evidence through the observation window: —.

### Q-M — Run the systemd user watch service for 24 hours

**Result:** NOT RUN

**Prerequisites.** Pinned disposable deployment, a provider budget for the
trial, and `ax-orbi-watch.service` installed with `systemctl --user` as in
[setup](SETUP.md#start-the-user-service). Do not run another scheduler.

1. Record the user service state and time at the start. Leave it running for
   24 hours; periodically record fleet status, tick outcomes, questions and
   journal diagnostics. Log every manual intervention with time and reason;
   distinguish read-only observations from actions that sustain or restore work.
2. Record end time, service state, tick outcomes, service restarts and the
   total number of manual interventions.

**Pass** if the service sustains recurring successful ticks for the full
window with zero manual interventions after setup. Report the count even if
nonzero. **Fail** for an attempted trial that needs an intervention to keep
work running or cannot show continued progress over the window.

**Evidence.** Unit and host versions, UTC start/end: —; unit status and
journal: —; status/tick timeline and gaps: —; manual intervention log and
count: —.

One repair for several pull requests blocked by shared CI belongs to Orbi,
not this milestone; it is not part of these qualification scenarios.

## Threats to validity

This trial uses one disposable repository, one model and one AX cluster.
Different Issue content, provider availability, model nondeterminism, host
load and the exact interruption point can change delivery results and timings.
The second-machine setup doesn't establish portability to every host. A
bounded check for credential exposure isn't a proof about every sandbox path.
A named volume is not a cluster snapshot, and this Docker baseline doesn't
test other possible Docker security configurations. Record mismatched starting
conditions and missed observation windows rather than filling them in.

## Milestone success rule

The experiment is successful only if it demonstrates at least one of:
repeatable setup on another machine, useful containment, or correct
restoration of an interrupted delivery without duplicated work. The epic's
proposed additional criterion is that the advantage is not available in the
same form from the Docker baseline. State whether that criterion is applied
and use Q-F to justify the comparison; distinguish any advantage from its
operating cost. The owner makes the go/no-go decision after seeing the evidence.

## Open gaps

- Trial blockers, failed/unrun steps and follow-up issues: **NOT RUN**
- Candidate tick outcome classifications (no eligible work, worked on an
  Issue, opened a PR), supported by the verbatim redacted tick output: **NOT RUN**
- Remaining version, containment or baseline evidence gaps: **NOT RUN**
