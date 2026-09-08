# Plan07 Release Candidate Binding Implementation Plan

> **Execution rule:** Complete each task with RED, minimal GREEN, and the
> listed focused verification before moving to the next task. Do not push a Git
> tag or run a real registry publication until all local contracts are GREEN.

**Goal:** Bind one immutable yFeiSTAI source commit, one release tag, three GHCR
workload image digests, all five base-image digests (including PocketBase), one
independent observer signer digest, `deploy/image-lock.json`, the exact base
Compose file, and all three rendered production Compose files into one
fail-closed Plan07 release candidate.

**Architecture:** Extend the image-lock writer with schema-version-3 candidate
metadata, require that metadata in production wrapper/preflight/verifier paths,
and harden the private-image workflow so an exact candidate tag is validated
before registry login. Keep legacy locks readable only through an explicit
historical-inspection mode.

**Tech stack:** Python 3, pytest, PyYAML, GitHub Actions YAML, Docker Compose
configuration text.

---

## Execution Status — 2026-09-06

**Current goal alignment after local-main integration:** all local application code
was merged into `main@b543e872f71126ed11690925f6466d3d6e56bba6`. The current goal
now includes a new-main publication, shared-host ingress adaptation, Plan07 live
operational/acceptance evidence, and the independent branded RAGFlow deployment
and lifecycle validation. See the authoritative `0.000` goal update in
`2026-07-30-yfeistai-openmaic-first-release-reassessment.md` and the G1–G5/R1
mapping in Plan07. This entry is a documentation update, not an executed release.

The successful historical candidate `93844791` / run `34034924980` / artifact
`9990178253` remains valid for its exact source. It is not the final candidate
for the newly merged RAGFlow/branding work. Complete any necessary ingress and
publication wiring changes before freezing the next clean HEAD, then publish a
new immutable tag and independently verify that run's raw artifact and digests.
Do not move old tags, reuse old receipts for new code, or repeat already-completed
branch consolidation as a release prerequisite.

The existing four custom-image contract covers the platform workloads and
independent observer signer, not a RAGFlow engine image. R1 must lock its upstream
source, branding patch, separately produced engine digest and deployment/connection
identity to the same release batch. Design and test any schema/interface changes
before implementing them; this goal update neither changes schema3 nor inserts
unrecognized image-lock fields. R1 evidence is additional to, not a replacement
for, the original twelve live categories. Existing registry authorization stands;
real Provider, TLS and fresh capacity inputs are still required for dependent steps.

The earlier entries below are historical execution evidence; statements about a
then-pending publication or a merge-after-acceptance sequence do not override this
current goal alignment.

**Actual Task1 receipt-step correction:** candidate `38c8e1de` was published under
`yfeistai-first-release-20260906-38c8e1de` in first-attempt run `34032988226`.
All four staging builds, final tag promotions, remote digest comparisons and
promotion receipt verification succeeded. The run nevertheless ended **failure**:
`Record Task1 release evidence` passed its POSIX recovery-policy admission, then
`source-head` failed with `ValueError: candidate root must be an absolute path`.
Both receipt commands still supplied relative `release-candidate`; artifact upload
was skipped. This is an input-wiring defect, not an unsafe-ancestor failure.
Keep that immutable tag and its images; do not reuse it or call this Task1 complete.
The two arguments now use `"$GITHUB_WORKSPACE/release-candidate"`; the strict
candidate admission and recovery implementation remain unchanged. The updated
workflow regression failed first (native1, 1/1), then the complete affected file
passed **173/173**, native0, pytest105.19s / wall109.235s, unchanged source.
The new Linux regression executes the complete YAML Task1 bash step with the real
CLI and Git against an isolated clean fixture commit, and verifies both receipts,
all five unchanged candidate inputs and unchanged Git HEAD/status. The native CI
and downloaded-artifact slice passed **82/82**, 92 deselected, native0,
pytest72.64s / wall75.342s, with all 942 source hashes unchanged and process
residuals empty. Snapshot SHA256:
`78bb68a4fa654e3c0ffe4a651eca04f3810e67a3b767848faa30fc0b4994ee65`.
Its image digest inputs are synthetic fixtures, not publication evidence. Actual
diff review passed; the next immutable run must still complete and yield a
verified raw nine-member artifact before deployment can proceed.

**Real Actions API compatibility correction:** the first frozen candidate
`34007a4826c25771a28c15c0fa24e19fb8ba4be8` was tagged and pushed as
`yfeistai-first-release-20260906-34007a48`. Run `34032340707` reached the
OpenMAIC build after completing the deeptutor staging build, then was explicitly
cancelled after an independent API check exposed a local verifier defect.
Both that run and completed historical run `33325874655` return REST `path` as
`.github/workflows/private-platform-images.yml`, not the ref-qualified workflow
identity. The verifier now checks that exact path; `head_branch`, `head_sha`,
repository, run/attempt, artifact digest and receipt `githubWorkflowRef` bindings
remain unchanged. Correct API metadata and a fabricated suffix produced two
effective REDs before the one-line fix; the complete affected test file then
passed **173/173**, native0, pytest110.82s / wall115.157s, unchanged source.
Independent diff review found no remaining issue. The cancelled run is not a
successful publication; its original tag/logs and any staging image are retained.
No same-tag rerun, tag movement or image cleanup is performed. The correction
must be committed and published as a new immutable candidate.

**Subsequent release-closure approval:** the user approved the versioned Observer
manifest as the authoritative source for all seven deployment inputs, explicit
per-run authorization expiry, and a new run/credential batch on retry (no implicit
renewal). Implementation and affected verification are complete. Earlier descriptions below of this
design as undecided are historical, not a remaining approval dependency.
Standing scope includes a new immutable final-source tag and GHCR publication,
isolated deployment, all twelve live categories, then local-main integration.
Read-only host inventory confirmed SSH/Docker and GitHub package-write availability,
but found 96% disk usage (about 6.7 GiB free), about 2.1 GiB available memory,
the existing yFeiSTAI hostname's TLS mismatch and empty deployed model profiles.
Production domain, Provider credential channel and resource disposition if capacity
is insufficient were requested together; no server mutation or publication occurred.
Evidence: `E:\CodexTemp\yfeistai-release-closure-20260906-readiness.md`.

**Final Observer delta:** manifest schema 2 binds the seven explicit deployment
inputs; launcher and host preflight obtain all fifteen environment values from
that admitted manifest. Admission/startup recheck a 30..7200-second remaining
authorization window. Expired committed batches cannot be renewed or overwritten;
retry creates a new run/batch. Structural recovery remains available for expired
uncommitted batches. No implicit renewal or unrelated proxy behavior was added.
The final eight-file scope returned Windows **291 passed / 3 POSIX-only skipped**
(native 0, pytest 90.34s / wall 94.078s) and Linux **293 passed / 1 Windows-only
skipped** (native 0, pytest 95.70s / wall 99.361s). Both retained unchanged-source
markers; Linux process-group residuals are empty. The final 942-input native
archive SHA256 is `e717582c871f272abd96531784064b086c3c117cbce2159334c386b889412b1f`.
All 68 pending Python files passed Ruff check/format check; two test-only
formatting changes had unchanged ASTs and were included in both final runs.
Independent specification and implementation reviews reported no remaining
concrete blocker in this delta. Evidence:
`E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\observer-manifest-closure-20260906.md`.
Earlier failed attempts are retained and classified there: missing native
dependencies/input files, three cross-platform fixture errors, and one Windows
basetemp quoting/MAX_PATH error are not silently reclassified as behavioral RED.
Local software gates are ready for candidate freeze; publication, isolated
deployment, and all twelve live categories still require their actual evidence.

Latest development integrates the confirmed POSIX recovery protocol into the production
receipt producers and assembler. A fresh per-call attempt retains original file and parent
descriptors across the complete exception/finally/compensation path; admission precedes
output or runner work. Both POSIX cleanup helpers now use isolated recovery. Raw reports
and staging aliases are compensated without archival, and primary failures retain nested
recovery/journal locations. Windows keeps its existing handle-based path. CI supplies the
explicit recovery root and distinct existing worker UID to both Task1 receipt commands.

Frozen native input: 929 files, archive SHA256
`1d74acdfc1f13843cc6f3072d0c7e66296e3cada7c2136e321af21af12fda23f`.
Complete producer/recovery/input scope: **551 passed / 8 Windows-only skipped**,
pytest 838.72s / wall 841.276s. The original cleanup file is **4/4**, including the two
previously failing foreign-replacement cases with real different-UID competitors.
Complete verifier: **210 passed / 4 Windows-only skipped** (503.06s / 505.759s).
Complete backup probe as UID 1000: **66 passed / 5 Windows-only skipped** (6.68s / 9.042s).
Selected CI/downloaded-artifact contracts: **80 passed / 92 deselected** (64.68s / 67.464s).
Every final native gate has exit 0, unchanged inputs, no timeout/collection error and zero
owned process-group residuals. All 66 earlier full-suite failures also passed their exact
recheck. Fixture/environment errors remain separately classified, not promoted to valid RED.
Independent production integration review found no remaining concrete blocker.

Windows final complete twelve-file verification: **997 passed / 11 POSIX-only skipped**,
native/wrapper 0, pytest 1985.29s / wall 2015.531s, unchanged source, no timeout/collection
error and no owned process residual. The original six producer/input files are 479 passed /
1 skipped, verifier 213/1, platform Compose 172/0 and backup probe 63/8, all subsets of the
same 1008 cases rather than additional totals. Windows handle/close/reparse assertions ran.
All 67 pending Python files passed Ruff check, format check and AST syntax checks;
workflow YAML, receipt-step/deployment-template Bash syntax and full git diff-check passed.
Those receipt-integration Task6 affected checks and scoped review are closed. The
subsequent Observer design and implementation are now closed by the newer record
above; publication and deployment/live evidence remain separate pending stages.
Earlier Windows attempts are retained as incomplete: one disabled the existing user-site pptx
dependency; the other was rescheduled because its 900s bound was below the prior six-file
1208.516s runtime. The isolated CLI confirmed user-site disabled -> missing pptx/exit 1,
normal installed dependency path -> exit 0, without package or source changes. Neither
incomplete attempt is counted as whole-scope GREEN/RED. The final retry used new isolated
runtime/temp directories and a 3600s bound; no earlier test material was cleaned.
See the [recovery integration plan](./2026-09-06-plan07-posix-receipt-recovery.md) and
`E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\receipt-production-integration-audit-20260906.md`.
No deployment identity/filesystem validation, Actions run, registry operation, project
commit/push/merge or live receipt occurred. Publisher code/dependencies/children remain
operator-trusted; a UID argument does not create worker isolation. Observer configuration
and expiry/reissue were subsequently approved and implemented above. Task1/5/7 ongoing, Task8 not_ready,
same-candidate live evidence 0/12.

Previous development closed the default venv interpreter compatibility gap. A Python-only
availability validator retains the lexical absolute entrypoint and checks regular/executable
access; generic artifact and pg_restore no-link rules remain unchanged. A real child checks
venv prefix and a venv-only module. Eight cases moved from 5 passed / 3 failed (native 1)
to 8 passed (native 0). Full backup scope first exposed a test open-tracker missing dir_fd
passthrough and missing SQLAlchemy; the test-only forwarding fix and two missing declared
dependencies resolved those, without weakening assertions. Final complete backup file:
Linux **66 passed / 5 Windows-only skipped**, Windows **63 passed / 8 POSIX-only skipped**,
both native/wrapper 0 (pytest/wall 6.93/9.423s and 4.48/6.453s respectively).
The complete Linux release-verifier then passed **210 / 4 Windows-only skipped**, native/
wrapper 0, pytest 478.25s / wall 480.850s, no timeout or collection errors. Both final Linux
files use the unchanged 923-input archive
`12b7ad625a338e6995e03c79280dea8cf3f4f1c752f63ce38b5f0c3494916f68`.
Only private-venv SQLAlchemy 2.0.49 and greenlet 3.4.0 were added using official hash-pinned
wheels; all 36 existing packages and system Python remain unchanged. Installation/check/
local no-DB probe returned 0. The runtime installation remains operator-trusted; the new
precheck is not race-free/authenticated exec. No Docker, SSH, push or cross-task messages.
Evidence: `E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\backup-interpreter-fix-audit-20260906.md`,
`backup-probe-full-native-20260906-02`, `release-verifier-full-native-20260906-02`, and
integration `.pytest_cache/backup-probe-full-windows-20260906-01.*`.
These are local filesystem/child-process and mock DB/object-store tests, not live restore
or release. Source edits remain uncommitted. Task1/5/7 ongoing, Task8 not_ready, live 0/12;
the receipt rollback REDs still open at that earlier interpreter stage are now closed by
the native production integration above; the seven observer inputs remain unresolved.
The checked-in image-lock is still historical schema 1, not a current schema 3 candidate.

