import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Button } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/chat", label: "Ask AI" },
  { to: "/documents", label: "Documents" },
  { to: "/upload", label: "Upload" },
  { to: "/conversations", label: "History" },
];

export function Layout() {
  const { user, logout } = useAuth();
  return (
    <div className="min-h-screen bg-page">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6">
          <NavLink to="/" className="flex items-center gap-2 font-semibold text-ink">
            <img src="/favicon.svg" alt="" className="h-7 w-7" />
            DocuMind <span className="text-brand-600">AI</span>
          </NavLink>
          <nav className="order-3 flex w-full min-w-0 gap-1 overflow-x-auto sm:order-none sm:w-auto" aria-label="Main">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition ${
                    isActive ? "bg-brand-50 text-brand-800 ring-1 ring-brand-200" : "text-ink-muted hover:bg-slate-100 hover:text-ink"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden text-sm text-ink-muted md:inline" title={user?.email}>
              {user?.name}
              {user?.role === "admin" && (
                <span className="ml-2 rounded-full border border-brand-300 bg-brand-50 px-2 py-0.5 text-xs text-brand-800">admin</span>
              )}
            </span>
            <Button variant="secondary" onClick={logout}>Log out</Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <Outlet />
      </main>
    </div>
  );
}
