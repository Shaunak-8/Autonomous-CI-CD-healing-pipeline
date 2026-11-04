import React from 'react';

export default function WhiteBoxPanel({ whitebox }) {
  const coverage = whitebox?.coverage || {};
  const uncovered = whitebox?.uncovered_paths || [];
  const generated = whitebox?.generated_tests || [];

  const coverageStats = [
    { label: 'LINE', value: coverage.line_coverage != null ? `${coverage.line_coverage.toFixed(1)}%` : 'N/A' },
    { label: 'STATEMENT', value: coverage.statement_coverage != null ? `${coverage.statement_coverage.toFixed(1)}%` : 'N/A' },
    { label: 'BRANCH', value: coverage.branch_coverage != null ? `${coverage.branch_coverage.toFixed(1)}%` : 'N/A' },
    { label: 'FUNCTION', value: coverage.function_coverage != null ? `${coverage.function_coverage.toFixed(1)}%` : 'N/A' },
  ];

  return (
    <div style={styles.card}>
      <h3 style={styles.title}>WHITE_BOX_ANALYSIS</h3>
      <div style={styles.overview}>
        {coverageStats.map((metric) => (
          <div key={metric.label} style={styles.pill}>
            <span style={styles.pillLabel}>{metric.label}</span>
            <span style={styles.pillValue}>{metric.value}</span>
          </div>
        ))}
      </div>

      <div style={styles.sectionBlock}>
        <div style={styles.sectionHeader}>WHITE_BOX_FINDINGS</div>
        {uncovered.length === 0 ? (
          <div style={styles.empty}>No uncovered paths recorded.</div>
        ) : (
          <div style={styles.tableScroll} tabIndex={0} aria-label="White-box findings">
          <table style={styles.table}>
            <thead>
              <tr>
                <th>File</th>
                <th>Function</th>
                <th>Line</th>
                <th>Type</th>
                <th>Condition</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {uncovered.slice(0, 6).map((item, idx) => (
                <tr key={`${item.file}-${item.line}-${idx}`}>
                  <td>{item.file}</td>
                  <td>{item.function}</td>
                  <td>{item.line}</td>
                  <td>{item.type}</td>
                  <td>{item.condition || 'N/A'}</td>
                  <td>{item.coverage_status || 'UNTESTED'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      <div style={styles.sectionBlock}>
        <div style={styles.sectionHeader}>GENERATED_TESTS</div>
        {generated.length === 0 ? (
          <div style={styles.empty}>No targeted tests generated.</div>
        ) : (
          <div style={styles.tableScroll} tabIndex={0} aria-label="Generated tests">
          <table style={styles.table}>
            <thead>
              <tr>
                <th>Test</th>
                <th>Target Path</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {generated.map((item, idx) => (
                <tr key={`${item.file}-${idx}`}>
                  <td>{item.name || 'whitebox_case'}</td>
                  <td>{item.file}</td>
                  <td>{item.status || 'UNKNOWN'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      {whitebox?.reason && <div style={styles.reason}>{whitebox.reason}</div>}
    </div>
  );
}

const styles = {
  card: { background: '#111118', border: '1px solid #1e1e2e', borderRadius: 16, padding: 20, display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0, overflow: 'hidden' },
  title: { fontSize: 10, color: 'var(--text-secondary)', letterSpacing: 2, margin: 0, fontFamily: 'var(--font-mono)', fontWeight: 800 },
  overview: { display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 },
  pill: { background: '#0a0a0f', border: '1px solid #1e1e2e', borderRadius: 12, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 4 },
  pillLabel: { fontSize: 8, color: 'var(--text-secondary)', letterSpacing: 1.5, fontFamily: 'var(--font-mono)', fontWeight: 700 },
  pillValue: { fontSize: 18, fontWeight: 800, color: '#fff', fontFamily: 'var(--font-mono)' },
  sectionBlock: { display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 },
  tableScroll: { maxWidth: '100%', overflowX: 'auto', border: '1px solid #252530', borderRadius: 6 },
  sectionHeader: { fontSize: 9, color: 'var(--text-secondary)', letterSpacing: 2, fontFamily: 'var(--font-mono)', fontWeight: 700 },
  table: { width: '100%', minWidth: 640, borderCollapse: 'separate', borderSpacing: 12, fontSize: 11, lineHeight: 1.6, textAlign: 'left', color: '#d9d9d9', fontFamily: 'var(--font-mono)' },
  empty: { color: 'var(--text-secondary)', fontSize: 12, fontStyle: 'italic' },
  reason: { fontSize: 11, color: '#b5b5b5', fontFamily: 'var(--font-mono)' },
};
