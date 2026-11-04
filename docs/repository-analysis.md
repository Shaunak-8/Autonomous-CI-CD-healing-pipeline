# Adaptive repository analysis

The backend inventories source files before running checks. Classification uses
both file count and source bytes; a repository must fit both limits for a tier.
These are configurable operating limits, not universal definitions of repo size.

| Tier | Source files | Source bytes | Timeout per command | Fix attempts per iteration |
| --- | ---: | ---: | ---: | ---: |
| Small | up to 500 | up to 5 MiB | 120 seconds | 20 |
| Medium | up to 5,000 | up to 50 MiB | 600 seconds | 10 |
| Large | beyond either medium limit | | Checks marked incomplete | 0 |

Dependencies, generated output, caches and virtual environments are excluded.
Symlinks are not followed. Oversized source files are counted for classification
but excluded from code context, graph parsing and white-box analysis, and are
listed as skipped. Skips and inventory read errors prevent a complete result.
Standard directory exclusions are implemented in `backend/core/repository.py`;
arbitrary project `.gitignore` rules are not currently interpreted.

Python package roots are discovered through pyproject.toml, pytest.ini, setup.py
and requirements.txt. They run Flake8, Mypy and Pytest using the backend's Python
interpreter. JavaScript/TypeScript package roots use their package.json lint,
test and optional build scripts with CI=true. Packages must have their dependencies
installed. Background agent runs now prepare the clone before checking it: Python
dependencies and validation tools go into a workspace virtual environment, while
JavaScript packages use npm ci when a lockfile is available. Install failures are
reported as run failures with diagnostic output. The direct analyzer and benchmark
do not install packages automatically. Install commands default to a 600-second
timeout; a first run with large packages such as PyTorch can take several minutes.
AUTO_INSTALL_DEPENDENCIES=false disables preparation for preconfigured workspaces.
C/C++ retains basic g++ syntax checks but is incomplete without project build/test
configuration. Other languages are inventoried but reported as incomplete until a
runner is configured. Workspaces are discovered from nested manifests; custom monorepo
task orchestration and project-specific build flags may still require adapters.

Every command reports passed, failed or incomplete. Missing tools, missing test
scripts, no collected tests, timeouts and truncated command output cannot produce
a pass. A tool can fail without a recognized source finding; its failed outcome
still blocks success. Checks capture at most 2 MiB per output stream into memory.
Commands have a timeout and their process tree is terminated on timeout.

All configured checks run over each discovered package. Automatic fixes prioritize
syntax, dependency roots and severity, then attempt a bounded batch. The remaining
work is reported as deferred and revisited on later iterations up to MAX_RETRIES.
Full package checks run after each batch. This is bounded batch fixing with full
validation; affected-test selection and persistent analysis caches are future work.

Context uses whole lines around a failing location, related imports and a nearby
test within a byte budget. Token counts are estimates (UTF-8 bytes / 3), not a
provider tokenizer; leave room for instructions, error messages and model output.
The LLM returns the displayed excerpt. Replacement preserves all source outside
that excerpt, retains line endings and rejects concurrent source changes. Python
replacements are compiled before writing. Provider output truncation is rejected
for Groq and Gemini. LLM calls and code execution still depend on your configured model and
installed tools; a benchmark does not establish automatic fix quality.

Push happens only after all configured checks pass, the analysis is complete and
there are applied fixes. Failed and incomplete checks are shown in the dashboard.
The result includes repository_profile, analysis, push and deferred_failures.
White-box analysis in automatic runs performs static branch discovery without
running another coverage suite or generating additional tests. It explicitly
reports that coverage was not measured.

## Configuration

Optional overrides in backend/.env (defaults shown):

```dotenv
SMALL_REPO_MAX_FILES=500
SMALL_REPO_MAX_BYTES=5242880
MEDIUM_REPO_MAX_FILES=5000
MEDIUM_REPO_MAX_BYTES=52428800
SMALL_CHECK_TIMEOUT=120
MEDIUM_CHECK_TIMEOUT=600
SMALL_FIX_BATCH_SIZE=20
MEDIUM_FIX_BATCH_SIZE=10
MAX_SOURCE_FILE_BYTES=1048576
LLM_CONTEXT_TOKENS=4000
AUTO_INSTALL_DEPENDENCIES=true
DEPENDENCY_INSTALL_TIMEOUT=600
```

MAX_RETRIES limits the number of analysis/fix/validation iterations. The default
of five allows up to 50 fix attempts for a medium repository; unfinished findings
remain visible and are never treated as a pass.

## Local validation

From backend, using the Python interpreter with requirements installed:

