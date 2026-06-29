import { Link, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";

export default function Layout({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const loc = useLocation();
  const isActive = (p: string) => loc.pathname.startsWith(p);
  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-slate-900 text-slate-100 border-b border-slate-800">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-6">
            <Link to="/" className="text-lg font-semibold tracking-tight">
              QA/AQA
            </Link>
            <nav className="flex items-center gap-4 text-sm">
              <Link
                to="/workflows"
                className={
                  "hover:text-white " +
                  (isActive("/workflows") ? "text-white font-medium" : "text-slate-400")
                }
              >
                Workflows
              </Link>
              <Link
                to="/designer"
                className={
                  "hover:text-white " +
                  (isActive("/designer") || isActive("/run")
                    ? "text-white font-medium"
                    : "text-slate-400")
                }
              >
                Designer
              </Link>
              <Link
                to="/monitor"
                className={
                  "hover:text-white " +
                  (isActive("/monitor") ? "text-white font-medium" : "text-slate-400")
                }
              >
                Monitor
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-slate-400">{user?.email}</span>
            <span className="px-2 py-0.5 rounded bg-slate-800 text-xs uppercase tracking-wider">
              {user?.role}
            </span>
            <button
              onClick={logout}
              className="text-slate-300 hover:text-white underline-offset-2 hover:underline"
            >
              Logout
            </button>
          </div>
        </div>
      </header>
      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-6">{children}</main>
    </div>
  );
}
