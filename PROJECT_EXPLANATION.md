# Autonomous CI/CD Healing Agent — Project Explanation

**Team:** Hog Riders  
**Subject:** Cloud Computing  
**Project:** Autonomous CI/CD Healing Agent

## 1. Project in one sentence

The Autonomous CI/CD Healing Agent is a cloud-enabled DevOps platform that accepts a GitHub repository, detects CI/CD failures, analyzes their probable root cause, generates targeted AI-assisted fixes, validates them with real checks, and can publish verified changes to a separate Git branch.

The key idea is **verification-first self-healing CI/CD**: the system does not treat an AI response as a successful fix until automated checks have passed.

## 2. Problem statement

Modern teams use Continuous Integration and Continuous Delivery/Deployment (CI/CD) to build, test, and deploy software automatically. When a pipeline fails, a developer normally has to read the logs, locate the faulty source file, create a fix, rerun tests, and push the change.

This project automates much of that repetitive workflow. It reduces the time needed for initial debugging while retaining safety controls such as test validation and safe Git branches.

## 3. Objectives

- Detect test, linting, type-checking, syntax, and build failures in repositories.
- Convert raw tool output into structured findings.
- Use a multi-agent workflow to analyze, fix, validate, and report the issue.
- Stream progress live to a browser dashboard.
- Persist users, projects, runs, issues, iterations, and fixes in a database.
- Push only verified fixes to a new branch, protecting the main branch.

## 4. High-level architecture

```text
React/Vite Dashboard
       |
       | REST API + Server-Sent Events (SSE)
       v
FastAPI Backend
       |
       +-- Supabase: authentication and JWT sessions
       +-- Neon Postgres: managed cloud database
       +-- GitHub: repository clone, branch, commit, and push
       +-- Groq / Gemini: cloud LLM APIs for analysis and code fixes
       +-- LangGraph: multi-agent workflow orchestration
       +-- Repository check tools: pytest, flake8, mypy, npm, g++
```

The frontend, API, data layer, source-control integration, and AI services are separate components. This is a multi-tier architecture that is suitable for independent cloud deployment and scaling.

## 5. Technology stack

| Layer | Technologies | Purpose |
|---|---|---|
| Frontend | React 18, Vite, React Router | Browser dashboard |
| Frontend state/UI | Zustand, Axios, Recharts, React Flow, Dagre | State, API calls, charts, dependency graphs |
| Backend | Python, FastAPI, Uvicorn | REST API and background workflow host |
| Agent workflow | LangGraph | Controlled multi-agent state machine |
| Database | Neon PostgreSQL, SQLAlchemy | Managed persistent storage and ORM |
| Authentication | Supabase, JWT | User identity and protected requests |
| AI services | Groq and Google Gemini | Root-cause reasoning and fix generation |
| DevOps/source control | GitPython, GitHub API | Clone repositories and publish branches |
| Validation | pytest, flake8, mypy, npm, ESLint, g++ | Test, lint, type, build, and syntax checks |
| Live updates | Server-Sent Events | Server-to-browser progress stream |

> Note: The current implementation uses **React with Vite**. An older README reference to Next.js does not describe the present frontend implementation.

## 6. Complete execution flow

When a user starts an agent run, the following steps take place:

1. The user signs in through Supabase.
2. The frontend submits repository information to `POST /api/run`.
3. FastAPI verifies the JWT, creates a unique run ID, and stores a run record.
4. The backend opens an SSE event queue for that run.
5. A background task clones the GitHub repository into `workspaces/run_<run_id>`.
6. The repository is inventoried to identify its files, languages, manifests, and size.
7. The Analyzer Agent runs applicable checks and turns their output into structured failures.
8. A dependency graph is created to prioritize likely root-cause files.
9. The Fixer Agent asks an available LLM provider for a focused code change.
10. The Validator Agent reruns the required checks/tests.
11. If checks fail, the workflow retries within configured limits; if they pass, it prepares a success result.
12. The Reporter Agent stores and streams the final report.
13. When publishing succeeds, the system creates a separate Git branch, commits the verified fix, and pushes it to GitHub.

```text
START
  |
  v
Clone repository --> Inspect repository --> Run checks/tests
                                                |
                                                v
                                      Failures found?
                                      /             \
                                   no                 yes
                                   |                   |
                                   v                   v
                                Report       Analyze root cause
                                                       |
                                                       v
                                                Generate fix
                                                       |
                                                       v
                                                Validate fix
                                                 /          \
                                              pass          fail
                                               |             |
                                               v             v
                                    Commit/push new      Retry analysis
                                      branch, report      within limits
```

