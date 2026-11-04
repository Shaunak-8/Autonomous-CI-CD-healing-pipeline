import { Target, AlertTriangle, CheckCircle, Layers } from 'lucide-react';
import { useAgentStore } from '../store/agentStore';

export default function RunSummaryCard({ status, totalFailures, totalFixes, score }) {
  const profile = useAgentStore((s) => s.repositoryProfile);
  const analysis = useAgentStore((s) => s.analysis);
  const push = useAgentStore((s) => s.push);
  const metrics = [
    { label: 'FAILURES', value: totalFailures, icon: <AlertTriangle size={14} />, color: '#ff3b3b' },
    { label: 'FIXES', value: totalFixes, icon: <CheckCircle size={14} />, color: '#00ff88' },
    { label: 'SCORE', value: score?.total || score?.final || 0, icon: <Target size={14} />, color: '#00ccff' },
  ];

  return (
    <div style={styles.card}>
      <h3 style={styles.title}><Layers size={14} color="#a855f7" /> RUN SUMMARY</h3>
      {profile && (
        <p style={{ color: '#aaa', fontSize: 12 }}>
          {profile.size.toUpperCase()} REPOSITORY · {profile.source_files.toLocaleString()} source files
          {' · '}{(profile.source_bytes / 1024 / 1024).toFixed(1)} MB
        </p>
      )}
      {analysis && !analysis.complete && (
        <p role="status" style={{ color: '#ffb347', fontSize: 12 }}>
          Analysis incomplete. {analysis.reason} Changes were not pushed.
        </p>
      )}
      {analysis?.ai && !analysis.ai.ready && <p role="alert" style={{ color: '#ffb347', fontSize: 12 }}>{analysis.ai.reason}</p>}
      {push && (
        <p role={push.status === 'failed' ? 'alert' : 'status'} style={{ color: push.status === 'success' ? '#00ff88' : '#ffb347', fontSize: 12, overflowWrap: 'anywhere' }}>
          {push.status === 'success' ? <>Pushed to new branch: {push.branch_url ? <a href={push.branch_url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit' }}>{push.branch}</a> : push.branch}</> :
            `Push ${push.status}: ${push.error || push.reason || 'No source changes to commit.'}`}
        </p>
      )}
      {analysis?.checks?.filter((check) => check.status !== 'passed').map((check) => (
        <details key={`${check.root}:${check.name}`} style={{ color: '#ffb347', fontSize: 12, marginBottom: 8 }}>
          <summary>{check.root}: {check.name} — {check.status}</summary>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{check.output}</pre>
        </details>
      ))}
      <div style={styles.grid}>
        {metrics.map((m, i) => (
          <div key={i} style={styles.metricBox}>
            <div style={{ color: m.color, marginBottom: 8 }}>{m.icon}</div>
            <span style={{ ...styles.value, color: m.color }}>{m.value}</span>
            <span style={styles.label}>{m.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const styles = {
  card: { background: '#111118', border: '1px solid #1e1e2e', borderRadius: 16, padding: 20 },
  title: { fontSize: 10, color: 'var(--text-secondary) !important', letterSpacing: 2, marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8, fontFamily: "var(--font-mono) !important", fontWeight: 800 },
  grid: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 },
  metricBox: { background: '#0a0a0f', border: '1px solid #1e1e2e', borderRadius: 12, padding: 16, textAlign: 'center' },
  value: { display: 'block', fontSize: 20, fontWeight: 800, fontFamily: "var(--font-mono)", color: '#fff' },
  label: { display: 'block', fontSize: 8, color: 'var(--text-secondary) !important', letterSpacing: 2, marginTop: 6, fontFamily: "var(--font-mono)", fontWeight: 700, opacity: 0.9 },
};
