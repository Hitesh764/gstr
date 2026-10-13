import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import ClientsPage from "./pages/ClientsPage";
import UploadPage from "./pages/UploadPage";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import SignupPage from "./pages/SignupPage";
import ExceptionQueuePage from "./pages/ExceptionQueuePage";
import NotificationSettingsPage from "./pages/NotificationSettingsPage";
import NoticesPage from "./pages/NoticesPage";
import NoticeDetailPage from "./pages/NoticeDetailPage";
import PortfolioPage from "./pages/PortfolioPage";
import AuditBriefPage from "./pages/AuditBriefPage";
import AuditWorkspacePage from "./pages/AuditWorkspacePage";
import AuditEngagementPage from "./pages/AuditEngagementPage";
import GSTR1WorkspacePage from "./pages/GSTR1WorkspacePage";
import NotificationBell from "./components/NotificationBell";
import ProtectedRoute from "./auth/ProtectedRoute";
import { useAuth } from "./auth/AuthContext";

function Header() {
  const { user, firm, signOut, hasRole } = useAuth();
  if (!user) return null;

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link to="/" className="text-sm font-semibold text-slate-900">
          GST Reconciliation
        </Link>
        <nav className="flex items-center gap-4 text-sm text-slate-600">
          <Link to="/" className="hover:text-slate-900">
            Clients
          </Link>
          {hasRole("ASSOCIATE") && (
            <Link to="/portfolio" className="hover:text-slate-900">
              Portfolio
            </Link>
          )}
          {hasRole("ASSOCIATE") && (
            <Link to="/audit" className="hover:text-slate-900">
              Audit
            </Link>
          )}
          {hasRole("ASSOCIATE") && (
            <Link to="/gstr1" className="hover:text-slate-900">GSTR-1</Link>
          )}
          <Link to="/exceptions" className="hover:text-slate-900">
            Exception queue
          </Link>
          {hasRole("ASSOCIATE") && (
            <Link to="/notices" className="hover:text-slate-900">
              Notices
            </Link>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <NotificationBell />
          <div className="text-right leading-tight">
            <div className="font-medium text-slate-900">{user.name}</div>
            <div className="text-xs text-slate-500">
              {firm?.name} · {user.role.replace("_", " ").toLowerCase()}
            </div>
          </div>
          <button
            onClick={() => void signOut()}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-slate-50">
        <Header />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />

          <Route
            path="/"
            element={
              <ProtectedRoute>
                <ClientsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/portfolio"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <PortfolioPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/clients/:clientId/audit-brief"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <AuditBriefPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/audit"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <AuditWorkspacePage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/audit/:engagementId"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <AuditEngagementPage />
              </ProtectedRoute>
            }
          />
          <Route path="/gstr1" element={<ProtectedRoute minimumRole="ASSOCIATE"><GSTR1WorkspacePage /></ProtectedRoute>} />
          <Route
            path="/exceptions"
            element={
              <ProtectedRoute>
                <ExceptionQueuePage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/notices"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <NoticesPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/notices/:noticeId"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <NoticeDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/settings/notifications"
            element={
              <ProtectedRoute>
                <NotificationSettingsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/gstins/:gstinId/upload"
            element={
              <ProtectedRoute minimumRole="ASSOCIATE">
                <UploadPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/gstins/:gstinId/dashboard/:period"
            element={
              <ProtectedRoute>
                <DashboardPage />
              </ProtectedRoute>
            }
          />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}