## 7. Multi-agent design

The backend uses a LangGraph workflow rather than a single unrestricted AI prompt. Each agent has a clear responsibility.

| Agent | Responsibility |
|---|---|
| Analyzer Agent | Builds repository inventory, runs checks, deduplicates failures, and performs white-box analysis where possible. |
| Fixer Agent | Selects an available LLM provider and generates/apply focused fixes. |
| Validator Agent | Re-runs checks and determines whether a proposed fix actually works. |
| Reporter Agent | Produces final run information, score, and user-visible summary. |
| Memory Agent | Keeps run-related information for reporting and repeated reasoning. |
| Workspace Agent | Generates workspace plans, root-cause analysis, and infrastructure suggestions. |
| Review Agent | Handles GitHub push webhook review signals and records detected issues. |

Separating these responsibilities makes the workflow easier to test, audit, and maintain. In particular, the component that proposes a change is not the only component deciding whether the change is correct.

## 8. Repository analysis and validation

The system uses a `RepositoryInventory` to classify a repository before execution. It detects source files, manifests, language types, repository size, and policy limits.

It can run the following checks when appropriate:

| Repository type | Checks |
|---|---|
| Python | `flake8`, `mypy`, `pytest` |
| JavaScript | `npm run lint`, `npm run test`, `npm run build` |
| C++ | `g++ -fsyntax-only` |

Raw output is parsed into findings containing details such as the file, line number, bug type, message, and severity. Typical categories are syntax errors, logic/test failures, type errors, linting issues, and JavaScript issues.

For test failures, the project also attempts to map a failing test to imported source modules, so the model receives a likely source target instead of only the test filename.

## 9. Safety controls

The project does not blindly overwrite a repository.

- Generated fixes are validated by actual checks and tests.
- Fixes are written atomically: a temporary file is written and then moved into place.
- Repository file paths are checked to avoid path traversal outside the cloned repository.
- Check subprocesses remove sensitive credentials from their environment.
- Tool execution has timeouts and captured-output limits.
- A separate Git branch is used rather than directly modifying `main`.
- Authentication is required for protected operations.
- Project ownership is checked before a user can run a project.

The E2B sandbox layer is prepared for cloud sandbox execution. At present, its implementation falls back to local validation when a remote sandbox is unavailable; therefore it should not be presented as complete remote isolation yet.

## 10. Database design

The project uses SQLAlchemy models with Neon PostgreSQL as the intended managed cloud database. For local development, the backend can fall back to SQLite if the configured database cannot be reached.

```text
User
  |
  +-- Project
        |
        +-- Run
        |     |
        |     +-- Iteration
        |     |     |
        |     |     +-- Fix
        |     |
        |     +-- stored run memory/result
        |
        +-- Issue
        +-- Task
```

| Table | Stored information |
|---|---|
| `users` | Supabase user ID, email, profile details |
| `projects` | Repository URL, project name, owner, tags, visibility |
| `runs` | One agent execution, status, scores, failures, fixes, result memory |
| `iterations` | Each analysis/fix/validation cycle in a run |
| `fixes` | File, bug type, line, confidence, status, commit message |
| `issues` | Review issues for a project |
| `tasks` | Workspace tasks and their states |

## 11. Real-time dashboard communication

The frontend listens to:

```text
GET /stream/{run_id}
```

using Server-Sent Events (SSE). The backend sends events such as repository cloned, analysis started, failure detected, graph updated, provider selected, fix applied, and run completed.

SSE is well suited because the main communication direction is server to browser. It avoids repeated polling and lets users observe long-running CI repair work in real time.

## 12. Main API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/me` | Registers/ensures the authenticated user exists locally |
| `GET /api/repos` | Retrieves available GitHub repositories |
| `POST /api/projects` | Creates a project |
| `GET /api/projects` | Lists a user’s projects |
| `POST /api/run` | Creates and starts an autonomous agent run |
| `GET /api/status/{run_id}` | Returns run status |
| `GET /api/results/{run_id}` | Returns final run result |
| `GET /stream/{run_id}` | Streams live run events |
| `POST /api/workspace/chat` | Generates roadmap tasks |
| `POST /api/workspace/rca` | Generates root-cause analysis |
| `POST /api/workspace/infra` | Generates infrastructure suggestions |
| `POST /api/webhooks/github` | Receives GitHub push webhook events |

## 13. Frontend features

The React dashboard includes:

- authentication and session restoration;
- project and repository management;
- dashboard and run history;
- live agent thought/progress stream;
- dependency/causality graph visualization;
- repository health score and score charts;
- code-diff and applied-fix views;
- CI/CD timeline;
- root-cause and infrastructure workspace views;
- task management;
- issue heat-map/statistics view.

## 14. Cloud Computing relevance

This project demonstrates a practical cloud application rather than only a local program.

1. **Three-tier architecture:** the browser frontend, API backend, and managed data layer are independently deployable.
2. **Managed database:** Neon Postgres provides persistent cloud storage that survives local process restarts and supports multiple users.
3. **Identity as a service:** Supabase provides cloud-managed authentication and JWT identity.
4. **AI as a service:** Groq and Gemini are external cloud AI providers; the application selects available configured providers.
5. **DevOps as a service:** GitHub provides remote source control, branching, collaboration, and CI/CD integration.
6. **Real-time cloud communication:** SSE streams progress from backend workers to user browsers.
7. **Scalability potential:** frontend, API workers, database, AI calls, and sandbox workers can be scaled independently.

## 15. Cloud Computing viva guide

This section is deliberately written for a Cloud Computing review. The most important rule is to distinguish what the current project **implements** from what is the recommended **production deployment design**.

### 15.1 Cloud service models used

| Cloud model | Use in this project | Explanation |
|---|---|---|
| SaaS (Software as a Service) | GitHub, Supabase, Neon dashboard, Groq, Gemini | The team consumes ready-to-use cloud applications/APIs instead of operating their underlying infrastructure. |
| PaaS (Platform as a Service) | Neon Postgres, Supabase, a possible FastAPI hosting service | These services provide managed runtime/database capabilities while hiding server management. |
| IaaS (Infrastructure as a Service) | Possible VM/container host for the backend | If self-hosted, the API and worker processes can run on cloud VMs or container infrastructure. |
| FaaS / serverless (future option) | Event-triggered or short API operations | Some endpoints, webhooks, and cleanup jobs could be deployed as functions, but the long-running repair workflow is better handled by workers. |

### 15.2 Recommended production deployment

```text
Users
  |
  v
CDN / HTTPS / Load Balancer
  |                         \
  v                          v
Static React frontend       FastAPI API replicas
                                   |
                                   +--> Redis / message queue
                                   |          |
                                   |          v
                                   |     Scalable repair workers
                                   |          |
                                   |          +--> isolated sandbox jobs
                                   |
                                   +--> Neon PostgreSQL
                                   +--> Supabase Auth
                                   +--> GitHub / Groq / Gemini APIs
```

The current local project runs the FastAPI application and background task in one process for simplicity. In production, the API should submit repair jobs to a durable queue, while separate workers perform cloning, analysis, test execution, and AI calls.

### 15.3 Scalability

**Horizontal scaling** means adding more API or worker instances rather than only increasing one machine's CPU/RAM. This project supports that design because the frontend, API, database, and AI calls are separate concerns.

- The React frontend can be served from object storage/CDN and scaled almost automatically.
- Multiple stateless FastAPI API instances can sit behind a load balancer.
- Long-running repair jobs should be processed by independent worker replicas.
- Neon provides managed database scaling and persistent shared state.
- A queue prevents sudden bursts of GitHub repositories from overwhelming the service.
- Repository size limits, command timeouts, and output limits prevent one job from consuming unlimited resources.

**Important answer:** The present code uses FastAPI `BackgroundTasks`, which is appropriate for a prototype but is not a durable distributed job queue. Celery with Redis, RabbitMQ, cloud queues, or Kubernetes Jobs would be stronger production choices.

### 15.4 Availability and fault tolerance

Availability means the service remains usable even when part of the system fails. Fault tolerance means it handles errors without producing unsafe or misleading results.

Current fault-tolerance features include:

- database fallback to SQLite for local development if the configured database cannot be reached;
- multiple configured API-key slots for LLM providers;
- Groq/Gemini provider selection and graceful reporting when AI is unavailable;
- retry loops for failed validation;
- bounded command execution through timeouts;
- safe separate Git branches rather than direct main-branch changes;
- SSE event history/replay support for reconnecting dashboard clients.

For a production system, high availability would additionally require multi-instance API deployment, a managed durable queue, database backups/point-in-time recovery, health checks, autoscaling, and multi-zone deployment where supported.

### 15.5 Resource isolation and virtualization

The project executes code from repositories, so isolation is important. The application has an E2B sandbox abstraction and local fallback. In a production cloud environment, each repository job should run inside an isolated container, microVM, or managed sandbox with:

- CPU, memory, disk, and execution-time limits;
- a temporary filesystem;
- no access to production secrets;
- restricted or disabled outbound network access where possible;
- automatic cleanup after the job finishes.

Containers package the backend and dependencies consistently; Kubernetes, Docker Swarm, or managed container platforms can schedule multiple isolated workers. Do not claim that the current local fallback is complete cloud isolation—it is an area for improvement.

### 15.6 Security in the cloud

| Security area | Current approach | Production improvement |
|---|---|---|
| Identity | Supabase JWT authentication | Enforce MFA/organization controls if required |
| Authorization | Project ownership checks on protected project/run operations | Apply ownership checks consistently to every resource endpoint |
| Secrets | API keys come from environment configuration; validation subprocesses remove sensitive variables | Use a cloud secret manager and rotate credentials |
| Git changes | Uses a separate branch before merge | Require PR review, protected branches, and least-privilege GitHub tokens |
| Untrusted repository code | Timeouts, output limits, path checks, sandbox abstraction | Use isolated remote sandboxes and restricted network policies |
| Network | REST/SSE API design | Enforce HTTPS, restrict CORS, add WAF/rate limiting |
| Webhooks | GitHub webhook endpoint exists | Verify GitHub HMAC signatures before processing payloads |

### 15.7 Data management

Neon Postgres is used for durable relational data such as users, projects, runs, iterations, fixes, issues, and tasks. A relational database fits because these records have clear relationships, require querying, and need transactional consistency.

Database best practices for production are:

- use encrypted TLS connections;
- take managed backups and enable point-in-time recovery when available;
- use connection pooling;
- restrict database credentials by environment and role;
- avoid saving raw access tokens, secrets, or unredacted repository output;
- create indexes for commonly queried fields such as run ID, project ID, and user ID.

### 15.8 Observability and monitoring

Observability lets operators understand the health and behavior of cloud services.

The application already exposes a `/health` endpoint and creates structured run records and SSE progress events. A production deployment should additionally collect:

- **metrics:** request latency, error rate, queue depth, worker utilization, test duration, repair success rate;
- **logs:** request IDs/run IDs, failures, audit events, without exposing secrets;
- **traces:** one trace across API request, queue task, worker, LLM call, database access, and GitHub operation;
- **alerts:** API unhealthy, database unreachable, high failure rate, queue backlog, or unexpected cloud cost.

### 15.9 Cost optimization

The main variable costs are LLM calls, sandbox/worker compute time, database usage, and network/storage usage.

The project already uses targeted context, batching, repository size classification, output limits, and retries with caps. These reduce unnecessary tokens and compute. Other cost controls include caching dependency installations, setting worker concurrency limits, auto-cleaning workspaces, using budget alerts, and avoiding repeated tests where safe.

### 15.10 CI/CD and Infrastructure as Code

This project automates CI repair; its own deployment should also use CI/CD. A production pipeline could:

1. run frontend and backend tests;
2. build versioned container images;
3. scan dependencies and images;
4. deploy first to staging;
5. run smoke tests;
6. promote to production using rolling, blue-green, or canary deployment;
7. roll back automatically if health checks fail.

Infrastructure as Code tools such as Terraform, Pulumi, AWS CloudFormation, or Kubernetes manifests would make cloud resources reproducible, version-controlled, and auditable.

### 15.11 Stateless vs stateful components

| Component | Classification | Reason |
|---|---|---|
| React frontend | Stateless | Can be served by any CDN/static host instance. |
| FastAPI API | Ideally stateless | User/run state should live in the database/queue, allowing replicas behind a load balancer. |
| Repair worker | Stateless per job | Each job can be retried or moved to another worker if its state is persisted externally. |
| SSE connection | Connection state | Requires reconnect/replay or a shared event broker when API replicas are used. |
| Neon Postgres | Stateful | It stores durable system data. |
| Repository workspace | Temporary state | It should exist only for the duration of a repair job and then be cleaned up. |

### 15.12 Cloud Computing questions with strong answers

**Why is this a cloud application and not just a web application?**  
It consumes managed cloud services for identity, database, AI inference, and Git repository operations. Its frontend, API, worker, and database layers can be deployed independently and scaled horizontally.

**Which cloud deployment model would you choose?**  
For a college/medium-scale application, a managed PaaS/container platform for FastAPI workers, Neon for Postgres, Supabase for auth, and static hosting/CDN for React. For enterprise scale, Kubernetes or managed container orchestration plus a durable queue would provide stronger worker scaling and isolation.

