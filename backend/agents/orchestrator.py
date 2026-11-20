from langgraph.graph import StateGraph, START, END
from typing import TypedDict, List, Optional
import os
import json
import time
import re
import logging
import hashlib
from groq import Groq
import config


from agents.analyzer import AnalyzerAgent
from agents.fixer import FixerRouter
from agents.validator import ValidatorAgent
from agents.reporter import ReporterAgent
from agents.memory import MemoryAgent
from core.dependency_graph import DependencyGraph
from core.failure_ledger import FailureLedger
from core.confidence import calculate as calc_confidence
from core.context_builder import ContextBuilder
from core.repository import safe_path, RepositoryInventory
from core.llm_status import check_groq
from core.gemini_client import GeminiModel

from api.enums import parse_iteration_status, IterationStatusEnum, sanitize_result, normalize_status
from api.sse import emit
from sqlalchemy.orm import Session
from db import crud

import tools.git_ops as git_ops

logger = logging.getLogger(__name__)

# ---------------- STATE ---------------- #

class AgentState(TypedDict):
    run_id: str
    repo_url: str
    repo_path: str
    team_name: str
    leader_name: str
    failures: List[dict]
    fixes_applied: List[dict]
    iteration: int
    max_retries: int
    status: str
    results: Optional[dict]
    causal_graph: Optional[dict]
    whitebox: Optional[dict]
    health_before: Optional[int]
    health_after: Optional[int]
    db_iteration_id: int
    token: Optional[str]
    repository_profile: dict
    check_report: dict
    deferred_failures: int
    total_findings: int
    capped_findings: int
    ai: dict
    branch_name: str
    push_result: dict
    published_commit_sha: Optional[str]
    published_batches: int
    stop_reason: str

# ---------------- ORCHESTRATOR ---------------- #