Pre-interpreter-fix Linux findings are retained as history below. The original receipt
cleanup file was rerun on current 923 inputs: **2 passed / 2 failed, native/wrapper 1**,
pytest 5.23s / wall 10.169s, no errors/skips; replacement-at-unlink failures remain.
Twelve files have changed since the old 920-input RED, so its full snapshot is historical.
The complete release-verifier file then returned **64 passed / 146 failed / 4 Windows-only
skipped, native/wrapper 1**, pytest 305.38s / wall 314.739s, no timeout. Failure groups:
140 missing-jsonschema fixture imports and 6 default symlink-Python rejections in backup
probe setup; not 146 behavioral REDs and not a passing complete Linux verifier.
Only the private validation venv gained five absent, version/hash-pinned wheels from
official PyPI: jsonschema 4.26.0, jsonschema-specifications 2025.9.1, referencing 0.37.0,
rpds-py 2026.6.3 and attrs 26.1.0. Existing packages are unchanged, nothing removed;
system Python is untouched. Installation, dependency check and schema probe returned 0.
A subsequent three-node run returned **2 passed / 1 failed, native 1** (pytest 9.67s /
wall 12.799s): native sealed-memfd child reads and FIFO rejection passed; the representative
bundle now passes the missing import but reaches the same Python-entry rejection.
The whole verifier has not been rerun after dependency installation. A separate real-child
probe confirmed that simply resolving the venv symlink switches to the base prefix and
loses the venv-only jsonschema package. This is a Task5 default-interpreter compatibility
gap, not only a missing dependency; preserve venv semantics and other no-symlink input
checks when designing its fix. A copy-based venv GREEN would not close this gap.
Actual production-image impact remains conditional and untested.