**How will load balancing work?**  
An HTTPS load balancer routes requests across stateless FastAPI replicas. Long-running repair work is not performed by the load balancer; it is submitted to a queue and consumed by worker replicas.

**How do you handle a worker failure in the middle of a run?**  
Persist the run state in Postgres, use an acknowledged durable queue, set a lease/visibility timeout, and retry the job on another worker. Jobs must be idempotent so retrying does not create duplicate branches or commits.

**Why not execute all work in a serverless function?**  
Repository clone, dependency installation, test execution, and LLM retry loops can take longer than typical serverless limits and need filesystem isolation. A container worker or sandbox is a better fit; serverless functions are useful for lightweight APIs, webhooks, and cleanup tasks.

**How do you secure API keys in cloud deployment?**  
Never hard-code them or send them to the browser. Store them in a secret manager, inject them only into authorized backend workloads, rotate them, use least privilege, and redact them from logs/subprocess environments.

**How does autoscaling help?**  
API replicas can scale with incoming dashboard/API requests, while worker replicas scale with queue depth. This prevents long repair jobs from blocking interactive user requests.

**What is the difference between vertical and horizontal scaling here?**  
Vertical scaling gives one worker more CPU/RAM for a large repository. Horizontal scaling starts more API/worker replicas so several repositories can be analyzed in parallel.

**How would you provide disaster recovery?**  
Use managed database backups and point-in-time recovery, versioned infrastructure configuration, container-image registry, multi-zone deployment where possible, and tested restore procedures.

**What is eventual consistency in this project?**  
The database may show a run as started before all live dashboard events arrive. SSE events provide near-real-time progress, while the persisted final Run record remains the authoritative completed result.

**What is the biggest cloud risk in this application?**  
Executing untrusted repository code and uncontrolled resource consumption. The answer is isolated sandbox workers, strict resource limits, secret isolation, network restrictions, monitoring, and human approval before merge.

## 16. Current implementation status

The local project was verified with:

- FastAPI health endpoint responding successfully on port `8001`.
- React/Vite dashboard running on port `5173`.
- 118 backend tests passing.
- 15 frontend tests passing.

## 17. Limitations and future improvements

The following are appropriate, honest future enhancements:

- Use a durable distributed job queue such as Celery/Redis, cloud tasks, or workers instead of in-process FastAPI background tasks.
- Complete remote E2B sandbox execution for stronger isolation of untrusted repositories.
- Add automatic workspace cleanup after each run.
- Validate GitHub webhook signatures with HMAC.
- Restrict CORS to production frontend domains instead of allowing all origins.
- Create pull requests automatically rather than only pushing a branch.
- Add rollback, human approval, and organization policies before merge.
- Add security-vulnerability scans and more language support.
- Improve handling of very large repositories through distributed workers and caching.

## 18. General project / viva answers

### What is CI/CD?

Continuous Integration automatically builds and tests code changes frequently. Continuous Delivery/Deployment automatically prepares or releases verified changes.

### Why use multiple agents?

Each agent has a narrow responsibility: detection, repair, validation, reporting, and memory. This creates a more controlled and testable pipeline than a single model prompt.

### How do you know the AI-generated fix is correct?

We do not trust the LLM response alone. The Validator reruns relevant checks and tests. A fix is only considered successful when validation passes.

### Why create a new branch?

It protects the original branch. A developer can inspect the generated fix, run additional CI checks, and merge only after approval.

### Why use SSE rather than polling?

SSE sends progress as soon as it occurs, reduces repeated HTTP requests, and is simpler than WebSockets for one-way status updates.

### Why use a managed cloud database?

It provides persistent multi-user data storage independent of the API server. Project history, run results, fixes, and tasks remain available after server restarts or redeployments.

### What happens when an LLM is unavailable?

The application can still analyze and report failures. It marks fixes as unavailable rather than falsely reporting a repair.

### Can this replace developers?

No. It accelerates diagnosis and repetitive repair work. Complex architecture, security, business logic, and final merge approval still require engineers.

## 19. Suggested review introduction

> Our project is an AI-powered, cloud-enabled self-healing CI/CD platform. A user selects a GitHub repository from a React dashboard. The FastAPI backend clones and analyzes the repository, runs checks, identifies root causes, generates targeted fixes with cloud LLM services, and validates every fix using actual tests. All activity is streamed live to the dashboard and stored in Neon Postgres. Verified fixes can be committed and pushed to a separate Git branch, keeping the original branch safe.

