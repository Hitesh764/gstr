import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getErrorMessage,
  getNotificationPreferences,
  updateNotificationPreferences,
} from "../api/client";
import type { NotificationPreference } from "../types";

export default function NotificationSettingsPage() {
  const queryClient = useQueryClient();
  // null until the person changes something; until then the server's values are shown.
  const [draft, setDraft] = useState<NotificationPreference[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const prefsQuery = useQuery({
    queryKey: ["notifications", "preferences"],
    queryFn: getNotificationPreferences,
  });

  const rows: NotificationPreference[] = draft ?? prefsQuery.data ?? [];

  const save = useMutation({
    mutationFn: () =>
      updateNotificationPreferences(
        rows.map((p) => ({
          event_type: p.event_type,
          in_app: p.in_app,
          email: p.email,
        })),
      ),
    onSuccess: (rows) => {
      setDraft(rows);
      setSaved(true);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
      window.setTimeout(() => setSaved(false), 2500);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  function toggle(eventType: string, field: "in_app" | "email") {
    setDraft(
      rows.map((r) => (r.event_type === eventType ? { ...r, [field]: !r[field] } : r)),
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="text-lg font-semibold text-slate-900">Notification settings</h1>
      <p className="mt-1 text-sm text-slate-600">
        Choose how you hear about each kind of event. In-app notifications appear
        in the bell menu. Email is sent in addition.
      </p>

      {error && (
        <div
          role="alert"
          className="mt-4 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
        >
          {error}
        </div>
      )}

      {prefsQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
      {prefsQuery.isError && (
        <p role="alert" className="mt-6 text-sm text-rose-700">
          {getErrorMessage(prefsQuery.error)}
        </p>
      )}

      {rows.length > 0 && (
        <>
          <div className="mt-6 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-4 py-2">Event</th>
                  <th className="w-24 px-4 py-2 text-center">In app</th>
                  <th className="w-24 px-4 py-2 text-center">Email</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((pref) => (
                  <tr key={pref.event_type} className="border-b border-slate-100">
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-900">{pref.label}</div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {pref.description}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <input
                        type="checkbox"
                        checked={pref.in_app}
                        onChange={() => toggle(pref.event_type, "in_app")}
                        aria-label={`${pref.label}, in app`}
                      />
                    </td>
                    <td className="px-4 py-3 text-center">
                      <input
                        type="checkbox"
                        checked={pref.email}
                        onChange={() => toggle(pref.event_type, "email")}
                        aria-label={`${pref.label}, email`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={() => save.mutate()}
              disabled={save.isPending}
              className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            >
              {save.isPending ? "Saving…" : "Save preferences"}
            </button>
            {saved && <span className="text-sm text-emerald-700">Saved.</span>}
          </div>

          <p className="mt-6 text-xs text-slate-500">
            Emails go out through the email provider configured for this deployment. In
            local development that is the server log, so nothing is actually delivered
            until a provider is set up.
          </p>
        </>
      )}
    </main>
  );
}