All **30 distinct platform-skipped IDs** from the five historical Windows full-file runs
now match passing items in four current Linux reports, including isolated passing items
inside failing reports. This closes their platform execution gap only, not the complete
Linux/Python release gate. The three current test runs share archive
`cf92039cb669d4cf71df40772867d8b8b6019f9a40a18832a385b3ad3e77b542`; all 923 hashes match
current source and all owned process-group residuals are zero. Material is retained under
`E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\` in
`receipt-cleanup-current-native-20260906-01`, `release-verifier-full-native-20260906-01`,
and `descriptor-inputs-bundle-native-20260906-01`; dependency/probe files use prefix
`verifier-jsonschema-20260906-01-`. Pytest now follows the user's later direct-run authority,
serial within this task without external-task grants/messages; other host typechecks were
observed, so no whole-host exclusivity/performance-baseline claim is made. No production/test
edit, Docker, SSH, commit/push or cross-task message occurred; only dependency setup used
the official package network. Task1/5/7 ongoing, Task8 not_ready, same-candidate live **0/12**.

Latest runtime-attestation native gate: the complete current test file collected 146 items
and returned **140 passed, 6 Windows-only skipped, no failures/errors, native/wrapper 0**
on local Ubuntu 22.04 / ext4 / Python 3.11.15 as UID 1000 (pytest 74.61s / wall 77.018s,
no timeout). It exercises FIFO rejection, openat/no-follow, renameat2, descriptor identity
and synchronously injected path races; there are no memfd/O_TMPFILE calls in this scope.
Docker/preflight/signer paths use fixtures, so this is not live container/receipt evidence.
All 923 snapshot input hashes match before/after execution and current Windows source;
archive SHA256 is `cf92039cb669d4cf71df40772867d8b8b6019f9a40a18832a385b3ad3e77b542`.
Source/runtime/temp/basetemp are private under `/var/tmp/yfeistai-runtime-attestation-l7ga6_yw`.
Owned process-group/Windows runner residuals are zero; all temporary material is retained.
Evidence: `E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\runtime-attestation-full-native-20260906-01`.
No production edit, Docker/SSH/network, commit/push or external-task notification occurred.
The independent receipt-cleanup two-test RED and documented final stat/rmdir same-UID race
remain unresolved. Seven-input authority and the rollback design still await a decision;
Task1/7 remain ongoing, Task8 not_ready, and same-candidate live evidence remains **0/12**.

Latest native Linux gate: the complete current observer provisioning test file returned
**33 passed, no failures/errors/skips, native/wrapper 0** on local Ubuntu 22.04 / ext4 /
Python 3.11.15 as UID 0 (pytest 8.87s / wall 13.731s). Both root-operator/nonroot-operator
permission items executed with real DAC checks in 18 identity-dropped subprocesses; there
was no system-user modification. This is same-inode secret access, not a Docker mount proof.
The current 923-file archive is verified before extraction and after execution; all 923
hashes also match the current Windows source. The initial stale 920-count preparation
assertion stopped before pytest; three previously added receipt/preflight tests account
for the inventory growth, with no input removed. Archive SHA256:
`cf92039cb669d4cf71df40772867d8b8b6019f9a40a18832a385b3ad3e77b542`.
Private source/runtime/temp/basetemp paths live under
`/var/tmp/yfeistai-observer-provision-2txu3vc5`; pytest plugin autoload is disabled with
only the required asyncio plugin explicitly enabled. Owned process-group residual is zero;
all temporary and audit material remains. Evidence directory:
`E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\observer-provision-full-native-20260906-01`.
No production edit, Docker/SSH/network/user-management operation, commit/push or external
task notification occurred. Seven-input authority and the independent Linux receipt rollback
RED remain unresolved; same-candidate Secret/DB/preflight runtime receipts are still missing.

Latest remaining Web gates: `playwright-site-visual-20260906-01` completed the original
full-site file with **48/48 passed**, native 0, no skips/unexpected/flaky results or JSON errors,
Playwright 404.358s / wall 408.328s, no timeout. Together with the earlier two classroom files,
the planned three-file visual scope is covered by **82 distinct passing IDs in two separate
serial runs** (80 screenshots and 2 interactions), not one combined invocation. The runs have
identical 761-file source hash maps; all 80 tracked Windows baselines are unchanged.
`web-i18n-full-20260906-01` returned native/wrapper 0 in 3.015s: locale parity passed, while
the advisory audit still reports 27 files with potential UI literals. Full `npm run lint`
(`web-eslint-full-20260906-01`) returned native/wrapper 0 in 176.828s: 0 errors, 49 warnings.
These are passing prescribed commands, not warning-free audits. No warning was suppressed.
Fresh verification of all eight retained Web gate markers (Node, typecheck, build, mocked
browser, both visual windows, i18n and lint) found every native exit 0 and every one of their
761 pre-run input hashes matching current source. Browser marker sourceUnchanged=false and
outer exit 1 remain accurate: Next rewrote two generated configs, then exact restoration
occurred only after native termination. Original production BUILD_ID is unchanged.
The full-site browser gate has mocked auth/API and no WebSocket audit; it does not establish
network isolation or same-candidate live behavior. All owned process/browser/listener counts
are zero; all evidence is retained. No Docker, SSH, registry, commit/push or external-task
notification occurred. Seven-input authority and Linux rollback designs await user decisions;
Task1/7 remain ongoing, Task8 not_ready and same-candidate live evidence remains **0/12**.

Classroom visual checkpoint: the two original classroom visual specs completed **34/34 passed**
(32 screenshot comparisons and 2 editor interactions), Playwright native 0, no skipped,
unexpected or flaky tests, no JSON errors; duration 380.053s / wall 383.266s, no timeout.
Run `playwright-classroom-visual-20260906-01` used one worker, zero retries and
`--update-snapshots=none`; the 32 existing Windows baselines were not changed.
The matrix is 2 hosts × 4 themes × 4 scenes with balanced viewport/language/motion factors,
not every factorial combination. API responses remain fixtures. This window did not include
the separate 48 full-site baseline cases (covered above); 48 live visual cases remain open.
Next modified only next-env.d.ts and tsconfig.json during execution, so the original wrapper
exit 1 and sourceUnchanged=false remain recorded. After native terminal, exact restoration
matched all 761 pre-run Web hashes; production BUILD_ID TX-7z1FZAfe6mXmrkNmue is unchanged.
Owned runner/manager/Next/worker/browser counts and all three listener counts are zero.
Logs, JSON, generated-input snapshot and test outputs are retained under the named
.pytest_cache run; no additional gate, commit, push or cross-task notification was started.

Latest local verification: the complete original mocked classroom browser entry now has
**6 passed, no skipped/unexpected/flaky tests, Playwright native 0**. This closes only the
mocked browser layer, not same-candidate live acceptance. Bootstrap CLI repair has 2 RED → 2 GREEN,
followed by **399 passed / 2 root-POSIX skipped** in the complete affected 11 files, native 0.
The complete Web Node entry returned **723/723 passed, Node native 0**; full Web typecheck
returned **native 0, no diagnostics**. Production build completed **native/wrapper 0**.

The five full-file Python runs below (2044 collected, 2014 passed, 30 skipped,
no failures/errors, all native 0) belong to the **frozen snapshot before the bootstrap
import repair**. Their 1458 hashes matched that snapshot, with no duplicate nodeids and
zero final Python residual. Only bootstrap production and its test file changed afterward;
these historical runs must not be relabeled a complete GREEN for the repaired candidate
or added to selected reruns. Native Linux skips, publication and live acceptance remain open.

- **Six mocked browser flows — locally completed, live layer still open:**
  `playwright-classroom-mocked-20260906-01` ran the existing first-release spec under
  teaching-flow, one worker and zero retries. Teacher preparation/submission/export retry,
  student full/micro handoff, four-format export matrix, content review/publication and
  learning-event retry/exactly-once all passed: native 0, 6 expected, 0 skipped/unexpected/flaky,
  empty JSON errors and all retry=0; Playwright 327.151s / wrapper wall 329.484s, no timeout.
  Official Playwright installed pinned Chromium 143.0.7499.4 build 1200 plus FFMPEG/Winldd
  to E:\CodexTemp\yfeistai-playwright-20260906-01, native 0 / wall 53.094s, browser GC disabled.
  The browser is real; business API and download content are fixtures. Runtime API rewrite
  is explicitly pinned to unused loopback 33123, Web/download bind only 127.0.0.1:33121/33122.
  This does not call production Provider/DB/export services or prove real export artifacts.
  Independent boundary review was checked against source. Next dev includes an npm version
  GET request; together with browser dependency download, this is not a zero-network run.
  Fresh isolated dist/report paths preserve existing build and reports; no baseline update.
  Next rewrote the two checked-in generated configs during execution, so outer guard exit 1
  and marker sourceUnchanged=false are retained. After native terminal, apply_patch restored
  only next-env.d.ts and tsconfig.json from the retained snapshot; all 761 Web input hashes
  then matched the pre-run source. Prior production BUILD_ID TX-7z1FZAfe6mXmrkNmue is unchanged.
  Owned runner/manager/Next/browser/download residual is zero; all three ports are closed.
  Browser and test/build material remain retained. Watchpack EINVAL for System Volume
  Information and Fast Refresh reload warnings did not fail the gate; no speculative product
  change was made. Log/native marker and the full JSON/snapshot subdirectory remain under
  .pytest_cache/playwright-classroom-mocked-20260906-01*. Native Linux rollback, seven-field
  authority, visual matrix, clean candidate and same-candidate live evidence remain open.

- **Bootstrap direct CLI — locally repaired and affected files reverified:** Clearing
  PYTHONPATH and isolating DEEPTUTOR_HOME reproduced native 1, missing `deeptutor`, when
  running the actual script with `--help`, before parsing/registration/DB/HTTP. Two real
  child-process cases cover repository and outside-repository cwd. Effective
  `bootstrap-cli-red-20260906-01`: native 1, 2 failed, pytest 3.33s / wall 6.469s;
  `bootstrap-cli-green-20260906-01`: native 0, 2 passed, pytest 7.91s / wall 11.641s.
  Minimal repair only establishes project/scripts import roots before local imports;
  registration, authorization and public parameters are unchanged. Independent read-only
  review found the original issue fixed and no new Important/Critical issue in this patch.
  Full `bootstrap-cli-adjacent-full-20260906-01`: native 0, 401 collected, 399 passed,
  2 root POSIX skips, no failures/errors, pytest 141.18s / wall 144.703s; no timeout or
  source drift. Actual production image startup and data-plane registration are not proven.
  Durable log/XML/JSON are in `.pytest_cache/`; test material remains intact.
- **Complete offline Web gates — unit entry and typecheck verified:**
  `web-node-full-20260906-01` compiled and executed all 92 `*.test.ts` files, 723 passed,
  no failures/skips, Node native 0; test duration 35.853s / full-entry wall 76.797s.
  The outer Python wrapper wrote its terminal JSON before a GBK Unicode log-echo error
  caused wrapper exit 1. Preserve this wrapper defect separately from the authoritative
  Node result and complete log; subsequent local runner output uses UTF-8.
  `web-typecheck-full-20260906-01`: native/wrapper 0, wall 52.719s, no diagnostics,
  `--noEmit --incremental false`; source unchanged. No live `.spec.ts` executes in the
  Node entry, and no real acceptance token/server is used. These are not live evidence.
  `web-build-full-20260906-01` used existing `npm run build`, including importer prebuild
  synchronization and the protective build wrapper: native/wrapper 0, wall 201.875s,
  timedOut=false; Next 16.2.3 completed all 64 static pages. All 761 Web source hashes
  remained unchanged; checked-in next-env.d.ts and tsconfig.json were restored byte-for-byte.
  standalone/server.js, routes/build/prerender manifests exist; BUILD_ID is
  `TX-7z1FZAfe6mXmrkNmue`, and vendored importer matches the installed dependency hash.
  Independent six-file Web runtime/type-change review found no Critical/Important issue;
  the primary agent verified the diff and all six hashes. Current bootstrap report's 1458
  inputs and both Node/typecheck reports' 761 Web inputs were rehashed with no drift.
  Previous Python snapshot differs only in the two bootstrap files. Fresh 60-file Python
  Ruff/format checks and tracked diff-check returned 0; candidate-owned Node/Python/cmd
  residual is zero. Build artifacts remain local, not deployed or accepted as live evidence.

- **Candidate core/runtime/observer repairs — implemented and full-file locally verified:**
  Registry-resolved workload digests are now checked for collisions before content staging;
  runtime derives the three required Compose root/hash inputs from the original candidate lease,
  never host overrides or a reopened pathname; a fixed letter prefix makes the observer canary
  compatible with both unchanged real receivers while preserving its full 48-byte random suffix.
  Effective `candidate-contracts-red-20260906-01`: native 1, 7 failures/2 controls. Minimal
  `...-green-20260906-01`: native 0, 10 passed, no collection/fixture/environment errors.
  Independent re-reviews found the three original issues fixed and no new Important/Critical
  issue within this patch. Six dirty live-harness files also received read-only review; scope
  and primary-agent-confirmed source hashes are in `.pytest_cache/candidate-review-20260906-01.md`.
  Complete `candidate-core-runtime-observer-full-20260906-01`: **native 0, 685 collected,
  658 passed, 27 skipped, no failures/errors**, pytest 851.75s, wall 864.031s, timedOut=false.
  Scope: full platform Compose, Docker wrapper, Preflight, release verifier, runtime attestation
  and observer service files, without selection filters. Source remained unchanged; all 1458
  recorded hashes matched afterward and Python process count was zero. All 27 skips are native
  POSIX/Linux-only contracts, not verified passes. All **60 changed/new Python files** passed
  Ruff check and format --check; tracked diff-check returned zero, with no source rewriting.
  Subsequent complete 11-file `observer-bootstrap-gateway-outage-full-20260906-01` returned
  **native 0, 399 collected, 397 passed, 2 skipped, no failures/errors**, pytest 116.75s,
  wall 118.938s. Source remained unchanged, all 1458 hashes matched and Python residual was zero.
  Scope: observer/secret/bootstrap, gateway/public trust and dedicated-outage probe/runner.
  Two skips require a root POSIX process. External HTTP/Docker/SSH are replaced; the bounded
  output test uses a real local Python child, with file effects confined to temporary fixtures.
  This is not all-mock execution, native Linux permissions or live deployment evidence.
  Complete six-file `candidate-probes-full-20260906-01`: **native 0, 224 passed, no failures,
  errors or skips**, pytest 14.97s, wall 16.469s; sourceUnchanged=true and all 1458 hashes matched.
  Scope: capacity, export, classroom release, OpenMAIC smoke contract/probe and tenant isolation.
  Transport is fixture-backed; real capacity, Provider, export and tenant acceptance remain open.
  **Complete receipt regression is now locally verified:**
  `release-evidence-full-shortpath-20260906-01` returned **native 0, 480 collected,
  479 passed, 1 POSIX skip, no failures/errors**, pytest 1189.27s, wall 1208.516s;
  timedOut=false, sourceUnchanged=true and all 1458 hashes matched. Prior historical
  `release-evidence-full-20260906-01`
  returned native 1, 475 passed/4 failed/1 skipped. All four failures were 275-character Windows
  fixture paths before the target behavior. With no code changes, fresh short basetemp made
  the same four cases pass (`release-evidence-paths-20260906-01`, native 0). The new complete
  run independently closes that Windows environment failure after the three repairs;
  earlier selected results are not combined to manufacture a full pass.
  Preserve root conftest guards; isolate DEEPTUTOR_HOME/TEMP/TMP before import and use a fresh
  nonexistent short Windows basetemp. Existing test material is not manually cleaned.
  Complete five-file `teaching-health-client-worker-full-20260906-01`: **native 0,
  256 passed, no failures/errors/skips**, pytest 14.97s, wall 19.016s, sourceUnchanged=true
  and all 1458 hashes matched. Scope: active health, route/canary, redaction, existing client
  protocol and worker retry. HTTP/DB are fixtures; real filesystem effects are task-local.
  Before import, account roots were verified beneath the fresh isolated runtime
  `E:\CodexTemp\yfeistai-health-worker-20260906-01\runtime`; TEMP/TMP use its sibling `tmp`.
  Independent read-only Dockerfile/.dockerignore/observer-overlay review found no new
  Important/Critical issue. The then-unproven bootstrap direct-script import concern
  is now reproduced and repaired in the local CLI as recorded above; actual production
  image .pth/sitecustomize and startup remain unverified.
  Remaining Web visual matrix/live browser, native-Linux and complete-diff gates,
  seven-field authority wiring, the native Linux rollback
  decision/fix, clean candidate publication and same-candidate live evidence remain open.
  Tasks 1/7 ongoing, Task 8 `not_ready`, live 0/12. Main/integration HEAD remains
  `2b2664cf9ab19c9209cb8d5ca538a96d0616ef57`; all 16 committed branches are ancestors of main,
  but dirty development remains uncommitted/unmerged. Existing root edits and old plan06
  conflicts are preserved. No real Docker/SSH/network/project commit/push or external-task
  messages occurred. Earlier counts below are historical scopes, not additive current proof.
- **Capacity receipt original-input binding — implemented, reviewed and locally verified:**
  The producer retains the original five-file candidate and readonly runtime file/directory
  identity, bytes and hash through its fixed runner, replay, all three publications and normal
  input closure. It never adopts a replacement snapshot. Publication remains capacity receipt
  → idempotency receipt → proof; original staging handle identity is recorded for each target
  before native linking. Compensation receives only original owned identities.
  Grouped input/output closure preserves the first exception and all later explicit notes,
  including nested cleanup diagnostics when a publication error is already active. Public
  parameters, environment, timeout, thresholds, schema and the POSIX rollback protocol are unchanged.
  Effective REDs: `capacity-inputs-red-20260906-01` native 1, 1 failed after real proof publication
  and runtime body drift; `...-red-20260906-03` native 1, 1 failed after real compound input close;
  `...-red-20260906-04` native 1, 3 failed for output close ordering and nested diagnostic loss.
  These hooks were reached, with no collection/fixture/environment errors.
  Final frozen-source `capacity-inputs-reviewed-green-20260906-02`: **native 0, 124 passed,
  356 deselected, no failures/errors/skips**, pytest 261.26s, wall 267.672s, timedOut=false,
  sourceUnchanged=true; all 1458 recorded source hashes matched afterward. Scope: 34 new capacity,
  18 existing capacity, 22 preflight-input, 7 close-failure, 22 running-container and 21 source-head
  cases. The earlier 121-item and observer 166-item runs predate the final source snapshot;
  they are not additive proof of a current complete release gate.
  Runner execution is fixture-backed; the positive path uses the real producer/assembler/file
  runtime. Some Windows same-byte replacement cases inject identity; native link/close tests
  perform the real operations before failure. No live HTTP capacity or POSIX rollback fix is proven.
  Independent spec and quality reviews passed, as did scoped Ruff, format and diff checks.
  Log/XML/JSON and test materials are retained in `.pytest_cache/`; final Python process count is zero.
  Main/integration HEAD remain `2b2664cf9ab19c9209cb8d5ca538a96d0616ef57`; all 16 committed local
  branches are ancestors of main. Dirty development is uncommitted, unintegrated and unpublished;
  existing dirty/conflict state is preserved. No Docker/SSH/registry/project commit/push or
  external-task notification occurred. Seven-field authority and Linux rollback decisions,
  complete candidate verification and live acceptance remain open. Tasks 1/7 ongoing;
  Task 8 `not_ready`; same-candidate live **0/12**.
- **Actual observer Compose environment — implemented and locally verified, authority wiring pending:**
  Host Preflight now validates the returned observer environment, never substituting host values
  for missing Compose fields. It shares runtime ID, HTTPS endpoint/fixed-path, five-authority
  separation and UTC 30–7200-second expiry rules, and compares four actual release fields with
  validated admission. This check loads no credentials or network client; a fixed redacted
  `observer deployment configuration` diagnostic does not echo supplied URLs or secrets.
  Effective `observer-environment-preflight-red-20260906-01`: native 1, 31 failed/3 controls
  (pytest 17.16s, wall 18.781s). First combination: 115 passed/6 failed, all six old Compose
  fixtures missing environment; valid fixtures were added without weakening their assertions.
  Subsequent groups returned native 0, 121/121 and 45/45. Fresh six-file combined confirmation
  `observer-environment-confirmed-green-20260906-01`: **native 0, 166 passed, no failures,
  errors or skips**, pytest 44.76s, wall 47.031s, sourceUnchanged=true and all recorded hashes
  matched afterward. Scope: actual-environment negative/positive contracts, observer service,
  preflight, deployment contract, signing and bootstrap; no real Docker deployment is proven.
  Log/XML/JSON and test materials remain under the integration tree `.pytest_cache/`.
  Independent read-only review found no new blocker. Earlier 62- and 119-item receipt results
  are prior snapshots, not additive evidence of a current complete release gate.
  Main/integration HEAD are still `2b2664cf9ab19c9209cb8d5ca538a96d0616ef57`; all committed
  local branches are ancestors of main, while dirty development changes remain unintegrated.
  Capacity input binding across all three publications and normal input closure is now locally
  completed above; this observer run predates the subsequent capacity/shared-helper edits.
  Seven-field authoritative wiring, native Linux rollback and same-candidate live acceptance
  remain incomplete. Tasks 1/7 ongoing; Task 8 not_ready; live **0/12**. No cross-task notices.
- **Preflight receipt original-input binding — implemented and locally verified:**
  The producer now retains the five-file candidate lease and the original readonly runtime
  file/directory identity, bytes and hash across both execution phases, replay and publication.
  Drift stops the next phase. Normal candidate exit, the final runtime recheck, input close
  and output recheck are inside the failure-compensation scope. Running-containers shares
  the extracted readonly input helper without changing its semantic verifier.
  Staging uses creation-handle identity/body; descriptor-bound no-replace publication records
  each target's original identity before the native call. Failure archives receive only owned
  identity tuples; secondary close/archive errors annotate the primary error. Existing POSIX
  pathname rename/unlink limitations remain unresolved and are not certified by this work.
  Effective RED: `preflight-inputs-red-20260906-03` native 1, 16 failed/1 control
  (pytest 46.59s, wall 48.469s), and `preflight-compensation-red-20260906-01` native 1,
  3 failed (pytest 10.03s, wall 12.297s). Earlier phase/receipt-field fixture mistakes are not RED.
  First combination returned native 1, 59 passed/1 failed; the obsolete three-read `_candidate`
  injection was migrated to real derive followed by competing-file creation, preserving its
  no-overwrite/no-quarantine assertions. Final `preflight-inputs-reviewed-green-20260906-01`
  returned native 0: **62 passed, 363 deselected, no errors/skips**, pytest 154.34s,
  wall 160.125s, sourceUnchanged=true. Coverage: 22 new preflight, 22 running-container,
  7 close-failure and 11 existing preflight items. The real producer/assembler/file-runtime
  positive case proves only database-revisions and service-health receipt replay, not live services.
  Docker execution is fixture-backed; Windows replacement cases include identity injection.
  Ruff/format passed; independent review found no new blocker. Evidence is in the integration
  tree `.pytest_cache/`; test materials are retained, final Python process count is zero.
  Earlier 119-item and 920-file Linux snapshots predate this edit and are not current full gates.
  Main and integration HEAD remain `2b2664cf9ab19c9209cb8d5ca538a96d0616ef57`; all committed
  local branches are ancestors of main, but this dirty development candidate is not integrated.
  No Docker, SSH, registry access, project commit/push or external-task coordination occurred.
  Task 1/7 remain in progress; Task 8 is `not_ready`, same-candidate live evidence **0/12**.
- **Seven deployment inputs — actual interpolation validated, authoritative carrier undecided:** `YFEISTAI_ENVIRONMENT_ID`
  and `YFEISTAI_SHARED_INGRESS_{OBSERVER_ID,OBSERVER_URL,CONTROL_URL,UPSTREAM_URL,CANDIDATE_HEALTH_URL,AUTH_EXPIRES_AT}`
  have no authoritative platform-settings/provisioning source. Wrapper and host preflight
  rebuild the other eight manifest fields but inherit these seven from the host. Host Compose
  inspection now shares runtime ID, authority and expiry validation; valid values do not prove
  their authoritative source. The deployment plan requires that source but does not approve
  a settings carrier, new file or manifest extension. Existing manifest equality is strict and
  PlatformSettings forbids extras. Recommended, not yet approved or implemented: version the
  committed observer manifest to bind seven explicit deployment inputs, derive both launcher
  and host Preflight values from it, and clear host overrides. Do not silently amend existing
  manifests, invent deployment IDs/domains or automatically extend expired authorization.
  Source audit on 2026-09-06 further distinguishes the six stable deployment values from
  the seventh value, the absolute per-run observation/control authorization expiry.
  `openmaic_observer_service.parse_observer_auth_expiry` admits only 30..7200 seconds
  remaining; `load_config` applies it at startup and `create_app` reapplies it on restart.
  Observation/control requests also check expiry. Internal proxy/API forwarding branches
  precede that check, so expiry does not prove that all already-running proxy traffic stops;
  conversely an expired configuration cannot pass a fresh startup or host preflight.
  Provisioning rejects existing run directories and committed-batch recovery, has no renewal
  CLI, and creates new tokens/keypair for each new batch. A manifest extension therefore
  needs an explicit per-run/reissue lifecycle, not merely seven-field wiring. Recommended
  but still undecided: create the batch near acceptance, fail closed on expiry, and use a
  new run/batch for a retry. Do not infer approval for transparent renewal, longer validity,
  same-run key rotation, or a long-lived service restart policy. This is a source-only audit,
  not a deployment result or a change to the pending design status.
- **Historical native Linux rollback RED — closed by the production integration above:**
  `test_posix_receipt_cleanup.py` exercises manifest and shared classroom cleanup with
  one owned-file control and one replacement case each. A deterministic hook replaces
  the pathname using real rename immediately before real unlink; this is native I/O,
  not an independent-process concurrency stress test. Ubuntu 22.04 / ext4 / UID 1000 /
  Python 3.11.15 returned native 1: **2 passed, 2 failed**, no errors/skips (pytest 3.65s,
  wall 6.754s). Both failures show the replacement name being deleted after the inode check.
  All 920 snapshot file hashes stayed unchanged and matched the worktree at that stage.
  Archive SHA256: `1681ef8c8bc9e9039c88832e7ee979d08f19173922d45d8126b9cfd2eb223174`.
  Logs/XML/native exit/source manifest: `E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210\posix-receipt-cleanup-native-red-20260906-02\`.
  Native snapshot: `/home/xinlingzhifei/.cache/yfeistai-plan07-linux-20260905/source-posix-receipt-cleanup-pjaiiso2/`.
  Original and foreign fixture objects retain recovery hardlinks; no business data was
  cleaned. The first mounted-worktree attempt failed on the Windows gitdir format before
  pytest started, and is not RED evidence. A verified source archive avoided that boundary.
  No production edits, dependency installs, SSH/Docker commands, project commit or push.
  Ruff/format passed; owned Linux pytest/wrapper and Windows Python processes are zero.
  Independent review confirms that pathname unlink has no expected-inode predicate;
  extra stat calls and cooperative flock do not exclude arbitrary non-cooperating swaps.
  O_TMPFILE removes unpublished staging cleanup only, not published-receipt rollback.
  Design **explicitly confirmed by the user on 2026-09-06; implementation in progress**:
  [POSIX receipt recovery plan](./2026-09-06-plan07-posix-receipt-recovery.md).
  A dedicated publisher
  identity protects an actually private same-filesystem quarantine; atomically relocate,
  validate against the original held file identity, remove only owned objects, and restore
  foreign entries with no-replace rename. On restore conflict retain both objects and
  report the exact recovery path. This changes the foreign-entry location contract;
  same-UID mode-0700 directories alone are not isolation. Link-as-final-commit is a separate
  alternative requiring a different post-link failure contract, not a drop-in fix.
  References: [unlink(2)](https://man7.org/linux/man-pages/man2/unlink.2.html),
  [rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html).
  Those two failures were release blockers at that stage and are now closed by the
  current native integration above. Actual deployment isolation/rollback remains unproved.
  Tasks 1/7 ongoing, Task 8 not_ready, live **0/12**; test/recovery material is retained.

- **Integrated review is blocking candidate commit/publication:** the launcher
  key mismatch, host-preflight topology and missing image templates now have
  the local fixes below. Actual Compose/image execution remains unverified.
  Five-file runtime semantic validation now has the scoped fix below;
  standalone outage publication and closing-tail rollback now have the scoped fix below;
  image-digest receipt publication now has the scoped fix below;
  source-head/running-container publication now has the scoped fix below;
  POSIX receipt rollback now has the native production integration above, with real
  deployment identity/filesystem validation still open;
  shared/dedicated plane publication now has the scoped fix below;
  generic probe publication now has the focused fix below;
  the external-supervisor final-commit path now has the scoped fix below.
  Cleanup previously treated unknown staging ownership as permission to remove
  a pathname; that P1 now has a minimal fix and the scoped evidence below.
  Actual observer/control/upstream ingress, bootstrap commit-time effects and
  existing shared-route upgrades remain separate unresolved review boundaries.
  Local GREEN below does not close these newly identified integration gaps.

- **Source-head/running-container publication binding implemented; local regression passed:**
  The three receipt producers share one no-replace transaction and retain the original
  five-file candidate through normal lease exit. Source-head binds the source-directory
  identity and checks Git HEAD/status/origin initially, before/after publication and
  after candidate exit. Running-containers retains the fixed attestation handle,
  original identity, bytes and hash throughout semantic validation and publication.
  Input closure stays inside rollback: only original staging identities authorize
  compensation; foreign outputs and producer inputs remain untouched. Native directory
  close failures retry only still-owned leases; successful closes never reclaim an old
  numeric handle. Cleanup failures annotate rather than replace the primary error.
  Runtime input opening no longer requests DELETE; export/output defaults are unchanged.
  Initial RED: native 1, 35 failed/6 controls (pytest 71.63s, wall 75.672s). First retry:
  native 1, source 19 passed/runtime 22 failed due to DELETE access conflicting with the
  fixed read-only verifier. The minimal access fix yielded native 0, 41/41 (pytest 73.26s,
  wall 76.890s). Seven input-close faults and two real local-Git cases passed. Two stale
  adjacent source tests first failed; only their exact four-stage query expectation and
  real lock-file drift injection were updated, then both passed.
  Final `receipt-producers-reviewed-green-20260906-01`: native 0, **99 passed/325 deselected**
  (pytest 161.84s, wall 167.313s). `receipt-producers-consumers-green-20260906-01`: native 0,
  **20 passed** (pytest 59.09s, wall 62.062s). XML union: **119 distinct items**, no failures,
  errors or skips; both sourceUnchanged=true and all recorded hashes match current files.
  Scope includes the three producers, input closure, actual temporary Git execution,
  runtime producer-to-assembler-to-verifier, manifest rollback/competitors, CLI routing,
  CI static wiring and downloaded artifact validation, not a full release/repository gate.
  Ruff/format/scoped diff-check passed; independent read-only review found no new blocker.
  Windows replacement/close faults include injection models. Mixed validation+close
  faults cover pre-publication primary-error preservation; late rollback is covered by
  single close faults, not a claimed combined late-fault proof. Local file I/O is real.
  No Docker/SSH/network/registry or project commit/push/merge. Git commits occur only in
  isolated pytest fixtures; test materials remain. Owned Python/Git process count is zero.
  Logs/XML/exit-and-source-hash records are in this worktree's `.pytest_cache/`.
  **Next:** POSIX stat-to-unlink rollback safety and native Linux verification, remaining
  receipt producer inputs, seven deployment input bindings and complete candidate gates.
  Plan07 Tasks 1/7 remain ongoing, Task 8 not_ready, live acceptance **0/12**.

- **Image-digest receipt publication binding implemented; local regression passed:**
  `write_image_digest_receipt()` retains one five-file candidate lease through
  publication and normal lease exit, then rechecks the original output identity/body.
  Descriptor-bound no-replace publication rejects all existing targets, including
  identical ones; arbitrary output paths and missing-parent creation remain supported.
  Compensation uses only the original staging identity and, after directory close,
  requires the original directory identity. Foreign files are never claimed by content.
  Bundle staging creation failures now use the matching identity-bound cleanup.
  Image-specific close invokes the native interface directly: only a Windows native
  OSError restores the owned lease. Old numeric handles are not reclaimed by directory
  identity; POSIX close errors never retry the old descriptor.
  Initial native RED: 10 failed/6 controls (pytest 19.83s, wall 23.563s), then 16/16 GREEN.
  Additional RED: staging cleanup 1 failed/4 controls, output replacement at normal
  lease exit 1 failed, reused-handle lifecycle model 1 failed. No collection/fixture errors.
  Final `image-receipt-reviewed-green-20260906-01`: native 0, **28 passed/346 deselected**
  (pytest 32.03s, wall 35.906s). Adjacent manifest generation/rollback, CLI, CI static
  wiring and downloaded-promotion consumers: `image-receipt-adjacent-green-20260906-01`,
  native 0, **20 passed** (pytest 58.15s, wall 62.078s). XML union is **48 distinct passes**,
  zero failures/errors/skips; both runs sourceUnchanged=true, all recorded source hashes
  still match. This is neither a whole-repository nor a complete release gate.
  Ruff, format and scoped diff-check passed; independent review found no new Windows
  blocker. Owned Python process count is zero. Evidence: `.pytest_cache/image-receipt-*-20260906-01.*`.
  One wrapper argument-parse failure never started pytest and is not test evidence;
  the corrected invocation used the argument delimiter. Fault-test materials remain.
  Local native file publication is real; candidate replacement and close faults include
  injected models. No Docker, SSH, registry/network, push or actual release was run.
  **Existing POSIX stat-to-unlink foreign-file races remain unresolved and unproven**;
  unrecoverable cleanup faults only annotate the primary failure. No Linux-release claim.
  Source-head/running-container publication is now covered by the newer entry above.
  Linux rollback safety, deployment wiring and same-candidate live gates remain open.
  Plan07 Tasks 1/7 remain ongoing, Task 8 not_ready, live acceptance 0/12.

- **Standalone outage parent lease and closing-tail rollback implemented; local regression passed:**
  The real parent entry retains one validated five-file lease from before child
  spawn through successful return, observer signature/body archival, stdout flush,
  receipt-last publication and normal lease exit. One runtime guard retains original
  staging ownership for the three outputs. Preexisting/competing identical archives
  are bound but never claimed. Failed-child reconciliation keeps its original order.
  Initial 30 cases returned native 1: 27 failed/3 controls (pytest 29.45s,
  wall 33.094s), then native 0: 30 passed (pytest 29.63s, wall 33.188s).
  The controls are positive execution, the existing lock-at-child-return rejection,
  and competing-file preservation, not new RED evidence. No collection/fixture errors.
  After the first 67-item adjacent GREEN, independent review identified release/guard
  close errors leaving owned outputs behind. Tail RED was 5 failed/3 controls
  (pytest 8.77s, wall 11.797s); a separate Windows native-CloseHandle-before-close
  model failed its exact case (pytest 2.21s, wall 5.219s).
  Tail compensation now freezes original identities from bindings/owned, verifies
  the original directories after reopening, and on Windows pins and removes only
  matching identities using the same handle. A failed release immediately checks
  its still-live original handle; successfully closed handles are not retained.
  Cleanup errors annotate rather than mask the primary error; competitors survive.
  The complete runner file returned native 0: 50 passed (pytest 36.55s,
  wall 39.782s). Final `outage-parent-final-adjacent-20260906-01` returned
  **native 0: 75 passed**, zero failures/errors/skips (pytest 48.70s, wall 52.062s),
  sourceUnchanged=true, all recorded source hashes matching current files.
  Scope: runner 50, complete observer deployment contracts 17, child lifecycle/
  publication 5, final verifier 2, dedicated producer-to-assembler positive 1.
  The 50 are included in 75, not additional passes. Ruff, format, scoped diff-check
  and final independent review passed; owned Python process count is zero.
  Reports under `.pytest_cache/`: `outage-parent-candidate-{red,green,adjacent}-20260906-01.*`,
  `outage-parent-tail-close-{red,green}-20260906-01.*`,
  `outage-parent-native-close-red-20260906-01.*`, and
  `outage-parent-final-adjacent-20260906-01.*`.
  Two old fake-PID timeout tests now isolate their lowest-level kill helper; no
  real kill, Docker, SSH or network was invoked. Temporary fixtures were retained.
  Windows candidate replacement and close faults are injected models; local native
  publication/pinning/rollback remain real. Written stdout cannot be retracted;
  failure requires a nonzero exit and no newly owned formal receipt. Preexisting
  and competing files are preserved. Existing POSIX stat-to-unlink races,
  unrecoverable cleanup failures and actual outage execution remain unproven.
  Image-digest publication binding now has the newer scoped evidence above; deployment wiring,
  real image/runtime evidence and the complete immutable-candidate gates remain open.

- **Shared/dedicated plane publication lease implemented; local regression passed:**
  `_write_openmaic_plane_receipt()` retains one five-file lease and delegates to
  a bound helper using the original candidate copy. The original binding checks,
  runner admission/return and each receipt-to-proof publication recheck that lease.
  Explicit lease close is inside the existing two-file publication rollback;
  only recorded receipt/proof identities are cleanup targets. Runtime, outage,
  observer, signature, attempt marker and external public-key inputs are preserved.
  The new 34 cases returned native 1: 8 failed/26 passed (pytest 106.19s,
  wall 110.782s). Valid new RED cases are base-Compose same-byte identity changes
  at three phases for both planes, plus two lease-exit faults. The other 26 are
  existing rejection controls, not new RED evidence. Minimal production changes
  returned native 0: 34 passed (pytest 92.53s, wall 96.063s), no errors/skips.
  The two evidence/verifier files filtered by `openmaic or plane` then returned
  native 0: 64 passed/501 deselected (pytest 186.94s, wall 192.125s). This includes
  the 34 cases, local real producer-to-assembler fixtures, proof-tampering
  rejection, target-parent replacement and CLI forwarding, not two full files.
  Review found the observer deployment AST contract still inspecting the former
  wrapper. Its exact case reproduced native 1, 1 failed (pytest 1.18s,
  wall 4.594s). Only three function locators changed; signature/trusted-time
  assertions were retained. The complete observer deployment contract file plus
  remaining callers returned native 0: 28 passed (pytest 43.82s, wall 47.671s).
  The 64+28 XML union is 92 distinct passing items with no errors/skips/failures.
  Only that contract test changed between these runs; all production hashes are
  identical and current, with sourceUnchanged=true within each run. Ruff, format,
  scoped diff-check and independent scoped review passed. Evidence under
  `.pytest_cache/`: `plane-candidate-publication-{red,green,adjacent}-20260906-01.*`,
  `plane-observer-contract-red-20260906-01.*`, and
  `plane-observer-contract-consumers-green-20260906-01.*`.
  Owned Python process count is zero; temporary fixtures remain without manual
  cleanup. Windows replacement is a stat/lstat identity model; exit faults are
  injected after a real normal lease exit. This does not prove sharing-lock
  bypass, native close faults, compound close failures, or the POSIX branch.
  Later standalone outage parent progress is recorded above. Actual deployment/publication and
  all twelve live evidence categories remain unverified; Plan07 Task 7 is ongoing.

- **Generic probe publication candidate lease implemented; local combined regression passed:**
  One validated five-file lease now spans runner admission/return and all three
  report/execution/receipt publications. Explicit close stays inside rollback,
  and failure archival stays inside the exclusive probe claim. The two Windows
  archive helpers transfer the original anchored handle to an fd and compare
  fstat ownership, avoiding incompatible native-file-ID/Python-stat comparisons;
  no pathname reopen or competitor cleanup was introduced.
  The original 17 tests produced native 1, 13 failed/4 existing rejection controls
  (pytest 50.59s, wall 53.672s). First GREEN attempt failed all 17 before runner
  execution: creating raw/artifacts changed the candidate directory timestamps.
  Directory binding now retains dev/ino/mode and rejects link/reparse, while
  protected files retain all six signature fields, held-fd rereads and journal
  checks. Directory positive/negative contracts produced 1 failed/16 passed
  (pytest 12.46s, wall 14.265s); their first attempt used an ineffective os.lstat
  mock, corrected to Path.lstat, and is not rejection evidence.
  Combined focused verification returned native 0: 34 passed, no errors/skips
  (pytest 54.76s, wall 56.625s), sourceUnchanged=true. Reports:
  `probe-candidate-publication-red-20260906-02.*`,
  `candidate-directory-contract-red-20260906-02.*`,
  `probe-candidate-publication-green-20260906-02.*`.
  Both complete files, `test_platform_compose.py` and
  `test_classroom_release_evidence.py`, returned native 1 under
  `probe-candidate-publication-full-20260906-01`: 484 collected, 482 passed,
  1 failed, 1 POSIX FIFO skipped (pytest 941.52s, wall 954.531s), no collection
  or fixture errors. The sole failure used catchable BaseException as a hard
  process crash; the new rollback correctly removed its expected leftovers.
  Production protection was not weakened. The test now uses a real Python child
  calling os._exit(83) after receipt publication; it asserts exact exit, retained
  trio/claim, absent failure archive/supervisor marker, and real assembler refusal.
  A separate KeyboardInterrupt test verifies normal unwinding removes the trio
  and claim while archiving execution/receipt. Directory explicit-check tests now
  fail immediately if rejection is deferred to exit; stable-file comparisons
  exclude read-dependent atime. Final verification in
  `probe-candidate-publication-followup-20260906-02` returned native 0: 36 passed,
  no failures/errors/skips (pytest 64.75s, wall 68.063s). Ruff, format, scoped
  diff-check and independent review passed. Both runs have sourceUnchanged=true;
  only the two test files differ between runs, and production hashes are identical
  and current. XML union is 484 distinct passing items plus 1 POSIX skip, not a
  single complete 485-item GREEN run. Owned Python process count is zero; temporary
  fixtures, including the hard-exit claim, were retained without manual cleanup.
  Later shared/dedicated parent execution and proof-last publication progress is
  recorded above, along with later standalone outage runner progress.
  Directory mutation and Windows candidate replacement use identity models;
  this does not prove bypassing Windows sharing locks, detecting every transient
  directory ABA, Linux execution, actual probes, or plane/outage publication.

- **External-supervisor final-commit candidate lease implemented; local caller regression passed:**
  `write_probe_final_commit_marker()` now retains one validated five-file lease
  through receipt validation, real Ed25519 verification, public-key/marker
  publication and normal lease exit. Checks follow verification, each publication
  and final fsync. Failure rolls back only recorded identities through the retained
  commit-directory handle, preserving preexisting keys, external signature/key
  sources, the original trio and competing replacements. Windows cleanup uses the
  same stat/fstat identity representation as publication ownership. Cleanup errors
  are attached without hiding the primary failure or skipping the next owned file.
  Five files at three phases, lease-exit failure and two competing-output cases
  produced native 1, 18 failed (pytest 66.72s, wall 70.765s), then native 0,
  18 passed (pytest 56.86s, wall 60.641s), with unchanged source hashes and no
  collection/fixture errors. Independent scoped review found no blocker.
  Reports: `supervisor-candidate-publication-{red,green}-20260906-01.*`.
  Adjacent signature/no-replace/manifest regression returned native 0: 62 passed,
  238 deselected (pytest 205.92s, wall 211.922s). A call-site audit identified
  three remaining consumer groups; those returned native 0: 33 passed
  (pytest 104.85s, wall 109.125s). The two runs contain 95 distinct items, no
  failures/errors/skips; the new 18 are included, not additional passes.
  Reports: `supervisor-candidate-publication-{adjacent,consumers}-20260906-01.*`;
  source hashes unchanged and matching current production, owned process count=0.
  This is not a rerun of all 300 file items. Later generic probe and plane
  publication-lease and standalone outage progress is recorded above.
  Windows input replacement is a stat/lstat model, not a real sharing-lock bypass;
  the exit fault is modeled after real lease exit, not a native os.close failure.
  This does not prove POSIX execution or other producers' publication lifecycle.

- **Runtime five-file candidate validation implemented; local consumer regression passed:**
  the existing lease retained five files but its token omitted base/data-plane
  bytes. Real stable-tamper cases failed as expected before the fix: native 1,
  11 failures and 3 controls passing. The token now preserves positions 0–2 and
  appends base/data-plane, rejects incomplete/non-byte tokens, and strictly
  validates all retained Compose bytes before parsing services. No candidate
  pathname content is reopened by token validation. Exact 14/14 passed, native
  0 (pytest 3.52s, wall 6.593s), with independent local review finding no blocker.
  Reports: `runtime-five-file-contract-{red,green}-20260906-01.*`. The runtime
  fixture now renders real templates and includes the actual service set.
  First full-file run returned native 1: 118 passed, 1 failed, 25 platform skips
  (pytest 126.14s, wall 129.844s). The single outdated fixture omitted image-default
  volumes for newly included services sharing the workload image; production
  volume checks remain unchanged. Corrected security/stable cases passed 2/2,
  native 0 (pytest 3.89s, wall 13.828s). Complete runtime + verifier regression
  finished in its original session with native 0: 358 collected, 332 passed and
  26 POSIX skips, no failures/errors/timeout (pytest 848.37s, wall 863.093s).
  Reports: `runtime-five-file-consumers-full-20260906-01.{log,xml,json}`;
  sourceUnchanged=true and owned process count=0.
  This does not prove Docker execution, POSIX-specific cases, or all other
  receipt producers' candidate identity retention during publication.

- **Task 1 integration fixes implemented, local evidence only:** the launcher
  now includes exactly `YFEISTAI_CANDIDATE_SHA256` and
  `YFEISTAI_IMAGE_LOCK_SHA256`, preserving exact-key rejection and ambient
  cleanup. Real provisioner/committed-consumer/wrapper-admission tests produced
  3 failures/1 control pass, then 4 passes; independent review passed. Metadata,
  POSIX preflight, docker.env writes and actual subprocess are isolated, so this
  is not a DAC/Docker proof. Preflight now uses
  `platform_compose_topology_arguments`, the candidate base file, fixed deployment
  root/project/env file and read-only `--profile legacy-local config` semantics;
  existing input bytes stay unchanged
  and host project-name pollution is removed. Two targeted cases first failed,
  then passed. Two exact COPY instructions package the four trusted templates;
  four precise `.dockerignore` exceptions restore their context inclusion without
  copying an entire deployment directory. Both packaging cases first failed,
  then passed. Reports are `observer-launcher-environment-*`,
  `preflight-candidate-topology-*` and `candidate-template-packaging-*`, all
  `20260906-01`. Six complete affected files returned native 0, 159 passed and
  3 platform skips (pytest 44.60s, wall 47.860s), source hashes unchanged including
  `.dockerignore`; `task1-integration-fixes-full-20260906-01.*`. Complete platform
  contract tests completed in `task1-platform-contracts-full-20260906-01`: native
  0, 151 passed, no skips (pytest 84.12s, wall 86.937s), source unchanged.
  Independent review completed, identifying a pre-existing host-preflight
  manifest-environment gap. The follow-up uses the same launcher admission
  consumer and leased lock bytes, passes three static inputs and eight runtime
  inputs plus the candidate root, strips ambient overrides, and revalidates
  before runtime, before each Compose subprocess and after config. Missing
  manifests fail closed for host runtime; manual observer flags cannot substitute
  or override the manifest. Six cases first failed, then passed; three complete
  affected files returned native 0, 134 passed and 3 skips (pytest 38.71s,
  wall 41.672s), source unchanged. Records:
  `preflight-observer-manifest-{red,green,full}-20260906-01.*`. Review found no
  source blocker; its deployment-command finding is corrected to use the same
  manifest; independent review confirmed the documentation fix. The final seven-file
  combination `task1-integration-manifest-seven-full-20260906-02` returned native
  0: 319 collected, 316 passed, 3 platform skips (pytest 132.23s, wall 136.110s),
  no failures/errors/timeouts, source hashes unchanged. The `-01` attempt had a
  mistyped test path, native 4 and zero executed tests; it is retained only as a
  launch error, not regression evidence. The seven separate
  environment/shared-ingress deployment inputs still lack complete authoritative
  wiring; neither actual interpolation nor service readiness is proven.
  No Docker, registry or live proof is claimed; the historical image lock is unchanged.

- **Staging ownership Windows regression GREEN; five Linux caller cases pending:** real create
  collisions, unknown ownership and pre-return replacement produced RED
  (4 failed/3 passed); a separate fdopen regression failed for an owned-file leak.
  Cleanup now requires a matching recorded identity; creation captures it before
  fragile writes/flush/fdopen, preserving original errors with cleanup notes.
  Windows exact 8/8 and 17 existing affected publication/cleanup cases passed,
  native 0 (affected pytest 70.40s, wall 74.516s). The same eight cases passed
  natively on Linux ext4 as UID 1000, Python 3.11.15: native 0, no skips (pytest
  5.27s, wall 10.112s). All 915 snapshot source hashes matched before/after and
  the repo at that run; owned residuals were zero. Windows records:
  `.pytest_cache/staging-ownership-*-20260906-01.*`; Linux task record:
  `staging-ownership-native-20260906-01/staging-ownership-native-result.json`.
  Initial spec and quality review passed. The complete file run ended native
  exit 1 with 276 passed, 4 failed and 1 skipped (pytest 865.03s, wall 879.078s,
  source unchanged). All four failures exposed a caller clearing its closed
  leases before finally reused their recorded ownership identities. The minimal
  follow-up retains those identities independently of handle lifetime. A new
  real-replacement regression first failed, then passed with the four failures
  and original eight ownership cases: 13 passed, native exit 0 (pytest 24.30s,
  wall 27.578s). The affected cleanup slice passed 12/12 (pytest 60.91s, wall
  65.297s, native 0); independent follow-up spec and quality reviews passed.
  Fresh Linux checks passed the eight ownership cases, but five caller cases
  failed during dependency imports: first the snapshot omitted the teaching
  fixture, then the corrected 916-file snapshot exposed missing `jsonschema`
  in the isolated venv. Both are environment-incomplete results, not behavioral
  RED/GREEN; no dependencies were installed. Latest snapshot SHA-256:
  `160fd8022c78816a17476bb98f9a36788f10379cab68a2b235945e960d42e71e`;
  source unchanged/current-repo matching and owned residuals zero. Windows
  `staging-ownership-caller-full-windows-20260906-01` is now complete: 282
  collected, 281 passed, one platform skip, native exit 0 (pytest 766.94s, wall
  780.063s), no failures/errors/timeouts, unchanged source hashes and owned
  wrapper/pytest residuals zero. The skip is the actual FIFO case independently
  verified on Linux earlier; the five new Linux caller cases remain unverified.
  Existing POSIX stat-to-unlink race semantics and unused unbound staging are
  outside this fix. The unchanged Windows native creator can mask its original
  write/flush error if its own cleanup fails (pre-existing P2 diagnostic limit).
  No live acceptance is implied.

- **Integrated pre-commit audit is still in progress:** all 52 changed/untracked
  Python files passed Ruff and format check after seven mechanical formatting
  changes and one import-format fix; scoped diff-check passed. The complete
  observer/bootstrap six-file run passed 107/107, native 0 (pytest 11.43s, wall
  16.344s), with before/after source hashes unchanged. That result is not a spec
  sign-off: independent review identified an internal observer API routing gap
  and missing strict bootstrap candidate semantics before effects. The routing
  gap is now fixed with two lines reusing the existing proxy: exact RED returned
  4 failed/10 passed, then exact GREEN 14/14 and the complete service file 47/47,
  all with native exits and unchanged source hashes during each run. Independent
  spec/quality re-review approved that fix. Bootstrap now reuses the complete
  `validated_candidate_artifact_lease` before observer/runtime effects while
  retaining unchanged checks through health and the DB transaction. Six invalid
  candidate cases produced behavioral RED (6 failed/1 valid control passed),
  then exact GREEN 7/7 and both bootstrap files 14/14, native 0, zero skips/errors
  (pytest 10.01s, wall 13.922s for the full pair). Independent spec/quality
  re-review approved this narrow fix. Reports are
  `observer-internal-{exact-red,exact-green,full-green}-20260905.*` and
  `bootstrap-semantics-{red,green,full}-20260905.*`.
  The gateway/outage four-file run returned
  234 passed/1 failed, native 1 (pytest 71.82s); its stale secret-redaction fixture
  lacked candidate fields. A real schema3 temporary candidate and an assertion
  that the probe body was reached restore its intended check (exact native 0,
  1/1, pytest 1.88s), without production changes. Complete four-file follow-up
  then passed 235/235, zero skips/errors, native 0 (pytest 75.20s, wall 78.907s),
  with source hashes unchanged; `gateway-outage-followup-20260905.*` records it.
  Evidence is retained as `.pytest_cache/observer-bootstrap-full-20260905.*`,
  `gateway-outage-full-20260905.*`, and `outage-secret-fixture-exact-20260905.*`.
- **Probe-boundary regression and stale test inputs:** six complete capacity,
  export, release-browser, smoke-contract/probe and tenant files returned 219
  passed/5 failed, native 1 (pytest 24.09s, wall 28.375s). Four CLI tests supplied
  incomplete config stand-ins and exited at the candidate lease before the
  intended interrupt/flush/redaction behavior; the visual test matched an old
  inline URL expression. Test-only fixes now exercise real schema3 candidates
  and CLI config, assert the probe was reached, and check the current HTTPS,
  same-origin, credential-free, PUT/path and successful-response structure.
  The original five passed native 0 (pytest 5.30s), then all three affected files
  passed 152/152, native 0 (pytest 13.76s, wall 17.000s), with source hashes
  unchanged during execution. No production behavior was loosened. Reports:
  `probe-boundaries-full-20260905.*` and
  `probe-cli-fixtures-{exact-green,three-files-green}-20260905.*`.
- **Public-key DAC defect fixed and independently reviewed:** provisioning now
  uses operator UID / container GID and 0440 only for the public key; private
  token/key modes and 0700 directories are unchanged. This matches
  [Docker's host-file secret ownership behavior](https://docs.docker.com/reference/compose-file/services/).
  The same two native Linux cases failed before the two-line change and passed
  afterward (native 0, 2/2, pytest 5.31s, wall 9.268s). Real chown/chmod/stat,
  same-inode hardlinks and UID/GID-dropping children prove operator read,
  bootstrap read/no-write and unauthorized 2002:2002 denial. Source hashes were
  unchanged during each run and residuals were zero. Independent spec and
  quality review approved the narrow fix. Records are in task directories
  `native-boundaries-{current,green}-20260905-01/public-key-posix-*` (JSON/XML).
  This is file DAC evidence, not nonprivileged provisioning, ancestor-path
  traversal or Docker mount proof. The first complete Windows eight-file follow-up
  returned 220 passed, 2 skipped, 1 failed (native 1): the signature test called
  the removed `_write_candidate_lock` fixture before reaching crypto assertions.
  This was a fixture error, not behavioral RED. Its test-only caller now uses
  the complete schema3 fixture and current binding calculation; real signature,
  wrong-key, SPKI and time-window assertions are unchanged. The exact case passed
  1/1, then the same eight files passed 221 with 2 skips, native 0 (pytest 24.79s,
  wall 28.141s), source hashes unchanged. Both skips are the independently passed
  native DAC cases above. Evidence: `observer-signature-fixture-final-20260905.*`
  and `observer-probe-full-final-20260905.*`. All 52 Python files passed fresh
  Ruff/format checks, and diff-check passed.
- **Complete local release-evidence regression:** all 272 collected cases in
  `test_classroom_release_evidence.py` reached native exit 0: 271 passed and one
  Windows-only platform skip (pytest 788.75s, wall 799.875s), no errors/failures
  or timeout, source hashes unchanged. Subsequent Linux execution first exposed
  a fixture's source-root docker.env dependency, then missing declared psutil.
  The fixture now uses an adjacent temporary deployment with an empty env file;
  transparent lease/reader checks retain the real boundaries. Only psutil 5.9.8
  was added to the validation venv; its original 30 packages are unchanged.
  The FIFO target subsequently passed as recorded below; the complete Windows
  file follow-up has now finished: 273 collected, 272 passed, one FIFO platform
  skip, native 0 (pytest 803.49s, wall 815.500s), no errors/failures/timeout,
  source hashes unchanged. The original wrapper and pytest processes are gone.
  The skipped FIFO case has separate native Linux evidence above. Follow-up:
  `.pytest_cache/release-evidence-fixture-full-final-20260905.*`; this closes the
  fixture regression, not the newly found integration defects. Original records:
  `.pytest_cache/release-evidence-full-20260905.{log,xml,json}`. This is not
  evidence for same-candidate live acceptance.
- **Earlier native Linux runtime regression; memfd closed below:** validation
  used a task-isolated CPython 3.11.16 venv on Ubuntu 22.04 WSL2 native ext4,
  not DrvFS or a container. The 915-file dirty-source snapshot was SHA-256
  verified; the final archive hash is
  `a9c5915a09b9b5285d7cd6cc0f5b4495f8cf7988fe736f03406a0ac717978f1a`.
  Initial exact tests exposed missing environment dependencies and unavailable
  `fcntl` sealing constants; those are not valid behavioral REDs. After loader
  dependencies were installed, the full runtime file plus POSIX interrupted
  directory-close case returned `123 passed, 6 skipped, 2 failed`, native 1
  (pytest 53.02s). Both actual symlink cases rejected access, but leaked native
  `ENOTDIR` instead of the candidate-validation error required by their contract.
  Component `openat` now converts only `ELOOP`/`ENOTDIR` to a static `ValueError`;
  no-follow flags, other I/O errors, cancellation and descriptor cleanup remain.
  Independent review approved that narrow change. The same complete Linux
  command then passed `125 passed, 6 skipped`, native 0 (pytest 50.78s, wall
  65.468s); all six skips are Windows-only. Fresh Windows runtime-file regression
  passed `105 passed, 25 skipped`, native 0 (pytest 85.02s, wall 89.740s), with
  no shared skipped runtime node IDs between platforms. Reports:
  `.pytest_cache/linux-runtime-affected-20260905.xml`,
  `.pytest_cache/linux-runtime-final-20260905.xml`, and
  `.pytest_cache/runtime-attestation-windows-final-20260905.xml`.
  Owned Linux pytest and matching local validation processes were zero afterward.
  This completes these runtime/file contracts, not Docker or live attestation.
  The standalone interpreter exposes `memfd_create` but lacks all six required
  `fcntl` sealing constants; the real-child memfd test was not verified at that
  stage. No constants or tests were patched to bypass it.
  The alternative Actions binary was not installed or executed: its
  [official usage restriction](https://github.com/actions/python-versions/blob/main/README.md)
  was identified, and the incomplete download failed TLS/hash verification.
  No system packages, Docker, server state, or previously published candidate
  were changed.
- **Second Linux runtime capability attempt did not close memfd:** one isolated
  conda-forge Python 3.11.16 installation completed native 0 in 52.018s, but its
  unmodified `-I` capability probe returned native 2: `os.memfd_create` and all
  six sealing constants were absent. No pytest/dependency installation or
  constant substitution followed. The 28-package provenance lock and terminal
  reports are under `E:\CodexTemp\yfeistai-linux-validation-20260905-uv01210`
  (`condaforge-outcome.md`, `condaforge-python-native-capabilities.json`,
  `condaforge-resolved-runtime-lock.json`). The official tool added only the
  task environment's one line to a previously absent user conda index; prior
  existence/bytes were backed up and the exact delta verified. Task environment
  and cache are retained, installer residuals are zero, Docker stayed stopped.
  This attempt supplied no real-child proof; the later result is recorded below.
- **Deadsnakes preparation stopped safely before extraction:** the Jammy 3.11.15
  PPA key fingerprint/signature, package index and first deb hashes verified,
  but the archive contains an absolute `sitecustomize.py` link to
  `/etc/python3.11/sitecustomize.py`. The isolated setup exited 1 (wall 18.978s),
  without extraction, link rewriting, interpreter/dependency install or pytest.
  Setup residuals were zero; 6.2 MB of task-local evidence remains. See
  `deadsnakes-outcome.md` in the task evidence directory above. This supplies no
  memfd capability evidence, and no production or system packages changed.
- **Isolated validation runtime is now ready:** a subsequent explicit archive
  policy omits only the exact authenticated, empty-site-configuration symlink;
  the packaged empty regular config remains unchanged. All four debs and every
  extracted regular-file hash verified. Python 3.11.15 plus the required wheels
  passed native memfd/sealing capability checks before and after dependency
  installation: all six constants, seal mask 15, and EPERM for write, growth,
  shrink and seal changes. No stdlib/constants were patched and no host site
  customization loaded. The initial uv option-conflict exit 2 is retained;
  corrected dependency installation exited 0. This is an isolated validation
  runtime, not a complete Debian installation or production 3.11.16 equivalence.
  See `deadsnakes-isolated-b72f6c90-outcome.md`.
- **Project-level native boundaries completed:** UID 1000, native ext4 and the
  fresh 915-source snapshot ran the real-child memfd lease, isolated deployment
  fixture and real FIFO rejection: 3/3, zero skips, native 0 (pytest 6.76s,
  wall 11.026s). Snapshot SHA-256:
  `a43b478790770eabe701b29a30fe82fcdb6e0dd16de1bc2b81129ebd3a60cde8`.
  Before/after source hashes and current repo comparison agree; owned residuals
  are zero. Task record `native-boundaries-final-20260905-01/`
  `memfd-fifo-fixture-final-result.json` identifies the original XML/log. The two
  preceding fixture/environment failures are retained, not counted as behavior
  RED. These local boundaries do not prove production-image or live acceptance.
- **Observer health authentication review and regression:** non-ASCII Bearer
  input reached `hmac.compare_digest(str, str)` and raised `TypeError`; five
  malformed-input cases reproduced this or an unnecessary secret read (native
  1). Reusing the existing ASCII/48–128-character token pattern before secret
  reads fixes that boundary without relaxing valid authentication. The five
  cases plus the existing valid/mismatched-token control passed 6/6 (native 0,
  pytest 3.21s). Eighteen further cases verify real temporary-file reads,
  accepted line endings, malformed content/path rejection, descriptor closure,
  and candidate-digest validation. The final complete health/active-probe/log
  regression passed `140/140`, zero skips/failures (native 0, pytest 21.31s,
  wall 26.433s); `.pytest_cache/teaching-health-affected-final-20260905.xml`.
  Scoped Ruff check and format check passed. Independent review approved the
  authentication change; owner/permission/link rejection still requires native
  platform coverage and is not implied by these Windows file tests.
- **Web candidate regression completed locally:** review found an immediate
  login-form count could reject a page before its Suspense form appeared. Three
  focused cases reproduced the missing wait/post-wait origin check (native 1,
  3 failed), then passed after adding a 10-second visible-form wait and a fresh
  HTTPS same-origin check before credential entry (native 0, 3 passed). Missing
  or ambiguous forms still fail closed. Nine complete affected Node test files
  passed `107/107`, zero skips/failures (native 0, Node 5.677s, wall 6.540s);
  full Web `tsc --noEmit --incremental false` passed (native 0, wall 84.573s).
  Independent review found no new scoped blocker. JUnit reports are
  `.pytest_cache/login-hydration-red-final-20260905.xml`,
  `.pytest_cache/login-hydration-green-20260905.xml`, and
  `.pytest_cache/web-candidate-affected-20260905.xml`. An initial invocation
  rejected an unsupported Node flag before testing (exit 9); it is not RED
  evidence. The request-observation test now accurately models unmatched
  requests waiting until timeout; this does not intercept network traffic.
  Live-browser design wording now matches the existing HTTPS-only policy.
  These are local regressions, not real browser or deployment acceptance.
- **Completed local verification:** downloaded promotion artifact contracts,
  including immutable source-commit templates and replacement-ref rejection:
  `39 passed, 85 deselected`, native exit 0 (pytest 42.93s). The two newly
  identified source-binding regressions each failed before their fix and passed
  together afterward (2/2).
- **In progress:** Task 6 scoped-diff/commit preparation and remaining platform
  verification. The downloaded validator consumes nine bounded ZIP members without
  extraction and renders Compose from the Actions source commit's Git blobs
  using `git --no-replace-objects`; current worktree templates are not its source
  of candidate identity.
- **Earlier affected verification (superseded below):** platform Compose, wrapper and preflight test files
  returned `214 passed, 1 skipped, 1 failed`, native exit 1 (pytest 78.29s).
  The sole failure is the source-checkout image-lock assertion: the checked-in
  historical lock is schema 1, not a generated schema 3 candidate. Six wrapper
  fixtures now use real temporary schema 3 candidates. Preflight rechecks its
  original five-file descriptor snapshot before runtime probes; the drift
  regression failed before the fix and passes in this affected run.
- **Publication-input handoff implemented and locally verified:** the new
  read-only `--verify-downloaded-promotion-artifact` CLI requires the raw ZIP,
  externally acquired Actions run/artifact metadata, and independently selected
  HEAD/tag/run/first-attempt/artifact identities. It reuses the strict validator;
  metadata reads are fixed-descriptor, non-symlink JSON objects bounded to 1 MiB.
  All 27 new CLI cases first failed for the absent action (native exit 1, pytest
  37.65s, wall 42.007s), then passed (native exit 0, pytest 20.99s, wall 24.581s),
  including two actual symlink rejection cases with no skips. Reports:
  `.pytest_cache/promotion-cli-red-20260905.xml` and
  `.pytest_cache/promotion-cli-green-20260905.xml`. Task 7.5 below documents the
  executable operational gate and its external API trust boundary; this local
  result does not prove real publication or authenticate arbitrary JSON.
- **Offline candidate input corrected:** the old source-checkout lock test
  reproduced its schema-1-versus-3 failure (native exit 1). It is now named
  `test_offline_rendered_candidate_digest_references_are_used_by_platform`,
  renders a real temporary candidate with synthetic digests, retains every
  strict schema/digest/base/signer assertion, and proves the five source files
  are unchanged. The initial corrected run exposed optional PocketBase being
  filtered from the Compose model; read-only `config` now includes its
  `legacy-local` profile solely to inspect that image reference. The exact case
  passed (native exit 0, pytest 7.08s). No service was started and no source lock
  was rewritten. The old failure is preserved as evidence, not skipped. Reports:
  `.pytest_cache/rendered-candidate-input-red-20260905.xml`,
  `.pytest_cache/rendered-candidate-input-green-20260905.xml` (intermediate
  PocketBase failure), and
  `.pytest_cache/rendered-candidate-input-green-final-20260905.xml`.
- **Complete four-file candidate regression:** the planned platform Compose,
  wrapper, preflight, and release-verifier command returned `455 passed,
  2 skipped`, native exit 0 (pytest 779.31s, wall 791.535s). JUnit records 457
  collected, zero failures/errors in
  `.pytest_cache/candidate-four-file-20260905.xml`; the owned pytest process
  count was 0 after completion. Skips are the Linux memfd real-child lease and
  POSIX interrupted-directory cleanup cases, not completed Linux validation.
  Independent spec and code-quality reviews approved the new downloaded CLI
  and corrected offline fixture, with no new scoped blocking findings.
- **Task 6 static checks:** the eight-file Ruff check passed. Format checking
  initially found only a dictionary-comprehension line wrap in
  `scripts/docker_compose.py`; that mechanical formatting change was followed
  by its complete test file (`57 passed, 1 skipped`, native exit 0, pytest
  15.37s; `.pytest_cache/wrapper-format-regression-20260905.xml`). The eight-file
  Ruff check, format check, and worktree `git diff --check` then all returned
  native exit 0. Git emitted existing LF/CRLF advisories for web files; no web
  file was changed in this turn. Full pending-diff integration and Linux-only
  verification remain separate from these local checks; no commit/tag/push or
  deployment was performed.
- **Full verifier verification completed locally:** the earlier full verifier run returned
  `171 passed, 42 failed, 1 skipped`, native exit 1 (pytest 801.66s, wall 816.075s).
  The shared gateway signing fixture omitted the base Compose file; it now binds
  all five files. Candidate-lease entry failures now preserve existing specific
  fail/missing diagnostics; actual exit-time drift still fails closed. Rerunning
  previously failed nodes yielded `41 passed, 173 deselected`, native exit 0
  (pytest 172.66s, wall 179.803s). The renamed strict-Compose rejection test and
  both candidate lease entry/exit rejection cases also passed in the subsequent
  gateway-affected run. A new complete verifier-file run now returned `213 passed,
  1 skipped`, native exit 0 (pytest 713.18s, wall 725.233s), recorded in
  `.pytest_cache/release-verifier-full-20260905.xml`. This closes the full-file
  local regression gap, not real release readiness. The one POSIX directory
  cleanup case remains unverified on this Windows host.
- **Gateway publication boundary:** a positive fixture passed while ten injected
  late same-byte/different-identity candidate replacements failed to be rejected.
  The producer now holds the five-file candidate lease across observation and
  publication, rechecks before publishing, and closes it inside the rollback
  scope before commitment. That exact group passed `11/11`, native exit 0
  (pytest 37.30s). Independent review found no blocking issue. The affected run
  returned `63 passed, 2 failed, 194 deselected`, native exit 1 (pytest 197.77s);
  the two failures were Windows fixtures whose attempted rename was blocked by
  the held candidate descriptor. Their five-case group now explicitly accepts
  either a blocked replacement with unchanged original bytes or a detected
  replacement, and passed `5/5`, native exit 0 (pytest 17.22s). A single combined
  affected rerun then passed `69 passed, 192 deselected`, native exit 0
  (pytest 209.24s, wall 216.246s), including the strict-Compose rejection,
  candidate lease entry/exit cases, gateway publication/rollback, and selected
  manifest publication/cleanup contracts. Its JUnit report is
  `.pytest_cache/gateway-manifest-affected-20260905-123424.xml`.
- **Manifest boundary verified locally:** ten late same-byte/different-identity
  replacements reached successful receipt validation/publication and were not
  rejected; the canonical positive case passed (`10 failed, 1 passed`, native
  exit 1, pytest 56.07s). Three additional valid REDs cover final lease-close
  failure, an exception after publication, and a late publish journal; concurrent
  foreign-output preservation passed (`3 failed, 1 passed`, native exit 1,
  pytest 18.02s). The implementation now validates all five retained original
  file bodies with the existing strict validators, keeps the original lease
  through publication, and rolls back only owned output on failure. The exact
  group passed `15/15`, native exit 0 (pytest 52.27s, wall 56.328s). The manifest
  affected run first returned `37 passed, 1 failed, 234 deselected`: the existing
  Windows directory-rename fixture was blocked by held candidate handles. It
  now explicitly proves either native blocked replacement with all original
  identities/bytes unchanged or a detected real replacement. The same affected
  group then passed `38 passed, 234 deselected`, native exit 0 (pytest 119.04s,
  wall 122.696s). Independent spec and quality reviews found no new blocking
  issue in this scope. The renderer's global
  directory-signature checks are unchanged. This Windows run does not prove
  Linux execution; the pre-existing POSIX cleanup stat/unlink race remains an
  explicit hardening limitation, not a completed guarantee.
  Reports: `.pytest_cache/manifest-candidate-red-20260905.xml` and
  `.pytest_cache/manifest-fault-red-20260905.xml` and
  `.pytest_cache/manifest-candidate-green-20260905.xml`; affected reports are
  `.pytest_cache/manifest-affected-20260905.xml` and
  `.pytest_cache/manifest-affected-green-20260905.xml`.
- **Runtime rollback fixture repaired:** the existing two-case publication
  regression initially failed before injection because its schema-3 fixture
  omitted PocketBase. Adding only the missing fixture image yielded `2 passed`,
  native exit 0 (pytest 2.86s); production checks and behavioral assertions were
  unchanged. The full runtime-attestation test file then returned `105 passed,
  25 skipped`, native exit 0 (pytest 76.01s, wall 80.693s), recorded in
  `.pytest_cache/runtime-attestation-affected-20260905.xml`. Skipped cases and
  this Windows result do not establish Linux host validation.
- **POSIX manifest descriptor ownership:** two local fake-POSIX cases reproduced
  duplicate close on both valid and mismatched bodies (native exit 1). Removing
  the duplicate ownership assignment yielded `2/2`, native exit 0 (pytest 1.86s),
  with the original mismatch error preserved; both also passed in the combined
  69-case rerun. This is not a Linux host run.
- **Not complete:** Tasks 7–8, successful publication of the final clean commit,
  downloaded real Actions artifact verification, and all same-candidate live
  acceptance evidence. Local tests do not establish any of those outcomes.

## Task 1: Add Candidate-Binding RED Contracts

**Files:**

- Modify: `tests/scripts/test_platform_compose.py`
- Modify: `tests/scripts/test_docker_compose.py`
- Modify: `tests/scripts/test_platform_preflight.py`
- Modify: `tests/scripts/test_verify_classroom_release.py`

1. Add a workflow contract proving every event except the first attempt of an
   exact tag push is rejected before the GHCR login step; the tag must match
   `yfeistai-first-release-YYYYMMDD-<HEAD8>` and its suffix must match the
   checkout commit.
2. Extend the workflow publication contract to stage all four images before a
   once-only digest promotion to one immutable candidate tag per image; reject
   every pre-existing final tag, every mutable compatibility tag, and every
   workflow rerun.
3. Add writer contracts requiring `sourceRepository`, `sourceHead`,
   `releaseTag`, `openmaicHead`, three matching non-zero workload digests, and
   one independent non-zero observer signer digest.
4. Parameterize drift contracts for source HEAD, release tag, and candidate
   image digests; every failure must preserve the lock and all four Compose files
   byte-for-byte.
5. Add wrapper and preflight contracts proving a legacy schema-version-1 lock
   is rejected before any subprocess invocation.
6. Add verifier contracts proving evidence cannot bind to a different source
   HEAD, release tag, OpenMAIC HEAD, or image digest.

Run only the new exact node IDs and confirm they fail for missing candidate
support rather than collection, fixture, Docker, or environment errors.

## Task 2: Implement Schema-Version-3 Atomic Rendering

**Files:**

- Modify: `scripts/render_platform_compose.py`

1. Add a small immutable candidate value model and strict validation helpers.
2. Require the writer to receive candidate source repository, source HEAD,
   release tag, and OpenMAIC HEAD.
3. Resolve all workload and observer signer images using the release tag, resolve
   all five fixed-tag base images from their remote manifests, reject
   zero/missing/colliding digests, and emit schema version 3 with strict
   `supportImages.observer_signer` validation.
4. Preserve the existing staged-write rollback behavior for the lock and all
   four Compose files.
5. Extend `load_image_lock()` with an explicit `require_candidate` boundary:
   historical callers may opt out, while production callers default to strict
   candidate validation.
6. Add CLI arguments for the candidate fields and fail before writes if any
   field is invalid.

Run the writer-focused new nodes, then the existing atomic writer and remote
manifest tests.

## Task 3: Fail Closed in Wrapper and Preflight

**Files:**

- Modify: `scripts/docker_compose.py`
- Modify: `scripts/platform_preflight.py`

1. Make the production platform wrapper require a candidate-bound lock before
   constructing or invoking any Docker Compose subprocess.
2. Make the data-plane wrapper enforce the same lock boundary where it uses
   custom platform images.
3. Make preflight report legacy, missing, or drifting candidate metadata as a
   blocking image-lock failure.
4. Keep offline contract checks network-free and preserve existing topology and
   environment sanitization behavior.

Run the new exact wrapper/preflight nodes, then the two affected script test
files.

## Task 4: Harden the Private Image Workflow

**Files:**

- Modify: `.github/workflows/private-platform-images.yml`
- Modify: `tests/scripts/test_platform_compose.py`

1. Add a first step that validates the exact release tag and checkout commit
   before Buildx setup or GHCR login.
2. Give every build only a unique run-scoped staging tag, stable step IDs, and
   the existing `linux/amd64`, pinned contexts, and pinned Dockerfiles.
3. After all four staged pushes, validate their build digests and call the
   writer with exact repository, source HEAD, release tag, pinned OpenMAIC HEAD,
   and explicit digests.
4. Validate the generated candidate lock against the workflow source values,
   the exact base Compose template, all five base-image digests, and all four
   build-step output digests. Reject every pre-existing final tag both before
   the builds and immediately before each promotion, then promote each digest
   once and remotely reverify it.
5. After remote verification, write a canonical schema-version-2 promotion
   receipt bound to the promotion intent, source candidate, every lock image
   digest plus the observer signer digest, exact GitHub run ID, first run
   attempt, and exact workflow ref. Invoke the strict local receipt verifier
   before artifact upload.
6. Do not create or promote compatibility aliases; upload the verified receipt,
   promotion intent, schema-version-3 lock, the exact base Compose file, and
   three rendered Compose files without any repository commit or push step.
7. Retain a single workflow concurrency group and least-privilege
   `contents: read`, `packages: write` permissions.

Run an offline YAML parse, the exact workflow contract node, and scoped
`git diff --check`.

## Task 5: Bind Release Verification Evidence

**Files:**

- Modify: `scripts/verify_classroom_release.py`
- Modify: `tests/scripts/test_verify_classroom_release.py`

1. Require receipt candidate identity to include `sourceHead`, `releaseTag`,
   `openmaicHead`, and the three image digests.
2. Compare all candidate fields against schema-version-3 image-lock metadata,
   the lock-derived observer signer, and the selected Compose references.
3. Report source/tag/digest drift as a blocking candidate-binding failure; do
   not convert a missing production receipt into synthetic success.
4. Preserve the existing eighteen evidence-layer requirements and honest
   `not_ready` result when evidence is absent.

Run focused binding nodes and the complete verifier test file.

## Task 6: Run Affected Verification and Commit

Run serially from the worktree:

For local Windows runs, set `DEEPTUTOR_HOME` and `TEMP`/`TMP` to task-owned isolated
runtime directories before pytest imports `tests/conftest.py`; keep its safety guards.
Pass a new, short, nonexistent `--basetemp` as explicit argv, or use forward slashes
in `PYTEST_ADDOPTS` (backslashes there are consumed by shlex), under `E:\CodexTemp`.
Never reuse an existing basetemp because pytest clears it. The
2026-09-06 four backup fixtures exceeded the Windows path limit with a long nested
temp root; shorter paths fixed those environment failures without production edits.

```powershell
python -m pytest tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py -q
python -m ruff check scripts/render_platform_compose.py scripts/docker_compose.py scripts/platform_preflight.py scripts/verify_classroom_release.py tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py
python -m ruff format --check scripts/render_platform_compose.py scripts/docker_compose.py scripts/platform_preflight.py scripts/verify_classroom_release.py tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py
git diff --check
```

Inspect the complete scoped diff. Confirm no branch/tag push, Docker operation,
image-lock publication, or main merge occurred. Commit only the candidate
binding implementation after all local checks are GREEN.

The eight-file static command above is the original minimum, not coverage for
the expanded pending candidate. Before committing, run Ruff check/format check
over every changed or newly added Python file and complete affected regressions
for observer/bootstrap, gateway/outage, capacity/export/release/smoke/tenant
probes, and the complete release-evidence file. Retain native exits and reports
bound to the tested source bytes. Independently review all pending implementation
and deployment changes; an earlier subset or an unbound historical report cannot
substitute for that review. Native Linux-only contracts need Linux evidence.

## Task 7: Publish One New Immutable Candidate

This task requires the user's standing tag/GHCR authorization and a healthy
runner. Never reuse or move an existing tag.

1. Confirm the worktree is clean and all required local commits are included.
2. Derive a new exact tag from the final HEAD and verify the name is absent
   locally and remotely.
3. Create and push only that tag; do not push a branch and do not merge main.
4. Observe the exact tag-triggered Actions run to native completion.
5. Download the artifact and verify its source HEAD, release tag, OpenMAIC HEAD,
   four promoted remote manifest digests, all five lock-resolved base-image
   digests, all four Compose references, and the promotion receipt against the
   out-of-band Actions run ID and first-attempt identity.

### Task 7.5: Read-Only Downloaded Artifact Gate

Obtain the raw, unmodified artifact ZIP and two independent JSON responses from
GitHub's authenticated API: the selected
[`GET /repos/{owner}/{repo}/actions/runs/{run_id}`](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run)
and
[`GET /repos/{owner}/{repo}/actions/artifacts/{artifact_id}`](https://docs.github.com/en/rest/actions/artifacts#get-an-artifact)
objects for `xinlingzhifei/DeepTutor`. An extracted directory or an artifact-list
response is not a substitute. Keep credentials out of these files and logs.

Select the expected full source HEAD, release tag, run ID, first attempt, and
artifact ID independently of the archive. In the clean verification checkout
containing that source commit's Git objects, run the following PowerShell command
with those selected values and the three local input paths:

```powershell
$promotionVerification = @(
    'scripts/render_platform_compose.py'
    '--verify-downloaded-promotion-artifact', $promotionZip
    '--actions-run-metadata', $actionsRunJson
    '--actions-artifact-metadata', $actionsArtifactJson
    '--source-head', $candidateHead
    '--release-tag', $candidateTag
    '--expected-github-run-id', $actionsRunId
    '--expected-github-run-attempt', '1'
    '--expected-github-artifact-id', $actionsArtifactId
)
python @promotionVerification
if ($LASTEXITCODE -ne 0) { throw 'Downloaded candidate verification failed' }
```

Each metadata file is a regular, non-symlink JSON object bounded to 1 MiB. The
CLI binds it to the independently selected identities before ZIP/Git reads,
then uses the strict nine-member downloaded-artifact validator. Success prints
only the validated promotion receipt JSON and returns native exit 0; missing,
failed, mismatched, or tampered input stays blocking. The CLI does not extract,
build, publish, rewrite the lock, or perform network requests. Git reads use
the selected commit, not dirty working-tree templates or replacement refs.

This offline command does not authenticate arbitrarily supplied JSON: trusted
API acquisition is an explicit external boundary. Synthetic unit-test metadata
and digests cannot satisfy this operational gate. Preserve the real API records,
raw ZIP, selected identities, final exit, and validated receipt as publication
evidence; only then continue with that exact candidate's deployment acceptance.

If the run fails, retain the immutable failed tag as historical evidence, fix
on a new commit, and create a new tag. Never repoint the failed tag.

GHCR does not provide an atomic four-image transaction or a conditional
"create tag only when absent" primitive for this workflow. Therefore the
repository and package write permissions are an external protected
single-writer trust boundary. A partial publication is never retried under the
same candidate tag: it remains failed historical evidence, and the next attempt
uses a new source commit and a new release tag. Deployments consume only the
digest-pinned artifact from a fully successful run.

## Task 8: Continue Plan07 Acceptance on the Same Candidate

Use only the successful candidate artifact for Plan07 Tasks 2-8 and the twelve
named acceptance suites. Do not mix evidence from another commit, tag, image,
or Compose set. Only after every required layer is GREEN may the branch be
safely merged into local `main` and reverified there.