class OrchestratorAgent:
    def _finding_strategy(self, failure, repo_path):
        """Retry a surviving finding when the source changed after an earlier fix."""
        file = failure.get('file')
        try:
            path = safe_path(repo_path, file)
            info = path.stat()
            signature = (info.st_mtime_ns, info.st_size)
            cache = getattr(self, '_source_fingerprints', {})
            previous = cache.get(str(path))
            if previous is None or previous[0] != signature:
                with path.open('rb') as handle:
                    fingerprint = hashlib.sha256(handle.read(1024 * 1024 + 1)).hexdigest()[:16]
                cache[str(path)] = (signature, fingerprint)
                self._source_fingerprints = cache
            else:
                fingerprint = previous[1]
        except (OSError, ValueError):
            fingerprint = 'unavailable'
        return f"{failure.get('bug_type', 'UNKNOWN')}:{failure.get('line', 0)}:{failure.get('error_message', '')[:100]}:{fingerprint}"

    def __init__(self, db: Session = None, publish: bool = True):
        self.db = db
        self.publish = publish
        api_key = os.getenv("GROQ_API_KEY")
        # Allow the agent to run in "analysis-only" mode when Groq isn't configured.
        # Fix generation will gracefully no-op in that case.
        self.groq_clients = [Groq(api_key=key, timeout=60, max_retries=0) for key in config.GROQ_API_KEYS]
        self.groq_client = self.groq_clients[0] if self.groq_clients else None
        self.ai_status = None
        
        # Gemini Support
        self.gemini_models = [GeminiModel(key, config.GEMINI_MODEL) for key in config.GEMINI_API_KEYS
                              if key != "your_gemini_api_key_here"]
        self.gemini_model = self.gemini_models[0] if self.gemini_models else None
        self._provider_slots = {'groq': 0, 'gemini': 0}

        self.analyzer = AnalyzerAgent()
        self.router = FixerRouter()
        self.validator = ValidatorAgent()
        self.reporter = ReporterAgent()
        self.memory = MemoryAgent()
        
        self.dep_graph = DependencyGraph()
        self.ledger = FailureLedger()
        self.context_builder = ContextBuilder()

        
        workflow = StateGraph(AgentState)
        workflow.add_node("analyze", self.analyze_node)
        workflow.add_node("fix", self.fix_node)
        workflow.add_node("validate", self.validate_node)
        workflow.add_node("report", self.report_node)

        workflow.add_edge(START, "analyze")
        workflow.add_conditional_edges("analyze", self.should_fix, {"continue": "fix", "end": "report"})
        workflow.add_edge("fix", "validate")
        workflow.add_conditional_edges("validate", self.should_retry, {"retry": "analyze", "end": "report"})
        workflow.add_edge("report", END)

        self.app = workflow.compile()

    def _limit_failures(self, state, failures):
        """Retain every finding; batching limits edits, never diagnostic visibility."""
        state['total_findings'] = len(failures)
        state['capped_findings'] = 0
        return failures

    def _next_provider_client(self, provider):
        clients = getattr(self, 'gemini_models', None) if provider == 'gemini' else getattr(self, 'groq_clients', None)
        if clients is None:
            fallback = getattr(self, 'gemini_model' if provider == 'gemini' else 'groq_client', None)
            clients = [fallback] if fallback is not None else []
        if not clients:
            return None, 0, 0
        if not hasattr(self, '_provider_slots'):
            self._provider_slots = {'groq': 0, 'gemini': 0}
        for _ in clients:
            index = self._provider_slots[provider] % len(clients)
            self._provider_slots[provider] += 1
            if id(clients[index]) not in getattr(self, '_unavailable_clients', set()):
                return clients[index], index + 1, len(clients)
        return None, 0, len(clients)

    # ---------------- ANALYZE ---------------- #

    def analyze_node(self, state: AgentState):
        run_id = state["run_id"]
        state["iteration"] += 1
        
        emit(run_id, "OrchestratorAgent", f"Starting analysis (Iteration {state['iteration']})...", "ANALYSIS_STARTED")

        if self.db:
            iteration = crud.create_iteration(
                self.db,
                run_id=run_id,
                iteration_number=state["iteration"]
            )
            state["db_iteration_id"] = iteration.id

        failures = self.analyzer.run(state["repo_path"], run_id=run_id)
        state['repository_profile'] = self.analyzer.inventory.summary()
        failures = self._limit_failures(state, failures)
        batch_size = self.analyzer.inventory.policy.fix_batch_size
        required_batches = (len(failures) + batch_size - 1) // batch_size
        state['max_retries'] = max(state['max_retries'], required_batches + config.MAX_RETRIES)
        state['check_report'] = self.analyzer.check_report
        policy = self.analyzer.inventory.policy
        self.context_builder = ContextBuilder(policy.context_tokens, policy.max_file_bytes)
        state["failures"] = failures
        state["whitebox"] = getattr(self.analyzer, "whitebox_report", None) or {
            "enabled": True,
            "status": "PARTIAL",
            "coverage": None,
            "uncovered_paths": [],
            "generated_tests": [],
            "reason": "White-box analysis unavailable.",
        }
        
        # Health score (before) = 100 - (failures * 5), capped at 0
        if state["iteration"] == 1:
            measured = state['check_report']['complete'] and (state['check_report']['passed'] or failures)
            state["health_before"] = max(0, 100 - len(failures) * 5) if measured else None
        
        if failures:
            emit(run_id, "AnalyzerAgent", f"Detected {len(failures)} failures.", "FAILURE_DETECTED")
            
            # Build dependency graph
            self.dep_graph.build_from_failures(failures, state["repo_path"])
            graph_data = self.dep_graph.to_dict()
            
            # Annotate nodes with status
            graph_data["nodes"] = [{"id": n, "status": "failing"} for n in graph_data["nodes"]]
            state["causal_graph"] = graph_data
            
            # Live Update: Initial Graph and Health
            emit(run_id, "OrchestratorAgent", json.dumps(graph_data), "GRAPH_UPDATED")
            emit(run_id, "OrchestratorAgent", json.dumps({"before": state["health_before"], "after": state["health_before"]}), "HEALTH_UPDATED")

            # Root nodes
            state["status"] = "running"
            
            root_nodes = self.dep_graph.get_root_nodes()
            emit(run_id, "OrchestratorAgent", f"Built dependency graph. Root cause nodes: {root_nodes}", "GRAPH_BUILT")
        else:
            state['status'] = 'passed' if state['check_report']['passed'] else 'failed'
            message = 'All configured checks passed.' if state['check_report']['passed'] else 'Analysis failed or is incomplete. See check results.'
            emit(run_id, "AnalyzerAgent", message, "ANALYSIS_COMPLETED")
            
        return state

    def should_fix(self, state: AgentState):
        return "continue" if state["failures"] else "end"

    # ---------------- FIX ---------------- #

    def fix_node(self, state: AgentState):
        run_id = state["run_id"]
        repo_path = state["repo_path"]
        self._batch_backups = {}
        self._batch_entries = []
        if getattr(self, 'ai_status', None) is None:
            groq_statuses = [check_groq(client, config.GROQ_MODEL) for client in self.groq_clients]
            gemini_statuses = [model.readiness() for model in self.gemini_models]
            self.groq_clients = [client for client, status in zip(self.groq_clients, groq_statuses) if status['ready']]
            self.gemini_models = [model for model, status in zip(self.gemini_models, gemini_statuses) if status['ready']]
            self.groq_client = self.groq_clients[0] if self.groq_clients else None
            self.gemini_model = self.gemini_models[0] if self.gemini_models else None
            groq_status = next((status for status in groq_statuses if status['ready']), groq_statuses[0] if groq_statuses else None)
            gemini_status = next((status for status in gemini_statuses if status['ready']), gemini_statuses[0] if gemini_statuses else None)
            statuses = [gemini_status, groq_status] if config.LLM_PRIMARY_PROVIDER == 'gemini' else [groq_status, gemini_status]
            self.ai_status = next((s for s in statuses if s and s['ready']), None)
            if self.ai_status is None:
                fallback_status = next((s for s in statuses if s), {
                    'ready': False, 'provider': 'none', 'model': '', 'reason': 'No AI provider is configured.'})
                self.ai_status = {**fallback_status, 'reason': ' '.join(s['reason'] for s in statuses if s)}
            self.ai_status['available_providers'] = [s['provider'] for s in statuses if s and s['ready']]
            emit(run_id, 'OrchestratorAgent',
                 f"AI selected: {self.ai_status['provider']}. Available providers: {', '.join(self.ai_status['available_providers']) or 'none'}.",
                 'AI_PROVIDER_SELECTED')
        state['ai'] = self.ai_status
        if not self.ai_status['ready']:
            state['deferred_failures'] = len(state['failures'])
            emit(run_id, 'FixerAgent', self.ai_status['reason'], 'AI_UNAVAILABLE')
            return state
        
        # Sort by root cause: fix root nodes first
        root_nodes = set(self.dep_graph.get_root_nodes())
        severity = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        sorted_failures = sorted(state["failures"], key=lambda f: (
            0 if f.get('bug_type') == 'SYNTAX' else 1,
            0 if f.get('file') in root_nodes else 1,
            severity.get(f.get('severity'), 2), f.get('file', '')))
        policy = state['repository_profile']['policy']
        fix_limit = policy.get('max_fixes_per_run', config.MAX_FIXES_PER_RUN)
        applied_count = sum(f.get('status') == 'applied' for f in state['fixes_applied'])
        remaining_budget = max(0, fix_limit - applied_count) if fix_limit else len(sorted_failures)
        batch_size = min(policy.get('fix_batch_size', config.FIX_BATCH_SIZE), remaining_budget)
        if batch_size == 0:
            state['deferred_failures'] = 0
            state['stop_reason'] = 'Configured fix budget reached; remaining findings are included in the report.'
            emit(run_id, 'OrchestratorAgent', state['stop_reason'],
                 'FIX_LIMIT_REACHED')
            return state
        attempted = 0
        batch_clients = {}

        def batch_client(provider):
            if provider not in batch_clients:
                batch_clients[provider] = self._next_provider_client(provider)
            return batch_clients[provider]
        
        grouped = {}
        finding_keys = {id(f): self._finding_strategy(f, state['repo_path']) for f in sorted_failures}
        for item in sorted_failures:
            grouped.setdefault(item['file'], []).append(item)
        for file_failures in grouped.values():
            eligible = [f for f in file_failures if not self.ledger.was_tried(
                f['file'], self._finding_strategy(f, state['repo_path']))]
            if not eligible:
                continue
            if attempted >= batch_size:
                break
            eligible = eligible[:batch_size - attempted]
            failure = dict(eligible[0])
            file_key = failure.get("file")
            bug_type = failure.get("bug_type", "UNKNOWN")
            strategy = self._finding_strategy(failure, state['repo_path'])
            failure['error_message'] = '\n'.join(
                f"Line {f.get('line', 0)}: {f.get('error_message', '')}" for f in eligible[:10])[:1500]
            
            # Check ledger — skip if already tried
            if self.ledger.was_tried(file_key, strategy):
                emit(run_id, "MemoryAgent", f"Skipping {file_key} — strategy already tried.", "FIX_ROUTED")
                continue
            if attempted >= batch_size:
                break
            if bug_type == 'LOGIC' and (os.path.basename(file_key).startswith('test_') or 'tests' in file_key.replace('\\', '/').split('/')):
                self.ledger.add_attempt(file_key, strategy, 'validation_only')
                emit(run_id, 'ValidatorAgent', f'Keeping {file_key} unchanged; test failures require a source-code fix.', 'FIX_ROUTED')
                continue
            attempted += len(eligible)
            
            try:
                emit(run_id, "OrchestratorAgent", f"Routing {bug_type} fix for {file_key}...", "FIX_ROUTED")
                
                AgentClass = self.router.route(bug_type, file_path=file_key)
                primary = self.ai_status.get('provider', 'groq')
                if primary == 'gemini':
                    primary_client, slot, slots = batch_client(primary)
                    fallback = batch_client('groq')[0]
                    agent = AgentClass(fallback, self.context_builder, gemini_model=primary_client)
                elif primary == 'groq':
                    primary_client, slot, slots = batch_client(primary)
                    fallback = batch_client('gemini')[0]
                    agent = AgentClass(primary_client, self.context_builder, gemini_model=fallback)
                else:
                    # Test and extension providers can supply their own mock
                    # client while retaining the standard batching workflow.
                    slot, slots = 0, 0
                    agent = AgentClass(self.groq_client, self.context_builder, gemini_model=self.gemini_model)
                agent.primary_provider = self.ai_status.get('provider', 'groq')
                if slots:
                    emit(run_id, 'OrchestratorAgent',
                         f'Batch progress {attempted}/{batch_size} issues assigned to {primary} credential {slot}/{slots}.',
                         'PROVIDER_SLOT_ASSIGNED')
                agent.on_progress = lambda message: emit(run_id, 'FixerAgent', message, 'AI_RETRY')
                
                # Memory history
                history = self.memory.get_context(file_key, failure.get("line", 0))
                
                # Blast radius
                try:
                    blast = self.dep_graph.get_blast_radius(file_key)
                except Exception:
                    blast = 1
                
                fix_res = agent.fix(failure, repo_path, history)
                if fix_res and fix_res.get('provider'):
                    provider = fix_res['provider']
                    self.ai_status = {**self.ai_status, 'ready': True, 'provider': provider,
                                      'model': config.GEMINI_MODEL if provider == 'gemini' else config.GROQ_MODEL, 'reason': ''}
                    state['ai'] = self.ai_status

                if fix_res and fix_res.get("fixed_code"):
                    # Apply fix
                    full_path = safe_path(repo_path, file_key)
                    if full_path.suffix == '.py':
                        compile(fix_res['fixed_code'], str(full_path), 'exec')
                    try:
                        with open(full_path, "r", encoding='utf-8', newline='') as f:
                            before_code = f.read()
                    except Exception:
                        before_code = ""
                        
                    self._batch_backups[file_key] = before_code
                    with open(full_path, "w", encoding='utf-8', newline='') as f:
                        f.write(fix_res["fixed_code"])
                    
                    # Confidence
                    conf = calc_confidence({
                        "bug_type": bug_type,
                        "lines_changed": abs(len(fix_res["fixed_code"].splitlines()) - len(before_code.splitlines())),
                        "blast_radius": blast,
                        "touches_imports": "import" in fix_res["fixed_code"][:200],
                    })
                    
                    fix_entry = {
                        "file": file_key,
                        "bug_type": bug_type,
                        "agent": agent.specialty,
                        "explanation": fix_res.get("explanation", ""),
                        "confidence": conf["score"],
                        "blast_radius": blast,
                        "status": "applied",
                        "iteration": state['iteration'],
                        "before_code": before_code[:500],
                        "fixed_code": fix_res["fixed_code"][:500],
                    }
                    # Sanitize fix entry before recording
                    fix_entry = sanitize_result(fix_entry)
                    state["fixes_applied"].append(fix_entry)
                    self._batch_entries.append(fix_entry)
                    
                    # Record in ledger and memory
                    for finding in eligible:
                        self.ledger.add_attempt(file_key, finding_keys[id(finding)], fix_entry["status"])
                    self.memory.record(file_key, failure.get("line", 0), strategy, fix_entry["status"])
                    
                    emit(run_id, agent.specialty + "Agent", json.dumps(fix_entry), "FIX_APPLIED")

                    # Live Update: Update Graph and Score progress
                    if state.get("causal_graph"):
                        for node in state["causal_graph"]["nodes"]:
                            if node["id"] == file_key:
                                node["status"] = "fixed"
                        emit(run_id, "OrchestratorAgent", json.dumps(state["causal_graph"]), "GRAPH_UPDATED")
                    
                    # Dynamic score estimation
                    current_score = {"base": 100, "speed_bonus": 0, "efficiency_penalty": 0, "total": max(0, min(100, 100 - (len(state["failures"]) - len(state["fixes_applied"])) * 5))}
                    emit(run_id, "OrchestratorAgent", json.dumps(current_score), "SCORE_UPDATED")

                    # DB LOGGING
                    if self.db:
                        crud.create_fix(
                            db=self.db,
                            run_id=run_id,
                            iteration_id=state.get("db_iteration_id"),
                            file_path=file_key,
                            bug_type=bug_type,
                            commit_message=fix_res.get("explanation", "Auto fix"),
                            status="applied",
                            confidence_score=conf["score"]
                        )
                else:
                    if fix_res and (fix_res.get('retryable') or fix_res.get('provider_failure')):
                        unavailable = getattr(self, '_unavailable_clients', set())
                        failed_providers = [p['provider'] for p in fix_res.get('providers', [])] or [primary]
                        for provider in failed_providers:
                            client = batch_clients.get(provider, (None, 0, 0))[0]
                            if client is not None:
                                unavailable.add(id(client))
                        self._unavailable_clients = unavailable
                        available = [provider for provider, clients in (
                            ('groq', getattr(self, 'groq_clients', [])),
                            ('gemini', getattr(self, 'gemini_models', [])))
                            if any(id(client) not in unavailable for client in clients)]
                        self.ai_status = {**self.ai_status, 'ready': bool(available),
                                          'reason': fix_res['error'], 'provider_errors': fix_res.get('providers', [])}
                        if available:
                            self.ai_status['provider'] = primary if primary in available else available[0]
                        state['ai'] = self.ai_status
                        emit(run_id, 'FixerAgent', fix_res['error'] + (
                            ' Validating current edits before retrying with the next available credential.' if available else ''),
                            'AI_RETRY' if available else 'AI_UNAVAILABLE')
                        break
                    for finding in eligible:
                        self.ledger.add_attempt(file_key, finding_keys[id(finding)], 'failed')
                    message = fix_res.get('error') if fix_res else None
                    emit(run_id, "OrchestratorAgent", message or f"Failed to generate fix for {file_key}", "FIX_FAILED")
            except Exception as e:
                logger.error(f"Error during fix loop for {file_key}: {e}", exc_info=True)
                emit(run_id, "OrchestratorAgent", f"Crash during fix for {file_key}: {str(e)}", "FIX_FAILED")
                for finding in eligible:
                    self.ledger.add_attempt(file_key, finding_keys[id(finding)], 'failed')
                
        state['deferred_failures'] = sum(
            not self.ledger.was_tried(f.get('file'), finding_keys[id(f)])
            for f in sorted_failures)
        emit(run_id, 'OrchestratorAgent',
             f"Batch attempted {attempted} findings; {state['deferred_failures']} findings await later iterations.", 'BATCH_COMPLETED')
        return state

    # ---------------- VALIDATE ---------------- #

    def _publish_validated_batch(self, state: AgentState):
        """Commit and push one non-regressing batch to this run's fix branch.

        A repository may still have known failures after a useful batch is fixed.
        Those failures must not prevent already validated work from being saved, but
        no batch is published when it regresses a check that was passing beforehand.
        """
        entries = [entry for entry in getattr(self, '_batch_entries', [])
                   if entry.get('status') == 'applied']
        if not entries or not getattr(self, 'publish', True):
            return
        if (state.get('push_result') or {}).get('status') == 'failed':
            return

        run_id = state['run_id']
        batch_number = state.get('published_batches', 0) + 1
        branch_name = state.get('branch_name')
        message = f'Applied autonomous fixes (validated batch {batch_number})'
        emit(run_id, 'GitAgent',
             f'Batch {batch_number} passed regression validation. Committing and pushing it to a fix branch...',
             'COMMIT_STARTED')
        try:
            push_res = git_ops.commit_and_push_batch(
                state['repo_path'], message, branch_name=branch_name, token=state.get('token'),
                expected_remote_sha=state.get('published_commit_sha'))
        except Exception as exc:
            push_res = {'status': 'failed', 'branch': branch_name or '', 'error': str(exc)}

        if push_res.get('status') == 'success' and push_res.get('branch') and push_res.get('commit_sha'):
            state['branch_name'] = push_res['branch']
            state['published_commit_sha'] = push_res['commit_sha']
            state['published_batches'] = batch_number
            state['push_result'] = push_res
            emit(run_id, 'GitAgent',
                 f"Pushed and verified validated batch {batch_number} on branch {push_res['branch']}: "
                 f"{push_res.get('branch_url') or push_res['commit_sha']}", 'COMMIT_DONE')
        elif push_res.get('status') == 'nothing_to_commit':
            state['push_result'] = push_res
            emit(run_id, 'GitAgent', 'Validated batch did not contain source changes to commit.', 'COMMIT_SKIPPED')
        else:
            if push_res.get('status') == 'success':
                push_res = {**push_res, 'status': 'failed', 'error_type': 'PUBLISH_PROTOCOL_ERROR',
                            'error': 'The publishing result did not include a verified branch and commit SHA.'}
            state['push_result'] = push_res
            emit(run_id, 'GitAgent',
                 f"Validated batch was not pushed: {push_res.get('error') or push_res.get('status')}",
                 'COMMIT_FAILED')

    def validate_node(self, state: AgentState):
        run_id = state["run_id"]
        if state.get('ai') and not state['ai']['ready'] and not getattr(self, '_batch_backups', {}):
            state['status'] = 'failed'
            return state
        emit(run_id, "ValidatorAgent", "Validating fixes...", "VALIDATION_STARTED")
        
        def check_identity(check):
            return (check['root'], check['name'], check.get('source'), tuple(check.get('command') or []))
        baseline = {check_identity(c) for c in state.get('check_report', {}).get('checks', []) if c['status'] == 'passed'}
        val_res = self.validator.validate(state["repo_path"])
        current = {check_identity(c): c for c in val_res['check_report']['checks']}
        regressed = [current.get(key, {'name': key[1], 'status': 'missing'})
                     for key in baseline if key not in current or current[key]['status'] != 'passed']
        if regressed and getattr(self, '_batch_backups', {}):
            for file, content in self._batch_backups.items():
                with open(safe_path(state['repo_path'], file), 'w', encoding='utf-8', newline='') as handle:
                    handle.write(content)
            for entry in self._batch_entries:
                entry['status'] = 'reverted'
            if self.db and state.get('db_iteration_id'):
                crud.revert_iteration_fixes(self.db, state['db_iteration_id'])
            emit(run_id, 'ValidatorAgent', json.dumps({
                'files': list(self._batch_backups), 'iteration': state['iteration'],
                'message': 'Reverted the batch because a previously passing check regressed: ' + ', '.join(c['name'] for c in regressed),
            }), 'FIX_REVERTED')
            val_res = self.validator.validate(state['repo_path'])
        elif not regressed:
            # This intentionally does not require the whole repository to pass:
            # pre-existing failures remain eligible for later batches, while the
            # current batch has proven it did not break prior green checks.
            self._publish_validated_batch(state)
        state['check_report'] = val_res['check_report']
        state['failures'] = self._limit_failures(state, val_res['check_report']['failures'])
        state['deferred_failures'] = sum(
            not self.ledger.was_tried(f.get('file'), self._finding_strategy(f, state['repo_path']))
            for f in state['failures'])

        # Determine if we passed or failed using robust helper
        it_status = parse_iteration_status(val_res["status"])
        state["status"] = it_status.value

        if self.db and state.get("db_iteration_id"):
            it_status = parse_iteration_status(state["status"])
            crud.update_iteration(self.db, iteration_id=state["db_iteration_id"], status=it_status.value, logs=val_res.get("message", "Validation complete"))
            
            # Overall status stays running until retries and final reporting finish.

        emit(run_id, "ValidatorAgent", f"Validation finished with status: {state['status']}", "VALIDATION_DONE")
        return state

    def should_retry(self, state: AgentState):
        if state["status"] == "passed":
            return "end"
        if not state['failures']:
            return 'end'
        if state.get('ai') and not state['ai']['ready']:
            state['stop_reason'] = state['ai'].get('reason', 'AI providers unavailable')
            return 'end'
        limit = state.get('repository_profile', {}).get('policy', {}).get('max_fixes_per_run', config.MAX_FIXES_PER_RUN)
        if limit and sum(f.get('status') == 'applied' for f in state['fixes_applied']) >= limit:
            state['stop_reason'] = 'Configured fix budget reached; unresolved findings remain.'
            return 'end'
        if state.get('deferred_failures') == 0:
            state['stop_reason'] = 'No untried repair strategy remains for the current source; unresolved findings remain.'
            return 'end'
        
        if state["iteration"] < state["max_retries"]:
            return "retry"
        state['stop_reason'] = 'Repair iteration budget exhausted; unresolved findings remain.'
        return "end"

    # ---------------- REPORT ---------------- #

    def report_node(self, state: AgentState):
        run_id = state["run_id"]
        check_report = state.get('check_report', {})
        validated = state['status'] == 'passed' and check_report.get('passed') and check_report.get('complete')
        if not validated:
            state['status'] = 'failed'
        branch_name = state.get('branch_name', '')
        push_res = state.get('push_result') or {'status': 'skipped', 'reason': 'No validated changes to push'}
        if not branch_name and not getattr(self, 'publish', True):
            push_res['reason'] = 'Publishing is disabled for this local verification run'
        elif not branch_name and not validated:
            push_res['reason'] = 'No batch completed regression validation before the run ended'
            emit(run_id, 'GitAgent', push_res['reason'], 'COMMIT_SKIPPED')

        remaining = len(state['failures'])
        measured = check_report.get('complete') and (validated or remaining)
        state['health_after'] = max(0, 100 - remaining * 5) if measured else None
        fixed_files = {f['file'] for f in state['fixes_applied']}
        if state.get('causal_graph'):
            for node in state['causal_graph'].get('nodes', []):
                if node['id'] in fixed_files:
                    node['status'] = 'fixed' if validated else 'unverified'
        # Keep enough diagnostic output for the dashboard without storing megabytes per check.
        report = {
            **check_report,
            'ai': state.get('ai'),
            'stop_reason': state.get('stop_reason', ''),
            'checks': [{**c, 'output': c.get('output', '')[-4000:]} for c in check_report.get('checks', [])],
        }
        results = self.reporter.build_results(
            run_id=run_id, repo_url=state['repo_url'], final_status=state['status'],
            iterations=state['iteration'], failures_log=state['failures'], fixes=state['fixes_applied'],
            health_score={'before': state.get('health_before', 0), 'after': state['health_after']},
            causal_graph=state.get('causal_graph'), branch_name=branch_name, whitebox=state.get('whitebox'),
            repository_profile=state.get('repository_profile'), check_report=report, push_result=push_res,
            deferred_failures=state.get('deferred_failures', 0))
        state['results'] = results
        if self.db:
            crud.update_run(
                self.db, run_id=run_id, status=state['status'],
                overall_score=100 if validated else 0,
                total_fixes=sum(f.get('status') == 'applied' for f in state['fixes_applied']), total_failures=remaining, memory=results,
                branch_name=branch_name if push_res.get('status') == 'success' else None)
        emit(run_id, 'OrchestratorAgent', json.dumps(results), 'RUN_COMPLETED')
        return state

    # ---------------- RUN ---------------- #

    def run(self, run_id: str, repo_url: str, repo_path: str, team_name: str = "", leader_name: str = "", token: str = None):
        max_retries = int(os.getenv("MAX_RETRIES", 5))
        inventory = RepositoryInventory(repo_path)
        batch_size = inventory.policy.fix_batch_size
        # Reserve a first attempt for every source-file batch, plus retry rounds.
        max_retries += (len(inventory.source_files()) + batch_size - 1) // batch_size
        emit(run_id, "OrchestratorAgent", "Run started", "START")
        emit(run_id, 'OrchestratorAgent',
             f"AI configuration: primary {config.LLM_PRIMARY_PROVIDER}; Groq {'configured' if self.groq_client else 'not configured'}; "
             f"Gemini {'configured' if self.gemini_model else 'not configured'}.", 'AI_CONFIGURATION')

        initial_state = {
            "run_id": run_id,
            "repo_url": repo_url,
            "repo_path": repo_path,
            "team_name": team_name,
            "leader_name": leader_name,
            "failures": [],
            "fixes_applied": [],
            "iteration": 0,
            "max_retries": max_retries,
            "status": "running",
            "results": None,
            "causal_graph": None,
            "health_before": 0,
            "health_after": 0,
            "db_iteration_id": 0,
            "token": token,
            "total_findings": 0,
            "capped_findings": 0,
            "branch_name": "",
            "push_result": None,
            "published_commit_sha": None,
            "published_batches": 0,
            "stop_reason": "",
        }

        # Findings per file are unknown until analysis; the state supplies the
        # tighter iteration budget once discovered. Leave graph capacity for it.
        return self.app.invoke(initial_state, config={'recursion_limit': max(10000, max_retries * 4 + 10)})