```powershell
py -3.14 -m pytest test_repository_scaling.py -q
py -3.14 benchmark_repository.py --files 1200 --run-checks
py -3.14 benchmark_repository.py --files 1200 --run-checks --inject-error
py -3.14 -X utf8 scripts/smoke_pipeline.py --live --prepare
```

The benchmark creates and removes an isolated temporary repository. It measures
inventory, graph, static branch analysis and context generation, then optionally
runs real Flake8, Mypy and Pytest checks. Memory measures the parent Python process
only. It does not call an LLM, clone repositories, push branches or access a database.

Observed local results on the development Windows machine for the synthetic
1,200-file fixture (approximately 97,000 lines and 1.3 MiB of source):

| Fixture | Result | Elapsed time |
| --- | --- | ---: |
| Clean | Flake8, Mypy and Pytest passed | 32.1 seconds |
| Injected undefined name | Flake8 and Mypy failed; overall failed despite passing Pytest | 33.7 seconds |

Graph, inventory, context and static branch analysis peaked at approximately
6.3 MiB of traced parent Python allocations. Tool subprocess memory is excluded.
These fixtures exercise scan volume and error propagation. They contain one
Pytest test and do not represent a complex project's test suite or prove LLM fix
accuracy. Real repository timing depends on dependencies and test workloads.

The live smoke test separately exercises Git cloning, environment creation,
provider model readiness, a real LLM repair, Flake8/Mypy/Pytest revalidation and
database result persistence against a temporary SQLite database. It uses the
configured Groq key but disables publishing, so it never pushes a remote branch.

Each model is checked against the provider's live model list before generating
fixes. Invalid/expired keys or unavailable models produce an actionable dashboard
message and stop repeated fix attempts. Fixes are batched by source file, unchanged
model output is not counted as a fix, and assertion failures are mapped to their
single imported source module when that mapping is unambiguous. The agent does
not automatically rewrite failing assertion tests to make them pass.

SSE events have monotonic IDs and a bounded replay buffer per run. Multiple
dashboard clients receive independent copies. Results are also stored in the
existing runs.memory JSON column, so polling can recover terminal state after a
disconnect or server restart without changing the database schema.

Gemini can be configured as an additional provider with GEMINI_API_KEY and
GEMINI_MODEL. LLM_PRIMARY_PROVIDER=groq keeps Groq as the default; set it to gemini
to prefer Gemini. The fixer switches to the alternate provider when the primary
cannot generate a completion, including Groq rate limits. Both providers use the
same exact-edit validation, test checks, regression rollback and publication gate.
Gemini credentials are sent in HTTP headers, never query strings or frontend
configuration. Child validation processes do not inherit either provider's key.

Run startup events identify configured providers and the provider selected for
generation. `/health` exposes model names, configuration flags and a pipeline
revision without exposing API keys. Gemini temporary failures receive at most
three attempts with short delays. When all configured providers fail, the run
reports each provider's status and stops generation; edits already applied still
undergo validation and regression rollback, and pending findings remain visible.

To exercise live Gemini fallback with an injected Groq quota error:

```powershell
py -3.14 -X utf8 scripts/smoke_pipeline.py --live --primary groq --force-groq-rate-limit
```

This command uses real Gemini generation on an isolated temporary fixture and
never pushes a remote branch.

Normal application runs publish validated source fixes automatically to a unique
new branch in the connected repository. The connected GitHub account/token must
have write access to repository contents. The agent creates the branch before
committing, retries the same branch on network errors, and verifies its remote
commit SHA before reporting a successful push. Existing branches are not reused,
and pushes do not use force. The result includes the branch/commit links; the
dashboard shows push failures separately from code validation results. Generated
dependency environments and other untracked files are excluded from commits.

Dependency installation emits recent installer output and elapsed time every 15
seconds, with a completion event after each step. Python setup first tries wheels
in `backend/results/dependency-wheels` (override with `DEPENDENCY_WHEELHOUSE`)
without contacting a package index. If required packages are missing, it falls
back to the configured index with bounded retries; network errors are reported
as download failures rather than inferred dependency-version incompatibilities.
JavaScript installation prefers the existing npm download cache.

Recover previously downloaded Python wheels on the current machine with:

```powershell
py -3.14 scripts/seed_dependency_cache.py
```

This reads pip's local cache and does not download packages. The optional
`--installed-site-packages PATH --distribution NAME` option can recover a pure
Python wheel from an existing installed distribution after checking its recorded
file hashes. Cached packages must still satisfy the repository's requirements;
setup continues to use separate virtual environments for each workspace.

Generated fixes cannot add lint/type suppression directives such as `type: ignore`,
`noqa`, `ts-ignore` or `eslint-disable`. Retry records include the current source
fingerprint, allowing a surviving finding to be revisited after a partial repair
changes that file while avoiding repeated requests against unchanged source.
