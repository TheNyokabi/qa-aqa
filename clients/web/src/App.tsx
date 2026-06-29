import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./lib/auth";
import LoginPage from "./pages/LoginPage";
import WorkflowsListPage from "./pages/WorkflowsListPage";
import WorkflowDetailPage from "./pages/WorkflowDetailPage";
import DesignerPage from "./pages/DesignerPage";
import WorkflowRunPage from "./pages/WorkflowRunPage";
import MonitorPage from "./pages/MonitorPage";
import Layout from "./components/Layout";

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="p-8 text-slate-500">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <Layout>{children}</Layout>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<Protected><Navigate to="/workflows" replace /></Protected>} />
      <Route path="/workflows" element={<Protected><WorkflowsListPage /></Protected>} />
      <Route path="/workflows/*" element={<Protected><WorkflowDetailPage /></Protected>} />
      <Route path="/designer" element={<Protected><DesignerPage /></Protected>} />
      <Route path="/run/*" element={<Protected><WorkflowRunPage /></Protected>} />
      <Route path="/monitor" element={<Protected><MonitorPage /></Protected>} />
    </Routes>
  );
}
