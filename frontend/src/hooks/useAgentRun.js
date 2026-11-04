import { useAgentStore } from '../store/agentStore';
import { runAgent } from '../lib/api';
import { useCallback } from 'react';

export const useAgentRun = () => {
    const projectId = useAgentStore((s) => s.projectId);
    const repoUrl = useAgentStore((s) => s.repoUrl);
    const teamName = useAgentStore((s) => s.teamName);
    const leaderName = useAgentStore((s) => s.leaderName);
    const reset = useAgentStore((s) => s.reset);
    const setStatus = useAgentStore((s) => s.setStatus);
    const setStartTime = useAgentStore((s) => s.setStartTime);
    const setRunId = useAgentStore((s) => s.setRunId);

    const startRun = useCallback(async () => {
        if (!repoUrl) return;

        reset();
        useAgentStore.getState().setRepoUrl(repoUrl);
        setStatus('running');
        setStartTime(new Date().toISOString());

        try {
            const result = await runAgent({ 
                project_id: projectId,
                repo_url: repoUrl, 
                team_name: teamName, 
                leader_name: leaderName 
            });
            setRunId(result.run_id);
        } catch (error) {
            useAgentStore.getState().appendThought({
                agent: 'RunAgent', type: 'RUN_FAILED',
                message: error.response?.data?.detail || error.message || 'Unable to start the run.',
                timestamp: new Date().toISOString(),
            });
            setStatus('failed');
        }
    }, [projectId, repoUrl, teamName, leaderName, reset, setStatus, setStartTime, setRunId]);

    return { startRun };
};
