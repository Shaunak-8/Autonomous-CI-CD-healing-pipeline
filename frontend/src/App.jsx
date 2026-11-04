import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import HeroPage from './pages/HeroPage';
import AuthPage from './pages/AuthPage';
import BasicInfoPage from './pages/BasicInfoPage';
import ConnectRepoPage from './pages/ConnectRepoPage';
import InviteCollabPage from './pages/InviteCollabPage';
import DashboardPage from './pages/DashboardPage';

import AppLayout from './components/AppLayout';
import GlobalDashboardPage from './pages/GlobalDashboardPage';
import ProjectsPage from './pages/ProjectsPage';
import WorkspacePage from './pages/WorkspacePage';
import RepoVisualizerPage from './pages/RepoVisualizerPage';
import InfrastructurePage from './pages/InfrastructurePage';
import { useAuth } from './hooks/useAuth';
import MatrixBackground from './components/MatrixBackground';
import RequireAuth from './components/RequireAuth';
import AuthCallbackPage from './pages/AuthCallbackPage';
import { consumeDestination } from './lib/authFlow';

function OAuthReturnRedirect() {
  const session = useAuth((s) => s.session);
  const loading = useAuth((s) => s.loading);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (loading || !session || !['/', '/auth', '/auth/callback'].includes(location.pathname)) return;
    navigate(consumeDestination(), { replace: true });
  }, [loading, location.pathname, navigate, session]);

  return null;
}

function App() {
  const initialize = useAuth((s) => s.initialize);

  useEffect(() => {
    initialize();
  }, [initialize]);

  return (
    <BrowserRouter>
      <OAuthReturnRedirect />
      <MatrixBackground />
      <Routes>
        {/* Onboarding Flow */}
        <Route path="/" element={<HeroPage />} />
        <Route path="/auth" element={<AuthPage />} />
        <Route path="/auth/callback" element={<AuthCallbackPage />} />
        <Route path="/onboarding" element={<RequireAuth><BasicInfoPage /></RequireAuth>} />
        <Route path="/connect-repo" element={<RequireAuth><ConnectRepoPage /></RequireAuth>} />
        <Route path="/invite" element={<RequireAuth><InviteCollabPage /></RequireAuth>} />
        
        {/* Protected App Layout */}
        <Route path="/app" element={<RequireAuth><AppLayout /></RequireAuth>}>
          <Route index element={<GlobalDashboardPage />} />
          <Route path="projects" element={<ProjectsPage />} />
          <Route path="project/:id" element={<DashboardPage />} />
          <Route path="workspace/:id" element={<WorkspacePage />} />
          <Route path="project/:id/graph" element={<RepoVisualizerPage />} />
          <Route path="project/:id/infra" element={<InfrastructurePage />} />
        </Route>
      </Routes>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');
        @keyframes spin { to { transform: rotate(360deg); } }
        * { scrollbar-width: thin; scrollbar-color: #1e1e2e #0a0a0f; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: #1e1e2e; border-radius: 2px; }
      `}</style>
    </BrowserRouter>
  );
}

export default App;
