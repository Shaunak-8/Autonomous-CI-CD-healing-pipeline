export function displayEvent(event) {
  try {
    const payload = JSON.parse(event.message);
    const summaries = {
      REPOSITORY_CLASSIFIED: () => `${payload.size} repository: ${payload.source_files} source files.`,
      GRAPH_UPDATED: () => `Dependency graph: ${payload.nodes?.length || 0} files, ${payload.edges?.length || 0} connections.`,
      HEALTH_UPDATED: () => `Health: ${payload.before ?? 'unmeasured'} → ${payload.after ?? 'unmeasured'}.`,
      SCORE_UPDATED: () => `Score: ${payload.total ?? payload.final ?? 0}.`,
      FIX_APPLIED: () => `Applied ${payload.bug_type || 'code'} fix to ${payload.file}.`,
      FIX_REVERTED: () => payload.message || `Reverted fixes in ${payload.files?.length || 0} files.`,
      RUN_COMPLETED: () => `Run ${payload.status}: ${payload.total_fixes ?? 0} fixes applied; ${payload.total_failures ?? 0} findings remain.`,
    };
    if (summaries[event.type]) return { ...event, message: summaries[event.type]() };
  } catch {
    // Most agent messages are ordinary text.
  }
  return { ...event, message: String(event.message || '').slice(0, 2000) };
}
