import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "./AuthContext";
import type { UserRole } from "../types";

/**
 * Gates a route behind authentication and, optionally, a minimum role.
 *
 * This is a usability control, not a security one. The backend enforces the
 * same rules on every request; hiding a page here only spares the user a
 * dead end. Anything that matters is checked server-side.
 */
export default function ProtectedRoute({
  children,
  minimumRole,
}: {
  children: ReactNode;
  minimumRole?: UserRole;
}) {
  const { user, loading, hasRole } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="text-sm text-slate-500">Loading…</div>
      </div>
    );
  }

  if (!user) {
    // Remember where they were headed so sign-in can return them there.
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  if (minimumRole && !hasRole(minimumRole)) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16 text-center">
        <h2 className="text-lg font-semibold text-slate-900">
          You do not have access to this page
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          This area requires the {minimumRole.replace("_", " ").toLowerCase()} role
          or higher. Your role is {user.role.replace("_", " ").toLowerCase()}.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
