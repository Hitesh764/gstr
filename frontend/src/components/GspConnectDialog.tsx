import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  disconnectGsp,
  getErrorMessage,
  getGspStatus,
  requestConsentOtp,
  verifyConsentOtp,
  verifyGstinLive,
} from "../api/client";
import { ATTEMPT_CLASS, STATE_CLASS, STATE_LABEL } from "../gspMeta";
import type { GspStatus, GstinInfo } from "../types";
import { formatDateTime, formatDuration, parseUtc, relativeTime } from "../utils/time";

type Step = "status" | "prepare" | "otp" | "done";

interface Props {
  gstinId: string;
  clientName: string;
  canDisconnect: boolean;
  onClose: () => void;
}

export default function GspConnectDialog({ gstinId, clientName, canDisconnect, onClose }: Props) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step | null>(null);
  // null until the person types, so the saved portal username can be shown without
  // copying it into state from an effect.
  const [usernameInput, setUsernameInput] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [attempt, setAttempt] = useState<{ id: string; expiresAt: string; message: string } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [windowNote, setWindowNote] = useState<string | null>(null);
  const [registration, setRegistration] = useState<GstinInfo | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const statusQuery = useQuery({
    queryKey: ["gsp", gstinId],
    queryFn: () => getGspStatus(gstinId),
  });
  const status = statusQuery.data;

  // Opening screen: connect if there is nothing to manage, otherwise show what
  // there is. Derived rather than set from an effect; `step` only holds the
  // person's own navigation from there.
  const username = usernameInput ?? status?.portal_username ?? "";
  const activeStep: Step | null =
    step ?? (status ? (status.state === "NONE" ? "prepare" : "status") : null);

  // Escape closes, as expected of a dialog.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // A one-second tick, only while an OTP is awaited, to drive the countdown.
  useEffect(() => {
    if (activeStep !== "otp") return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [activeStep]);

  function refreshEverywhere() {
    void queryClient.invalidateQueries({ queryKey: ["gsp", gstinId] });
    void queryClient.invalidateQueries({ queryKey: ["clients"] });
  }

  const sendOtp = useMutation({
    mutationFn: () => requestConsentOtp(gstinId, username.trim()),
    onSuccess: (res) => {
      setAttempt({ id: res.attempt_id, expiresAt: res.expires_at, message: res.message });
      setOtp("");
      setError(null);
      setStep("otp");
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const confirmOtp = useMutation({
    mutationFn: () => verifyConsentOtp(gstinId, attempt!.id, otp.trim()),
    onSuccess: (res) => {
      setWindowNote(res.window_note ?? null);
      setError(null);
      setStep("done");
      refreshEverywhere();
    },
    onError: (err) => {
      setError(getErrorMessage(err));
      // A closed request (too many wrong OTPs, or expired) cannot be retried, so
      // send the person back to request a fresh one rather than leaving them stuck.
      const message = getErrorMessage(err);
      if (/request a new|closed|expired/i.test(message)) setOtp("");
    },
  });

  const disconnect = useMutation({
    mutationFn: () => disconnectGsp(gstinId),
    onSuccess: () => {
      setConfirmDisconnect(false);
      setError(null);
      setStep("prepare");
      refreshEverywhere();
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const checkRegistration = useMutation({
    mutationFn: () => verifyGstinLive(gstinId),
    onSuccess: (info) => {
      setRegistration(info);
      setError(null);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const secondsLeft = attempt
    ? Math.max(0, Math.floor((parseUtc(attempt.expiresAt).getTime() - now) / 1000))
    : 0;
  const otpExpired = activeStep === "otp" && secondsLeft === 0;

  function submitUsername(e: FormEvent) {
    e.preventDefault();
    if (username.trim().length >= 2) sendOtp.mutate();
  }

  function submitOtp(e: FormEvent) {
    e.preventDefault();
    if (/^\d{4,8}$/.test(otp.trim()) && !otpExpired) confirmOtp.mutate();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 px-4 py-10"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !sendOtp.isPending && !confirmOtp.isPending) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="gsp-dialog-title"
        className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl"
      >
        <div className="flex items-start gap-3">
          <div className="min-w-0">
            <h2 id="gsp-dialog-title" className="text-base font-semibold text-slate-900">
              Live GST portal access
            </h2>
            <p className="mt-0.5 truncate text-sm text-slate-600">
              {clientName}
              {status && <span className="font-mono text-xs text-slate-500"> · {status.gstin}</span>}
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded px-2 py-1 text-slate-500 hover:bg-slate-100"
          >
            ✕
          </button>
        </div>

        {statusQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
        {statusQuery.isError && (
          <p role="alert" className="mt-4 text-sm text-rose-700">
            {getErrorMessage(statusQuery.error)}
          </p>
        )}

        {status?.provider_is_mock && (
          <div className="mt-4 rounded border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900">
            <strong>Demo mode.</strong> No real GST portal is contacted and no real OTP is sent.
            Enter <span className="font-mono">123456</span> when asked for the OTP.
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="mt-4 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
          >
            {error}
          </div>
        )}

        {status && activeStep === "status" && (
          <StatusView
            status={status}
            canDisconnect={canDisconnect}
            confirmDisconnect={confirmDisconnect}
            disconnecting={disconnect.isPending}
            onRenew={() => {
              setError(null);
              setStep("prepare");
            }}
            onAskDisconnect={() => setConfirmDisconnect(true)}
            onCancelDisconnect={() => setConfirmDisconnect(false)}
            onDisconnect={() => disconnect.mutate()}
          />
        )}

        {activeStep === "prepare" && (
          <form onSubmit={submitUsername} className="mt-4">
            <div className="rounded border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-700">
              <p className="font-medium text-slate-900">Before you start</p>
              <p className="mt-1">
                Access has to be switched on for this GSTIN on the GST portal itself. The
                taxpayer, or you with their login, does this once:
              </p>
              <ol className="mt-2 list-decimal space-y-0.5 pl-5">
                <li>Sign in at gst.gov.in and open <em>View Profile</em>.</li>
                <li>Under <em>Quick Links</em>, choose <em>Manage API Access</em>.</li>
                <li>Set <em>Enable API Request</em> to <strong>Yes</strong>.</li>
                <li>
                  Choose the <strong>longest duration offered</strong> (30 days). A short
                  duration means someone must reconnect before every monthly fetch.
                </li>
              </ol>
            </div>

            <label htmlFor="gst-username" className="mt-4 block text-sm font-medium text-slate-700">
              GST portal username
            </label>
            <input
              id="gst-username"
              autoFocus
              value={username}
              onChange={(e) => setUsernameInput(e.target.value)}
              autoComplete="off"
              placeholder="The username the client uses to log in to gst.gov.in"
              className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
            />
            <p className="mt-1 text-xs text-slate-500">
              An OTP will be sent by the GST portal to the mobile number or email registered
              for that user. We never see it: you type it in on the next screen.
            </p>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="submit"
                disabled={sendOtp.isPending || username.trim().length < 2}
                className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {sendOtp.isPending ? "Requesting…" : "Send OTP"}
              </button>
              <button
                type="button"
                onClick={() => checkRegistration.mutate()}
                disabled={checkRegistration.isPending}
                className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {checkRegistration.isPending ? "Checking…" : "Check this GSTIN's registration"}
              </button>
              {status?.state !== "NONE" && (
                <button
                  type="button"
                  onClick={() => setStep("status")}
                  className="ml-auto text-sm text-slate-600 underline"
                >
                  Back
                </button>
              )}
            </div>

            {registration && (
              <div
                className={`mt-3 rounded border px-3 py-2 text-sm ${
                  registration.is_active
                    ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                    : "border-amber-200 bg-amber-50 text-amber-900"
                }`}
              >
                <div className="font-medium">
                  {registration.legal_name ?? "Name not returned"}{" "}
                  <span className="font-normal">({registration.status})</span>
                </div>
                {registration.state_name && <div>{registration.state_name}</div>}
                {registration.warning && <div className="mt-1">{registration.warning}</div>}
              </div>
            )}
          </form>
        )}

        {activeStep === "otp" && attempt && (
          <form onSubmit={submitOtp} className="mt-4">
            <p className="text-sm text-slate-700">{attempt.message}</p>

            <label htmlFor="gst-otp" className="mt-4 block text-sm font-medium text-slate-700">
              OTP
            </label>
            <input
              id="gst-otp"
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={8}
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
              disabled={otpExpired}
              className="mt-1 w-40 rounded border border-slate-300 px-3 py-2 font-mono text-lg tracking-widest disabled:bg-slate-100"
            />
            <p className={`mt-1 text-xs ${otpExpired ? "text-rose-700" : "text-slate-500"}`}>
              {otpExpired
                ? "This OTP request has expired. Send a new one."
                : `Enter it within ${formatDuration(secondsLeft)}.`}
            </p>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="submit"
                disabled={confirmOtp.isPending || otpExpired || !/^\d{4,8}$/.test(otp)}
                className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {confirmOtp.isPending ? "Verifying…" : "Verify and connect"}
              </button>
              <button
                type="button"
                onClick={() => sendOtp.mutate()}
                disabled={sendOtp.isPending}
                className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {sendOtp.isPending ? "Sending…" : "Send a new OTP"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setStep("prepare");
                }}
                className="ml-auto text-sm text-slate-600 underline"
              >
                Change username
              </button>
            </div>
          </form>
        )}

        {activeStep === "done" && (
          <div className="mt-4">
            <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-3 text-sm text-emerald-900">
              <p className="font-medium">Connected.</p>
              {statusQuery.data?.session_expires_at && (
                <p className="mt-1">
                  Access lasts until {formatDateTime(statusQuery.data.session_expires_at)}. It is
                  renewed automatically every few hours until then, and GSTR-2B will be fetched on
                  the 14th of each month.
                </p>
              )}
            </div>
            {windowNote && (
              <div className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {windowNote}
              </div>
            )}
            <button
              onClick={onClose}
              className="mt-4 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
            >
              Done
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function StatusView({
  status,
  canDisconnect,
  confirmDisconnect,
  disconnecting,
  onRenew,
  onAskDisconnect,
  onCancelDisconnect,
  onDisconnect,
}: {
  status: GspStatus;
  canDisconnect: boolean;
  confirmDisconnect: boolean;
  disconnecting: boolean;
  onRenew: () => void;
  onAskDisconnect: () => void;
  onCancelDisconnect: () => void;
  onDisconnect: () => void;
}) {
  const live = status.state === "ACTIVE" || status.state === "EXPIRING";
  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded border px-2 py-0.5 text-xs font-medium ${STATE_CLASS[status.state]}`}
        >
          {STATE_LABEL[status.state]}
        </span>
        {status.connected_provider && (
          <span className="text-xs text-slate-500">via {status.connected_provider}</span>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {status.portal_username && (
          <>
            <dt className="text-slate-500">Portal user</dt>
            <dd className="font-mono text-slate-800">{status.portal_username}</dd>
          </>
        )}
        {status.granted_at && (
          <>
            <dt className="text-slate-500">Connected</dt>
            <dd className="text-slate-800">{formatDateTime(status.granted_at)}</dd>
          </>
        )}
        {status.session_expires_at && (
          <>
            <dt className="text-slate-500">{live ? "Access ends" : "Access ended"}</dt>
            <dd className="text-slate-800">
              {formatDateTime(status.session_expires_at)}
              {live && status.seconds_remaining != null && (
                <span className="text-slate-500">
                  {" "}
                  ({formatDuration(status.seconds_remaining)} left)
                </span>
              )}
            </dd>
          </>
        )}
      </dl>

      {status.state === "EXPIRED" && (
        <p className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          Automatic GSTR-2B fetching is paused for this GSTIN until access is renewed with a new
          OTP.
        </p>
      )}
      {status.renew_before_fetch_window && live && (
        <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          Access ends before the fetch window that opens on{" "}
          {formatDateTime(status.renew_before_fetch_window)}. Renew it before then, or that
          month's fetch will be skipped.
        </p>
      )}
      {status.window_note && (
        <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {status.window_note}
        </p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          onClick={onRenew}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
        >
          {live ? "Renew access" : "Reconnect"}
        </button>
        {canDisconnect && status.state !== "NONE" && !confirmDisconnect && (
          <button
            onClick={onAskDisconnect}
            className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50"
          >
            Disconnect
          </button>
        )}
      </div>

      {confirmDisconnect && (
        <div className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-3 text-sm text-rose-900">
          <p>
            Stop fetching GSTR-2B automatically for this GSTIN and discard the stored access?
            The GST portal session itself runs until it expires; the client can end it sooner
            under <em>Manage API Access</em>.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              onClick={onDisconnect}
              disabled={disconnecting}
              className="rounded bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50"
            >
              {disconnecting ? "Disconnecting…" : "Yes, disconnect"}
            </button>
            <button
              onClick={onCancelDisconnect}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-white"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {status.recent_attempts.length > 0 && (
        <div className="mt-5">
          <h3 className="text-sm font-semibold text-slate-900">Recent fetches</h3>
          <ul className="mt-2 divide-y divide-slate-100 rounded border border-slate-200">
            {status.recent_attempts.map((a) => (
              <li key={a.id} className="px-3 py-2 text-sm">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-mono text-xs text-slate-500">{a.period}</span>
                  <span className={`font-medium ${ATTEMPT_CLASS[a.status]}`}>
                    {a.status.toLowerCase()}
                  </span>
                  <span className="text-xs text-slate-400">
                    {a.trigger.toLowerCase()} · {relativeTime(a.created_at)}
                  </span>
                </div>
                {a.message && <p className="mt-0.5 text-xs text-slate-600">{a.message}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
