import { useEffect, useRef } from 'react';
import { useAgentStore } from '../store/agentStore';
import { getStatus, getResults } from '../lib/api';
import { displayEvent } from '../lib/runEvents';

export const useAgentSSE = (runId) => {
  const ref = useRef(null);
  const appendThought = useAgentStore((s) => s.appendThought);
  const appendFix = useAgentStore((s) => s.appendFix);
  const setScore = useAgentStore((s) => s.setScore);
  const setHealthScore = useAgentStore((s) => s.setHealthScore);
  const setCausalGraph = useAgentStore((s) => s.setCausalGraph);
  const setWhiteBox = useAgentStore((s) => s.setWhiteBox);
  const setStatus = useAgentStore((s) => s.setStatus);
  const setEndTime = useAgentStore((s) => s.setEndTime);
  const setTotals = useAgentStore((s) => s.setTotals);
  const setRepositoryProfile = useAgentStore((s) => s.setRepositoryProfile);
  const setAnalysis = useAgentStore((s) => s.setAnalysis);

  useEffect(() => {
    if (!runId) return;
    let disposed = false;
    let terminal = false;
    let checking = false;

    const baseUrl = (import.meta.env.VITE_API_URL || '').replace(/\/+$/, '');
    const es = new EventSource(`${baseUrl}/stream/${runId}`);
    ref.current = es;

    es.onmessage = (event) => {
      if (disposed || terminal) return;
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'heartbeat') return;

        // Always add to thought stream
        appendThought(displayEvent(data));

        // Handle specific event types
        switch (data.type) {
          case 'REPOSITORY_CLASSIFIED':
            setRepositoryProfile(JSON.parse(data.message));
            break;
          case 'FIX_APPLIED':
            // Try to parse fix data from message
            try {
              const fixData = JSON.parse(data.message);
              appendFix(fixData);
            } catch {
              appendFix({
                file: data.agent,
                bug_type: 'FIX',
                explanation: data.message,
                status: 'applied',
              });
            }
            break;

          case 'FIX_REVERTED': {
            const reverted = JSON.parse(data.message);
            useAgentStore.setState((state) => ({ fixes: state.fixes.map((fix) =>
              reverted.files.includes(fix.file) && fix.iteration === reverted.iteration ? { ...fix, status: 'reverted' } : fix) }));
            break;
          }

          case 'RUN_COMPLETED':
            // Try to extract result data
            try {
              const result = JSON.parse(data.message);
              if (result.score) setScore(result.score);
              if (result.health_score) setHealthScore(result.health_score);
              if (result.causal_graph) setCausalGraph(result.causal_graph);
              if (result.whitebox) setWhiteBox(result.whitebox);
              if (result.repository_profile) setRepositoryProfile(result.repository_profile);
              if (result.analysis) setAnalysis(result.analysis);
              useAgentStore.setState({ branch: result.branch || '', push: result.push || null });
              if (result.fixes) useAgentStore.setState({ fixes: result.fixes });
              if (result.total_failures !== undefined) setTotals(result.total_failures, result.total_fixes);

              // Respect backend final status (PASSED/FAILED/etc)
              const finalStatus = (result.status || result.final_status || '').toString().toLowerCase();
              setStatus(['passed', 'pass'].includes(finalStatus) && result.analysis?.complete !== false ? 'passed' : 'failed');
            } catch {
              // A malformed completion event cannot establish a successful run.
              setStatus('failed');
            }
            setEndTime(new Date().toISOString());
            terminal = true;
            es.close();
            break;

          case 'RUN_FAILED':
          case 'error':
            setStatus('failed');
            setEndTime(new Date().toISOString());
            terminal = true;
            es.close();
            break;

          case 'HEALTH_UPDATED':
            try { setHealthScore(JSON.parse(data.message)); } catch {
              // Ignore malformed optional update events.
            }
            break;

          case 'SCORE_UPDATED':
            try { setScore(JSON.parse(data.message)); } catch {
              // Ignore malformed optional update events.
            }
            break;

          case 'GRAPH_UPDATED':
            try { setCausalGraph(JSON.parse(data.message)); } catch {
              // Ignore malformed optional update events.
            }
            break;

          default:
            break;
        }
      } catch (e) {
        console.error('SSE parse error:', e);
      }
    };

    const checkPersistedStatus = async () => {
      if (disposed || terminal || checking) return;
      checking = true;
      try {
        const result = await getStatus(runId);
        if (disposed || terminal) return;
        if (['passed', 'failed', 'completed'].includes(result.status)) {
          if (result.has_results) {
            const report = await getResults(runId);
            if (!disposed && !terminal) es.onmessage({ data: JSON.stringify({ type: 'RUN_COMPLETED', agent: 'ReporterAgent', message: JSON.stringify(report) }) });
          } else {
            es.onmessage({ data: JSON.stringify({ type: 'RUN_FAILED', agent: 'OrchestratorAgent', message: result.error || 'Run failed before a final report was generated.' }) });
          }
        }
      } catch (error) {
        if (!disposed && !terminal && error.response?.status === 404) {
          es.onmessage({ data: JSON.stringify({ type: 'RUN_FAILED', agent: 'OrchestratorAgent', message: 'Run not found. Start a new run.' }) });
        }
        // Allow EventSource to reconnect when the backend becomes available.
      } finally {
        checking = false;
      }
    };
    checkPersistedStatus();
    es.onerror = checkPersistedStatus;
    const timer = setInterval(checkPersistedStatus, 5000);

    return () => {
      disposed = true;
      clearInterval(timer);
      es.close();
    };
  }, [runId]);
};
