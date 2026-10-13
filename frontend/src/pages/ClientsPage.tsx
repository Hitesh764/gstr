import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createClient, getErrorMessage, listClients } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import GspConnectDialog from "../components/GspConnectDialog";
import { STATE_CLASS, STATE_LABEL } from "../gspMeta";
import type { Client, ClientGSTIN } from "../types";
import { formatDate } from "../utils/time";

export default function ClientsPage() {
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const [name, setName] = useState("");
  const [gstin, setGstin] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  // Notifications about consent link here as /?connect=<gstinId>, so the person
  // lands directly in the dialog that fixes the problem.
  const connectId = searchParams.get("connect");

  const clientsQuery = useQuery({ queryKey: ["clients"], queryFn: listClients });

  const create = useMutation({
    mutationFn: () =>
      createClient({
        name: name.trim(),
        gstins: [{ gstin: gstin.trim().toUpperCase(), is_primary: true }],
      }),
    onSuccess: () => {
      setName("");
      setGstin("");
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: (err) => setFormError(getErrorMessage(err)),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !gstin.trim()) return;
    create.mutate();
  }

  function openConnect(gstinId: string) {
    setSearchParams({ connect: gstinId });
  }

  function closeConnect() {
    setSearchParams({});
  }

  const dialogTarget = connectId
    ? clientsQuery.data
        ?.flatMap((c) => c.gstins.map((g) => ({ client: c, gstin: g })))
        .find((x) => x.gstin.id === connectId)
    : undefined;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="text-lg font-semibold text-slate-900">Clients</h1>
      <p className="mt-1 text-sm text-slate-600">
        Each client may hold several GSTINs, one per state they are registered in.
      </p>

      {hasRole("ASSOCIATE") && (
        <form
          onSubmit={onSubmit}
          className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1">
              <label htmlFor="cname" className="block text-xs font-medium text-slate-700">
                Client name
              </label>
              <input
                id="cname"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
                placeholder="Shree Ganesh Manufacturing Co."
              />
            </div>
            <div className="min-w-56 flex-1">
              <label htmlFor="cgstin" className="block text-xs font-medium text-slate-700">
                Primary GSTIN
              </label>
              <input
                id="cgstin"
                value={gstin}
                onChange={(e) => setGstin(e.target.value.toUpperCase())}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 font-mono text-sm"
                placeholder="27AAAAA0000A1Z2"
                maxLength={15}
              />
            </div>
            <button
              type="submit"
              disabled={create.isPending}
              className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            >
              {create.isPending ? "Adding…" : "Add client"}
            </button>
          </div>
          {formError && (
            <p role="alert" className="mt-3 text-sm text-rose-700">
              {formError}
            </p>
          )}
        </form>
      )}

      <div className="mt-8">
        {clientsQuery.isLoading && <p className="text-sm text-slate-500">Loading clients…</p>}
        {clientsQuery.isError && (
          <p role="alert" className="text-sm text-rose-700">
            {getErrorMessage(clientsQuery.error)}
          </p>
        )}
        {clientsQuery.data?.length === 0 && (
          <p className="text-sm text-slate-500">No clients yet. Add one above to begin.</p>
        )}

        <div className="space-y-3">
          {clientsQuery.data?.map((client) => (
            <ClientCard
              key={client.id}
              client={client}
              canConnect={hasRole("ASSOCIATE")}
              onConnect={openConnect}
            />
          ))}
        </div>
      </div>

      {dialogTarget && (
        <GspConnectDialog
          gstinId={dialogTarget.gstin.id}
          clientName={dialogTarget.client.name}
          canDisconnect={hasRole("MANAGER")}
          onClose={closeConnect}
        />
      )}
    </main>
  );
}

function ClientCard({
  client,
  canConnect,
  onConnect,
}: {
  client: Client;
  canConnect: boolean;
  onConnect: (gstinId: string) => void;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="font-medium text-slate-900">{client.name}</h2>
        <span className="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-600">
          {client.status.toLowerCase()}
        </span>
        {canConnect && (
          <span className="ml-auto flex items-center gap-3 text-sm">
            <Link
              to={`/clients/${client.id}/audit-brief`}
              className="text-slate-700 underline hover:text-slate-900"
            >
              Audit brief
            </Link>
            <Link
              to={`/notices?client=${client.id}`}
              className="text-slate-700 underline hover:text-slate-900"
            >
              Notices
            </Link>
          </span>
        )}
      </div>

      {client.gstins.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">No GSTINs registered.</p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100">
          {client.gstins.map((g) => (
            <GstinRow key={g.id} g={g} canConnect={canConnect} onConnect={onConnect} />
          ))}
        </ul>
      )}
    </div>
  );
}

function GstinRow({
  g,
  canConnect,
  onConnect,
}: {
  g: ClientGSTIN;
  canConnect: boolean;
  onConnect: (gstinId: string) => void;
}) {
  const label =
    g.gsp_state === "NONE"
      ? "Connect live GST access"
      : g.gsp_state === "EXPIRED"
        ? "Reconnect"
        : "Manage access";

  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-2 py-2 text-sm">
      <span className="font-mono text-slate-800">{g.gstin}</span>
      <span className="text-slate-500">{g.state_name ?? "—"}</span>
      {g.is_primary && (
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">primary</span>
      )}

      <span
        className={`rounded border px-2 py-0.5 text-xs font-medium ${STATE_CLASS[g.gsp_state]}`}
        title={
          g.consent_expires_at
            ? `Access ${g.gsp_state === "EXPIRED" ? "ended" : "ends"} ${formatDate(g.consent_expires_at)}`
            : "Uploads only: no live GST access"
        }
      >
        {g.gsp_state === "NONE" ? "Manual upload" : STATE_LABEL[g.gsp_state]}
      </span>

      <span className="ml-auto flex flex-wrap items-center gap-3">
        {canConnect && (
          <button
            onClick={() => onConnect(g.id)}
            className={`rounded border px-3 py-1 text-sm ${
              g.gsp_state === "EXPIRED" || g.gsp_state === "EXPIRING"
                ? "border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100"
                : "border-slate-300 text-slate-700 hover:bg-slate-50"
            }`}
          >
            {label}
          </button>
        )}
        <Link to={`/gstins/${g.id}/upload`} className="font-medium text-slate-900 underline">
          Upload data
        </Link>
      </span>
    </li>
  );
}
