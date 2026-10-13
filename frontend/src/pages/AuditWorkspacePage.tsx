import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createAuditEngagement, getAuditDashboard, getErrorMessage, listAuditEngagements, listClients, listFirmUsers } from "../api/client";
import type { AuditEngagement } from "../types";
import { useAuth } from "../auth/AuthContext";

const STATUS_STYLES: Record<string, string> = {
  PLANNING: "bg-sky-50 text-sky-800 ring-sky-200",
  FIELDWORK: "bg-amber-50 text-amber-800 ring-amber-200",
  COMPLETION: "bg-violet-50 text-violet-800 ring-violet-200",
  REPORTING: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  CLOSED: "bg-slate-100 text-slate-700 ring-slate-200",
};

export default function AuditWorkspacePage() {
  const navigate = useNavigate();
  const { hasRole } = useAuth();
  const queryClient = useQueryClient();
  const [clientId, setClientId] = useState("");
  const [financialYear, setFinancialYear] = useState(() => {
    const now = new Date();
    const firstYear = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
    return `${firstYear}-${String((firstYear + 1) % 100).padStart(2, "0")}`;
  });
  const [partnerId, setPartnerId] = useState("");
  const [reportingDeadline, setReportingDeadline] = useState("");
  const [engagementType, setEngagementType] = useState<"STATUTORY" | "TAX" | "INTERNAL">("STATUTORY");
  const clients = useQuery({ queryKey: ["clients"], queryFn: listClients });
  const users = useQuery({ queryKey: ["firm-users"], queryFn: listFirmUsers });
  const engagements = useQuery({ queryKey: ["audit-engagements"], queryFn: () => listAuditEngagements() });
  const dashboard = useQuery({ queryKey: ["audit-dashboard"], queryFn: getAuditDashboard });
  const create = useMutation({
    mutationFn: createAuditEngagement,
    onSuccess: async (row) => {
      await queryClient.invalidateQueries({ queryKey: ["audit-engagements"] });
      navigate(`/audit/${row.id}`);
    },
  });
  const names = new Map((clients.data ?? []).map((client) => [client.id, client.name]));
  const partnerChoices = (users.data ?? []).filter((user) => user.is_active && user.role === "PARTNER");
  const canCreate = hasRole("MANAGER");

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-teal-700">Audit workspace</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">Engagements</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-600">Prepare audit evidence, review system observations, and record partner decisions in one engagement file.</p>
        </div>
        <Link to="/portfolio" className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">Client portfolio</Link>
      </div>

      {canCreate && <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-base font-semibold text-slate-900">Start an engagement</h2>
        <p className="mt-1 text-sm text-slate-600">Choose an existing client and an assigned engagement partner.</p>
        <form className="mt-4 grid gap-3 md:grid-cols-4" onSubmit={(event) => {
          event.preventDefault();
          create.mutate({ client_id: clientId, financial_year: financialYear, engagement_type: engagementType, engagement_partner_id: partnerId, reporting_deadline: reportingDeadline || undefined });
        }}>
          <label className="text-xs font-medium text-slate-600">Client
            <select required value={clientId} onChange={(e) => setClientId(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
              <option value="">Select client</option>{(clients.data ?? []).map((client) => <option key={client.id} value={client.id}>{client.name}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium text-slate-600">Financial year
            <input required pattern="20\d{2}-\d{2}" value={financialYear} onChange={(e) => setFinancialYear(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="2025-26" />
          </label>
          <label className="text-xs font-medium text-slate-600">Engagement type
            <select value={engagementType} onChange={(e) => setEngagementType(e.target.value as typeof engagementType)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
              <option value="STATUTORY">Statutory audit</option><option value="TAX">Tax audit</option><option value="INTERNAL">Internal audit</option>
            </select>
          </label>
          <label className="text-xs font-medium text-slate-600">Engagement partner
            <select required value={partnerId} onChange={(e) => setPartnerId(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
              <option value="">Select partner</option>{partnerChoices.map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium text-slate-600">Reporting deadline (optional)
            <input type="date" value={reportingDeadline} onChange={(e) => setReportingDeadline(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" />
          </label>
          {create.isError && <p role="alert" className="text-sm text-rose-700 md:col-span-4">{getErrorMessage(create.error)}</p>}
          <div className="md:col-span-4"><button disabled={create.isPending || !clientId || !partnerId} className="rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white hover:bg-teal-900 disabled:opacity-50">{create.isPending ? "Creating…" : "Create engagement"}</button></div>
        </form>
      </section>}

      <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[{label:"Active engagements", value:dashboard.data?.active_count ?? 0}, {label:"Reporting within 30 days", value:dashboard.data?.due_soon_count ?? 0}, {label:"Staff with assigned work", value:dashboard.data?.workload_by_staff?.length ?? 0}, {label:"Open risk flags", value:dashboard.data?.active_engagements.reduce((n, row) => n + row.open_risk_flags, 0) ?? 0}].map((card) => <div key={card.label} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><p className="text-xs font-medium text-slate-500">{card.label}</p><p className="mt-2 text-2xl font-semibold text-slate-900">{card.value}</p></div>)}
      </section>
      <section className="mt-3 grid gap-3 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><h2 className="text-sm font-semibold text-slate-900">Staff workload</h2>
          {dashboard.data?.workload_by_staff.length ? <ul className="mt-3 space-y-2">{dashboard.data.workload_by_staff.map((row) => <li key={row.staff_id} className="flex justify-between gap-3 text-sm"><span className="text-slate-700">{row.staff_name ?? "Staff member"} <span className="text-xs text-slate-400">{row.role}</span></span><span className="font-medium text-slate-900">{row.engagement_count}</span></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">No active workload assigned.</p>}
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><h2 className="text-sm font-semibold text-slate-900">Upcoming deadlines</h2>
          {dashboard.data?.active_engagements.filter((row) => row.days_until_deadline !== null && row.days_until_deadline !== undefined && row.days_until_deadline <= 30).length ? <ul className="mt-3 space-y-2">{dashboard.data.active_engagements.filter((row) => row.days_until_deadline !== null && row.days_until_deadline !== undefined && row.days_until_deadline <= 30).map((row) => <li key={row.id}><Link to={`/audit/${row.id}`} className="flex justify-between gap-3 text-sm hover:text-teal-800"><span className="text-slate-700">{row.client_name} · FY {row.financial_year}</span><span className={`font-semibold ${(row.days_until_deadline ?? 0) < 0 ? "text-rose-700" : "text-amber-700"}`}>{row.days_until_deadline} days</span></Link></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">No engagement deadlines within 30 days.</p>}
        </div>
      </section>

      <section className="mt-7">
        <div className="mb-3 flex items-center justify-between"><h2 className="text-base font-semibold text-slate-900">Firm engagements</h2><span className="text-xs text-slate-500">{engagements.data?.length ?? 0} shown</span></div>
        {engagements.isLoading && <p className="text-sm text-slate-500">Loading engagements…</p>}
        {engagements.isError && <p role="alert" className="text-sm text-rose-700">{getErrorMessage(engagements.error)}</p>}
        {engagements.data && engagements.data.length === 0 && <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-600">No audit engagements yet. Create one above to start the planning file.</div>}
        {!!engagements.data?.length && <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="grid grid-cols-[1.6fr_0.8fr_0.8fr_1fr_auto] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-[11px] font-semibold uppercase tracking-wide text-slate-500"><span>Client / engagement</span><span>Financial year</span><span>Type</span><span>Status</span><span /></div>
          {engagements.data.map((row) => <EngagementRow key={row.id} row={row} clientName={names.get(row.client_id) ?? "Client"} onOpen={() => navigate(`/audit/${row.id}`)} />)}
        </div>}
      </section>
    </main>
  );
}

function EngagementRow({ row, clientName, onOpen }: { row: AuditEngagement; clientName: string; onOpen: () => void }) {
  return <button onClick={onOpen} className="grid w-full grid-cols-[1.6fr_0.8fr_0.8fr_1fr_auto] items-center gap-3 border-b border-slate-100 px-4 py-3 text-left last:border-0 hover:bg-teal-50/40">
    <span><span className="block text-sm font-semibold text-slate-900">{clientName}</span><span className="mt-0.5 block font-mono text-[10px] text-slate-400">{row.id}</span></span>
    <span className="text-sm text-slate-700">FY {row.financial_year}</span><span className="text-sm capitalize text-slate-700">{row.engagement_type.toLowerCase()}</span>
    <span><span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ${STATUS_STYLES[row.status] ?? STATUS_STYLES.PLANNING}`}>{row.status}</span></span>
    <span className="text-sm font-semibold text-teal-800">Open <span aria-hidden="true">→</span></span>
  </button>;
}
