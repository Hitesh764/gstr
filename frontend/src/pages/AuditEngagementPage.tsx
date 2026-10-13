import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "../api/client";
import type { AuditEngagement, AuditRatioAnalysis, CAROClauseResponse, RelatedParty } from "../types";
import { useAuth } from "../auth/AuthContext";

type Tab = "overview" | "planning" | "evidence" | "bank" | "ratios" | "related-parties" | "caro" | "documents";
const panel = "rounded-xl border border-slate-200 bg-white shadow-sm";

export default function AuditEngagementPage() {
  const { engagementId = "" } = useParams();
  const { user, hasRole } = useAuth();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("overview");
  const engagement = useQuery({ queryKey: ["audit-engagement", engagementId], queryFn: () => api.getAuditEngagement(engagementId), enabled: !!engagementId });
  const client = useQuery({ queryKey: ["client", engagement.data?.client_id], queryFn: () => api.getClient(engagement.data!.client_id), enabled: !!engagement.data?.client_id });
  const parties = useQuery({ queryKey: ["related-parties", engagement.data?.client_id], queryFn: () => api.listRelatedParties(engagement.data!.client_id), enabled: !!engagement.data?.client_id });
  const responses = useQuery({ queryKey: ["caro-responses", engagementId], queryFn: () => api.listCaroResponses(engagementId), enabled: !!engagementId });
  const riskFlags = useQuery({ queryKey: ["audit-risk-flags", engagementId], queryFn: () => api.listAuditRiskFlags(engagementId), enabled: !!engagementId });
  const confirmations = useQuery({ queryKey: ["audit-bank-confirmations", engagementId], queryFn: () => api.listBankConfirmations(engagementId), enabled: !!engagementId });
  const refreshEngagement = () => queryClient.invalidateQueries({ queryKey: ["audit-engagement", engagementId] });
  const refreshParties = () => queryClient.invalidateQueries({ queryKey: ["related-parties", engagement.data?.client_id] });
  const refreshCaro = () => queryClient.invalidateQueries({ queryKey: ["caro-responses", engagementId] });
  const detail = engagement.data;
  const activeResponses = useMemo(() => (responses.data ?? []).filter((response) => response.status !== "SUPERSEDED"), [responses.data]);

  const generate = useMutation({ mutationFn: () => api.generateCaroDrafts(engagementId), onSuccess: refreshCaro });
  const startFieldwork = useMutation({ mutationFn: () => api.startAuditFieldwork(engagementId), onSuccess: refreshEngagement });

  if (engagement.isLoading) return <main className="mx-auto max-w-6xl px-4 py-10 text-sm text-slate-500">Loading engagement…</main>;
  if (engagement.isError || !detail) return <main className="mx-auto max-w-6xl px-4 py-10"><Link to="/audit" className="text-sm text-teal-800 underline">← Audit engagements</Link><p role="alert" className="mt-4 text-sm text-rose-700">{api.getErrorMessage(engagement.error)}</p></main>;

  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" }, { id: "planning", label: "Planning gates" },
    { id: "evidence", label: "Evidence & risks" },
    { id: "bank", label: "Bank confirmations" }, { id: "ratios", label: "Ratio analysis" },
    { id: "related-parties", label: "Related parties" }, { id: "caro", label: "CARO responses" },
    { id: "documents", label: "MRL & working papers" },
  ];
  const confirmedPlan = !!detail.materiality_confirmed_by && !!detail.caro_confirmed_by;

  return <main className="mx-auto max-w-6xl px-4 py-8">
    <Link to="/audit" className="text-sm font-medium text-teal-800 underline decoration-teal-300 underline-offset-4">← Audit engagements</Link>
    <header className="mt-4 flex flex-wrap items-start justify-between gap-4">
      <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-teal-700">{client.data?.name ?? "Audit engagement"}</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">FY {detail.financial_year} <span className="font-normal text-slate-500">· {detail.engagement_type.toLowerCase()} audit</span></h1>
        <p className="mt-1 font-mono text-[11px] text-slate-400">Engagement {detail.id}</p></div>
      <div className="flex flex-wrap gap-2">
        <StatusPill status={detail.status} />
        <span className={`rounded-full px-3 py-1.5 text-xs font-semibold ring-1 ${detail.caro_applicable === true ? "bg-teal-50 text-teal-800 ring-teal-200" : detail.caro_applicable === false ? "bg-slate-100 text-slate-600 ring-slate-200" : "bg-amber-50 text-amber-800 ring-amber-200"}`}>
          {detail.caro_applicable === true ? "CARO applicable" : detail.caro_applicable === false ? "CARO not applicable" : "CARO scope unconfirmed"}
        </span>
      </div>
    </header>

    <nav aria-label="Audit sections" className="mt-6 flex gap-1 overflow-x-auto border-b border-slate-200">
      {tabs.map((item) => <button key={item.id} onClick={() => setTab(item.id)} className={`whitespace-nowrap border-b-2 px-4 py-3 text-sm font-medium ${tab === item.id ? "border-teal-700 text-teal-900" : "border-transparent text-slate-500 hover:text-slate-800"}`}>{item.label}</button>)}
    </nav>

    <div className="mt-6">
      {tab === "overview" && <OverviewTab engagement={detail} clientName={client.data?.name ?? "—"} responses={activeResponses} partyCount={parties.data?.filter((p) => p.is_active).length ?? 0} openRiskCount={(riskFlags.data ?? []).filter((f) => ["SYSTEM_FLAGGED", "UNDER_REVIEW"].includes(f.status)).length} highRiskCount={(riskFlags.data ?? []).filter((f) => ["HIGH", "CRITICAL"].includes(f.severity.toUpperCase()) && ["SYSTEM_FLAGGED", "UNDER_REVIEW"].includes(f.status)).length} bankOutstanding={(confirmations.data ?? []).filter((b) => ["DRAFTED", "SENT", "PENDING"].includes(b.letter_status)).length} onGo={setTab} />}
      {tab === "planning" && <PlanningTab engagement={detail} hasRole={hasRole} onChanged={refreshEngagement} onStart={() => startFieldwork.mutate()} starting={startFieldwork.isPending} startError={startFieldwork.isError ? api.getErrorMessage(startFieldwork.error) : ""} />}
      {tab === "evidence" && <EvidenceTab engagement={detail} canUpload={hasRole("ASSOCIATE")} />}
      {tab === "bank" && <BankTab engagement={detail} />}
      {tab === "ratios" && <RatiosTab engagement={detail} />}
      {tab === "related-parties" && detail.client_id && <RelatedPartiesTab clientId={detail.client_id} rows={parties.data ?? []} isLoading={parties.isLoading} error={parties.isError ? api.getErrorMessage(parties.error) : ""} canManage={hasRole("MANAGER")} onChanged={refreshParties} />}
      {tab === "caro" && <CaroTab engagement={detail} responses={activeResponses} loading={responses.isLoading} error={responses.isError ? api.getErrorMessage(responses.error) : ""} canAssociate={hasRole("ASSOCIATE")} canManage={hasRole("MANAGER") && user?.id !== detail.engagement_partner_id} canPartner={hasRole("PARTNER")} currentUserId={user?.id ?? ""} onGenerate={() => generate.mutate()} generating={generate.isPending} generateError={generate.isError ? api.getErrorMessage(generate.error) : ""} onChanged={refreshCaro} />}
      {tab === "documents" && <AuditDocumentsTab engagementId={detail.id} engagementStatus={detail.status} partnerId={detail.engagement_partner_id} userId={user?.id ?? ""} canAssociate={hasRole("ASSOCIATE")} canManage={hasRole("MANAGER")} canPartner={hasRole("PARTNER")} />}
    </div>
    {startFieldwork.isSuccess && <p role="status" className="sr-only">Fieldwork started.</p>}
    {!confirmedPlan && detail.status === "PLANNING" && <p className="mt-5 rounded-lg bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-900">Planning confirmations are outstanding. Fieldwork cannot start until the assigned partner confirms materiality and CARO applicability.</p>}
  </main>;
}

function AuditDocumentsTab({ engagementId, engagementStatus, partnerId, userId, canAssociate, canManage, canPartner }: {
  engagementId: string; engagementStatus: string; partnerId: string; userId: string; canAssociate: boolean; canManage: boolean; canPartner: boolean;
}) {
  const qc = useQueryClient();
  const letters = useQuery({ queryKey: ["audit-mrl", engagementId], queryFn: () => api.listManagementRepLetters(engagementId) });
  const papers = useQuery({ queryKey: ["audit-working-papers", engagementId], queryFn: () => api.listAuditWorkingPapers(engagementId) });
  const procedures = useQuery({ queryKey: ["audit-procedures", engagementId], queryFn: () => api.listAuditProcedures(engagementId) });
  const [content, setContent] = useState("");
  const [procedureName, setProcedureName] = useState(""); const [procedureSA, setProcedureSA] = useState("SA 500");
  const [procedureNotes, setProcedureNotes] = useState(""); const [evidenceRefs, setEvidenceRefs] = useState("");
  const refresh = async () => { await qc.invalidateQueries({queryKey: ["audit-mrl", engagementId]}); await qc.invalidateQueries({queryKey: ["audit-working-papers", engagementId]}); await qc.invalidateQueries({queryKey: ["audit-procedures", engagementId]}); };
  const generateLetter = useMutation({mutationFn: () => api.generateManagementRepLetter(engagementId), onSuccess: refresh});
  const editLetter = useMutation({mutationFn: ({id, value}: {id: string; value: string}) => api.editManagementRepLetter(engagementId, id, value), onSuccess: refresh});
  const reviewLetter = useMutation({mutationFn: ({id, decision}: {id: string; decision: "approve" | "rework"}) => api.reviewManagementRepLetter(engagementId, id, decision), onSuccess: refresh});
  const approveLetter = useMutation({mutationFn: (id: string) => api.approveManagementRepLetter(engagementId, id), onSuccess: refresh});
  const markLetter = useMutation({mutationFn: ({id, action}: {id: string; action: "send" | "receive"}) => api.markManagementRepLetter(engagementId, id, action), onSuccess: refresh});
  const compile = useMutation({mutationFn: () => api.compileAuditWorkingPaper(engagementId), onSuccess: refresh});
  const reviewPaper = useMutation({mutationFn: ({id, decision}: {id: string; decision: "approve" | "rework"}) => api.reviewAuditWorkingPaper(engagementId, id, decision), onSuccess: refresh});
  const approvePaper = useMutation({mutationFn: (id: string) => api.approveAuditWorkingPaper(engagementId, id), onSuccess: refresh});
  const createProcedure = useMutation({mutationFn: () => api.createAuditProcedure(engagementId, {sa_reference: procedureSA, procedure_name: procedureName}), onSuccess: async () => { setProcedureName(""); await refresh(); }});
  const performProcedure = useMutation({mutationFn: (id: string) => api.performAuditProcedure(engagementId, id, {conclusion_notes: procedureNotes, evidence_file_paths: evidenceRefs.split("\n").map((s) => s.trim()).filter(Boolean)}), onSuccess: async () => {setProcedureNotes(""); setEvidenceRefs(""); await refresh();}});
  const reviewProcedure = useMutation({mutationFn: ({id, decision}: {id: string; decision: "approve" | "rework"}) => api.reviewAuditProcedure(engagementId, id, decision), onSuccess: refresh});
  const approveProcedure = useMutation({mutationFn: (id: string) => api.approveAuditProcedure(engagementId, id), onSuccess: refresh});
  const download = useMutation({mutationFn: () => api.exportAuditPackage(engagementId), onSuccess: (blob) => { const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = `audit-package-${engagementId}.zip`; a.click(); URL.revokeObjectURL(url); }});
  const advance = useMutation({mutationFn: (target: "completion" | "reporting" | "close") => api.advanceAuditEngagement(engagementId, target), onSuccess: async () => { await qc.invalidateQueries({queryKey: ["audit-engagement", engagementId]}); await qc.invalidateQueries({queryKey: ["audit-engagements"]}); }});
  const letter = letters.data?.[0]; const paper = papers.data?.[0]; const isAssignedPartner = canPartner && userId === partnerId;
  return <div className="space-y-5">
    <section className={`${panel} p-5`}><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-slate-900">Audit procedures and evidence references</h2><p className="mt-1 text-sm text-slate-600">The preparer records work and CA-authored notes; each procedure needs separate manager review and assigned-partner approval.</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700">{procedures.data?.length ?? 0} procedures</span></div>
      {canAssociate && <form className="mt-4 grid gap-3 sm:grid-cols-[130px_1fr_auto]" onSubmit={(e) => {e.preventDefault(); createProcedure.mutate();}}><label className="text-xs font-medium text-slate-600">SA reference<input value={procedureSA} onChange={(e) => setProcedureSA(e.target.value)} required className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" /></label><label className="text-xs font-medium text-slate-600">Procedure name<input value={procedureName} onChange={(e) => setProcedureName(e.target.value)} required className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="e.g. Obtain and inspect bank confirmation" /></label><button className="self-end rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Add procedure</button></form>}
      <div className="mt-4 space-y-3">{(procedures.data ?? []).map((procedure) => <div key={procedure.id} className="rounded-lg border border-slate-200 p-4"><div className="flex flex-wrap justify-between gap-2"><div><p className="text-sm font-semibold text-slate-900">{procedure.procedure_name}</p><p className="mt-1 text-xs text-slate-500">{procedure.sa_reference}</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700">{procedure.status}</span></div>
        {procedure.status === "PLANNED" || procedure.status === "REWORK" ? canAssociate && userId === procedure.performed_by && <div className="mt-3 grid gap-2"><textarea value={procedureNotes} onChange={(e) => setProcedureNotes(e.target.value)} className="min-h-20 rounded-lg border border-slate-300 p-2 text-sm" placeholder="CA/associate-authored work performed and observations"/><textarea value={evidenceRefs} onChange={(e) => setEvidenceRefs(e.target.value)} className="min-h-16 rounded-lg border border-slate-300 p-2 text-sm" placeholder="Evidence references, one per line"/><button onClick={() => performProcedure.mutate(procedure.id)} disabled={!procedureNotes.trim()} className="w-fit rounded-lg border border-teal-700 px-3 py-2 text-sm font-semibold text-teal-800 disabled:opacity-50">Record performed work</button></div> : null}
        {procedure.conclusion_notes && <p className="mt-3 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">{procedure.conclusion_notes}</p>}
        {procedure.status === "PERFORMED" && canManage && userId !== procedure.performed_by && <div className="mt-3 flex gap-2"><button onClick={() => reviewProcedure.mutate({id: procedure.id, decision: "approve"})} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Manager review</button><button onClick={() => reviewProcedure.mutate({id: procedure.id, decision: "rework"})} className="rounded-lg border border-slate-300 px-3 py-2 text-sm">Return for rework</button></div>}
        {procedure.status === "MANAGER_REVIEWED" && isAssignedPartner && userId !== procedure.performed_by && userId !== procedure.reviewed_by && <button onClick={() => approveProcedure.mutate(procedure.id)} className="mt-3 rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Partner approve</button>}
      </div>)}</div>
    </section>
    <section className={`${panel} p-5`}><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-slate-900">Management representation letter · SA 580</h2><p className="mt-1 text-sm text-slate-600">Editable draft only. CA reviews and explicitly sends it; generation never contacts the client.</p></div>{canAssociate && <button onClick={() => generateLetter.mutate()} disabled={generateLetter.isPending} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Generate draft</button>}</div>
      {letter ? <div className="mt-4 rounded-lg border border-slate-200 p-4"><div className="flex items-center justify-between"><span className="text-sm font-semibold">Version {letter.version}</span><span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-800">{letter.status}</span></div><pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-amber-50 p-3 text-xs leading-5 text-amber-950">{letter.draft_content}</pre>
        {letter.status === "DRAFT" && canAssociate && userId === letter.prepared_by && <><textarea aria-label="CA edited MRL" value={content || letter.final_content || ""} onChange={(e) => setContent(e.target.value)} className="mt-3 min-h-44 w-full rounded-lg border border-slate-300 p-3 text-sm" placeholder="Review and edit the representation letter before submitting for manager review."/><button onClick={() => editLetter.mutate({id: letter.id, value: content || letter.final_content || ""})} className="mt-2 rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold">Save CA edits</button></>}
        {letter.final_content && <details className="mt-3"><summary className="cursor-pointer text-sm font-medium text-teal-800">View CA-edited content</summary><pre className="mt-2 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs">{letter.final_content}</pre></details>}
        {letter.status === "DRAFT" && canManage && userId !== letter.prepared_by && letter.final_content && <div className="mt-3 flex gap-2"><button onClick={() => reviewLetter.mutate({id: letter.id, decision: "approve"})} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Manager review</button><button onClick={() => reviewLetter.mutate({id: letter.id, decision: "rework"})} className="rounded-lg border border-slate-300 px-3 py-2 text-sm">Return for rework</button></div>}
        {letter.status === "MANAGER_REVIEWED" && isAssignedPartner && userId !== letter.prepared_by && <button onClick={() => approveLetter.mutate(letter.id)} className="mt-3 rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Partner approve</button>}
        {letter.status === "PARTNER_APPROVED" && isAssignedPartner && <button onClick={() => markLetter.mutate({id: letter.id, action: "send"})} className="mt-3 rounded-lg border border-teal-700 px-3 py-2 text-sm font-semibold text-teal-800">Record explicit send to client</button>}
        {letter.status === "SENT" && canAssociate && <button onClick={() => markLetter.mutate({id: letter.id, action: "receive"})} className="mt-3 rounded-lg border border-slate-300 px-3 py-2 text-sm">Record signed receipt</button>}
      </div> : <p className="mt-4 text-sm text-slate-500">No MRL draft generated.</p>}</section>
    <section className={`${panel} p-5`}><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-slate-900">Working-paper compilation</h2><p className="mt-1 text-sm text-slate-600">Compilation remains a draft until manager review and assigned-partner approval.</p></div><div className="flex gap-2">{canAssociate && <button onClick={() => compile.mutate()} disabled={compile.isPending} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Compile latest file</button>}<button onClick={() => download.mutate()} disabled={!paper || paper.status !== "PARTNER_APPROVED" || !isAssignedPartner || download.isPending} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold disabled:opacity-40">Export approved package</button></div></div>
      {paper ? <div className="mt-4 rounded-lg border border-slate-200 p-4"><div className="flex flex-wrap justify-between gap-2"><div><p className="text-sm font-semibold">{paper.title} · v{paper.version}</p><p className="mt-1 text-xs text-slate-500">{paper.sa_reference}</p></div><span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-800">{paper.status}</span></div><pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs leading-5">{paper.content}</pre>
        {paper.status === "DRAFT" && canManage && userId !== paper.prepared_by && <div className="mt-3 flex gap-2"><button onClick={() => reviewPaper.mutate({id: paper.id, decision: "approve"})} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Manager review</button><button onClick={() => reviewPaper.mutate({id: paper.id, decision: "rework"})} className="rounded-lg border border-slate-300 px-3 py-2 text-sm">Return for rework</button></div>}
        {paper.status === "MANAGER_REVIEWED" && isAssignedPartner && userId !== paper.prepared_by && userId !== paper.reviewed_by && <button onClick={() => approvePaper.mutate(paper.id)} className="mt-3 rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Partner approve working paper</button>}
      </div> : <p className="mt-4 text-sm text-slate-500">No working-paper compilation yet.</p>}
      {download.isError && <p role="alert" className="mt-3 text-sm text-rose-700">{api.getErrorMessage(download.error)}</p>}
      {advance.isError && <p role="alert" className="mt-3 text-sm text-rose-700">{api.getErrorMessage(advance.error)}</p>}
      {isAssignedPartner && <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-100 pt-4">
        {engagementStatus === "FIELDWORK" && <button onClick={() => advance.mutate("completion")} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold">Partner move to Completion</button>}
        {engagementStatus === "COMPLETION" && <button onClick={() => advance.mutate("reporting")} className="rounded-lg border border-teal-700 px-3 py-2 text-sm font-semibold text-teal-800">Partner move to Reporting</button>}
        {engagementStatus === "REPORTING" && <button onClick={() => advance.mutate("close")} className="rounded-lg border border-rose-300 px-3 py-2 text-sm font-semibold text-rose-800">Partner close engagement</button>}
      </div>}
    </section>
  </div>;
}

function StatusPill({ status }: { status: string }) {
  const styles: Record<string, string> = { PLANNING: "bg-sky-50 text-sky-800 ring-sky-200", FIELDWORK: "bg-amber-50 text-amber-800 ring-amber-200", COMPLETION: "bg-violet-50 text-violet-800 ring-violet-200", REPORTING: "bg-emerald-50 text-emerald-800 ring-emerald-200", CLOSED: "bg-slate-100 text-slate-700 ring-slate-200" };
  return <span className={`rounded-full px-3 py-1.5 text-xs font-semibold ring-1 ${styles[status] ?? styles.PLANNING}`}>{status}</span>;
}

function OverviewTab({ engagement, clientName, responses, partyCount, openRiskCount, highRiskCount, bankOutstanding, onGo }: { engagement: AuditEngagement; clientName: string; responses: CAROClauseResponse[]; partyCount: number; openRiskCount: number; highRiskCount: number; bankOutstanding: number; onGo: (tab: Tab) => void }) {
  const finalized = responses.filter((response) => response.status === "FINALIZED").length;
  const draftCount = responses.filter((response) => response.status !== "FINALIZED").length;
  const cards = [
    { label: "Open system risk flags", value: `${openRiskCount} (${highRiskCount} high)`, action: "Review evidence", tab: "evidence" as const },
    { label: "CARO clauses finalized", value: `${finalized} / ${responses.length}`, action: "Open CARO workflow", tab: "caro" as const },
    { label: "Bank confirmations outstanding", value: String(bankOutstanding), action: "Track confirmations", tab: "bank" as const },
    { label: "Active related parties", value: String(partyCount), action: "Review master list", tab: "related-parties" as const },
    { label: "Materiality", value: engagement.materiality_confirmed_by ? `₹${Number(engagement.materiality_amount ?? 0).toLocaleString("en-IN")}` : "Awaiting partner", action: "Review planning", tab: "planning" as const },
    { label: "CARO scope", value: engagement.caro_confirmed_by ? (engagement.caro_applicable ? "Confirmed applicable" : "Confirmed not applicable") : "Awaiting partner", action: "Review planning", tab: "planning" as const },
  ];
  return <div className="space-y-5">
    <section className={`${panel} p-5`}><h2 className="text-base font-semibold text-slate-900">Engagement file</h2><p className="mt-1 text-sm text-slate-600">Current stage: {engagement.status}. Human review and partner sign-off are recorded separately from system-generated observations.</p>
      <dl className="mt-4 grid gap-4 sm:grid-cols-4"><Info label="Client" value={clientName} /><Info label="Financial year" value={engagement.financial_year} /><Info label="Engagement type" value={engagement.engagement_type} /><Info label="Days until deadline" value={engagement.reporting_deadline ? String(Math.ceil((new Date(`${engagement.reporting_deadline}T00:00:00`).getTime() - new Date().setHours(0,0,0,0)) / 86400000)) : "Not set"} /></dl>
    </section>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{cards.map((card) => <button key={card.label} onClick={() => onGo(card.tab)} className="rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm hover:border-teal-300"><span className="text-xs font-medium text-slate-500">{card.label}</span><span className="mt-2 block text-xl font-semibold text-slate-900">{card.value}</span><span className="mt-3 block text-xs font-semibold text-teal-800">{card.action} →</span></button>)}</div>
    <section className={`${panel} p-5`}><div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="text-base font-semibold text-slate-900">Review queue</h2><p className="mt-1 text-sm text-slate-600">CARO drafts requiring associate edits, manager review, or partner finalization.</p></div><button onClick={() => onGo("caro")} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white hover:bg-teal-900">Open queue</button></div>
      {responses.length === 0 ? <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">No CARO clause drafts have been generated.</p> : <div className="mt-4 grid gap-2 sm:grid-cols-3"><Info label="Draft or in review" value={String(draftCount)} /><Info label="Partner finalized" value={String(finalized)} /><Info label="Current clause responses" value={String(responses.length)} /></div>}
    </section>
  </div>;
}

function Info({ label, value }: { label: string; value: string }) { return <div><dt className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</dt><dd className="mt-1 text-sm font-medium text-slate-900">{value}</dd></div>; }

function PlanningTab({ engagement, hasRole, onChanged, onStart, starting, startError }: {
  engagement: AuditEngagement; hasRole: (role: "ASSOCIATE" | "MANAGER" | "PARTNER") => boolean;
  onChanged: () => void; onStart: () => void; starting: boolean; startError: string;
}) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [turnover, setTurnover] = useState("");
  const [pbt, setPbt] = useState("");
  const [netWorth, setNetWorth] = useState("");
  const [suggestion, setSuggestion] = useState<{ suggested_amount: string; basis: string; percentage: string } | null>(null);
  const [confirmAmount, setConfirmAmount] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [caroInputs, setCaroInputs] = useState({
    is_banking_company: false, is_insurance_company: false, is_section_8_company: false,
    is_one_person_company: false, is_small_company: false, is_private_company: false,
    is_holding_or_subsidiary_of_public_company: false, is_consolidated_financial_statements: false,
    paid_up_capital_and_reserves: "0", maximum_borrowings_during_year: "0",
    total_revenue_including_discontinued_operations: "0",
  });
  const [caroAssessment, setCaroAssessment] = useState<{ determined_applicable: boolean; applicable_clause_numbers: number[]; reasoning: string } | null>(null);
  const suggest = useMutation({ mutationFn: () => api.suggestAuditMateriality(engagement.id, { turnover, profit_before_tax: pbt, net_worth: netWorth }), onSuccess: (data) => { setSuggestion(data); setConfirmAmount(data.suggested_amount); } });
  const confirmMateriality = useMutation({ mutationFn: () => api.confirmAuditMateriality(engagement.id, { confirmed_amount: confirmAmount, override_rationale: confirmedAmountChanged ? overrideReason : undefined }), onSuccess: onChanged });
  const assess = useMutation({ mutationFn: () => api.assessCaroApplicability(engagement.id, caroInputs), onSuccess: (data) => { setCaroAssessment(data); void queryClient.invalidateQueries({ queryKey: ["audit-engagement", engagement.id] }); } });
  const confirmCaro = useMutation({ mutationFn: (applicable: boolean) => api.confirmCaroApplicability(engagement.id, applicable, "Partner reviewed the supplied company classification and financial inputs."), onSuccess: onChanged });
  const assignedPartner = user?.id === engagement.engagement_partner_id && user.role === "PARTNER";
  const canPrepare = hasRole("ASSOCIATE");
  const isPlanning = engagement.status === "PLANNING";
  const confirmedMateriality = !!engagement.materiality_confirmed_by;
  const confirmedCaro = !!engagement.caro_confirmed_by;
  const confirmedAmountChanged = !!suggestion && confirmAmount !== suggestion.suggested_amount;

  return <div className="space-y-5">
    <section className={`${panel} p-5`}>
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-base font-semibold text-slate-900">Materiality</h2><p className="mt-1 max-w-2xl text-sm text-slate-600">The configured firm convention suggests an amount. The assigned partner must confirm it before downstream use.</p></div><StateTag ok={confirmedMateriality} label={confirmedMateriality ? `Partner confirmed · ₹${Number(engagement.materiality_amount).toLocaleString("en-IN")}` : "Partner confirmation required"} /></div>
      {isPlanning && canPrepare && <form className="mt-4 grid gap-3 md:grid-cols-4" onSubmit={(e) => { e.preventDefault(); suggest.mutate(); }}>
        <MoneyField label="Turnover" value={turnover} onChange={setTurnover} /><MoneyField label="Profit before tax" value={pbt} onChange={setPbt} /><MoneyField label="Net worth" value={netWorth} onChange={setNetWorth} />
        <div className="flex items-end"><button disabled={suggest.isPending || !turnover || !pbt || !netWorth} className="w-full rounded-lg border border-teal-800 px-3 py-2 text-sm font-semibold text-teal-900 hover:bg-teal-50 disabled:opacity-50">{suggest.isPending ? "Calculating…" : "Suggest materiality"}</button></div>
      </form>}
      {suggest.isError && <ErrorText message={api.getErrorMessage(suggest.error)} />}
      {suggestion && !confirmedMateriality && <div className="mt-4 rounded-lg bg-sky-50 p-4 ring-1 ring-sky-200"><p className="text-sm font-semibold text-sky-950">Suggested: ₹{Number(suggestion.suggested_amount).toLocaleString("en-IN")}</p><p className="mt-1 text-xs text-sky-800">Basis: {suggestion.basis.replaceAll("_", " ")} at {suggestion.percentage}%.</p>
        {assignedPartner && <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); confirmMateriality.mutate(); }}><label className="text-xs font-medium text-slate-700">Partner-confirmed amount<input required type="number" min="0.01" step="0.01" value={confirmAmount} onChange={(e) => setConfirmAmount(e.target.value)} className="mt-1 block w-56 rounded border border-slate-300 px-3 py-2 text-sm" /></label>{confirmedAmountChanged && <label className="min-w-64 flex-1 text-xs font-medium text-slate-700">Override rationale<input required minLength={10} value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 text-sm" placeholder="Explain the professional basis for override" /></label>}<button disabled={confirmMateriality.isPending || (confirmedAmountChanged && overrideReason.trim().length < 10)} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white disabled:opacity-50">Confirm materiality</button></form>}
        {confirmMateriality.isError && <ErrorText message={api.getErrorMessage(confirmMateriality.error)} />}
      </div>}
    </section>

    <section className={`${panel} p-5`}>
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-base font-semibold text-slate-900">CARO applicability</h2><p className="mt-1 max-w-2xl text-sm text-slate-600">The ruleset version is selected by the backend for this financial year. A partner must verify the inputs and confirm or override the determination.</p></div><StateTag ok={confirmedCaro} label={confirmedCaro ? (engagement.caro_applicable ? "Partner confirmed applicable" : "Partner confirmed not applicable") : "Partner confirmation required"} /></div>
      {isPlanning && canPrepare && <form className="mt-4" onSubmit={(e) => { e.preventDefault(); assess.mutate(); }}>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{Object.entries({ is_banking_company: "Banking company", is_insurance_company: "Insurance company", is_section_8_company: "Section 8 company", is_one_person_company: "One Person Company", is_small_company: "Small company", is_private_company: "Private company", is_holding_or_subsidiary_of_public_company: "Holding/subsidiary of public company", is_consolidated_financial_statements: "Consolidated financial statements" }).map(([key, label]) => <label key={key} className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-xs text-slate-700"><input type="checkbox" checked={caroInputs[key as keyof typeof caroInputs] as boolean} onChange={(e) => setCaroInputs((old) => ({ ...old, [key]: e.target.checked }))} className="accent-teal-800" />{label}</label>)}</div>
        <div className="mt-3 grid gap-3 md:grid-cols-3"><MoneyField label="Paid-up capital and reserves" value={caroInputs.paid_up_capital_and_reserves} onChange={(value) => setCaroInputs((old) => ({ ...old, paid_up_capital_and_reserves: value }))} /><MoneyField label="Maximum borrowings during year" value={caroInputs.maximum_borrowings_during_year} onChange={(value) => setCaroInputs((old) => ({ ...old, maximum_borrowings_during_year: value }))} /><MoneyField label="Total revenue incl. discontinued operations" value={caroInputs.total_revenue_including_discontinued_operations} onChange={(value) => setCaroInputs((old) => ({ ...old, total_revenue_including_discontinued_operations: value }))} /></div>
        <button disabled={assess.isPending} className="mt-3 rounded-lg border border-teal-800 px-3 py-2 text-sm font-semibold text-teal-900 hover:bg-teal-50">{assess.isPending ? "Assessing…" : "Assess from versioned rules"}</button>
      </form>}
      {assess.isError && <ErrorText message={api.getErrorMessage(assess.error)} />}
      {caroAssessment && !confirmedCaro && <div className="mt-4 rounded-lg bg-sky-50 p-4 ring-1 ring-sky-200"><p className="text-sm font-semibold text-sky-950">System determination: {caroAssessment.determined_applicable ? "Applicable" : "Not applicable"}</p><p className="mt-1 text-sm text-sky-900">{caroAssessment.reasoning}</p><p className="mt-2 text-xs text-sky-800">Clauses in configured scope: {caroAssessment.applicable_clause_numbers.join(", ") || "none"}</p>{assignedPartner && <div className="mt-3 flex flex-wrap gap-2"><button onClick={() => confirmCaro.mutate(caroAssessment.determined_applicable)} disabled={confirmCaro.isPending} className="rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white">Confirm determination</button><button onClick={() => confirmCaro.mutate(!caroAssessment.determined_applicable)} disabled={confirmCaro.isPending} className="rounded-lg border border-slate-400 px-3 py-2 text-sm font-semibold text-slate-800">Override to {caroAssessment.determined_applicable ? "not applicable" : "applicable"}</button></div>}</div>}
      {(confirmCaro.isError || assess.isError) && confirmCaro.isError && <ErrorText message={api.getErrorMessage(confirmCaro.error)} />}
      {confirmedCaro && <p className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-900">Partner confirmation is recorded on this engagement.</p>}
    </section>

    {isPlanning && assignedPartner && <section className={`${panel} flex flex-wrap items-center justify-between gap-3 p-5`}><div><h2 className="text-base font-semibold text-slate-900">Begin fieldwork</h2><p className="mt-1 text-sm text-slate-600">Available after partner confirmation of both planning assessments.</p></div><button onClick={onStart} disabled={starting || !confirmedMateriality || !confirmedCaro} className="rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white hover:bg-teal-900 disabled:opacity-50">{starting ? "Starting…" : "Start fieldwork"}</button>{startError && <ErrorText message={startError} />}</section>}
  </div>;
}

function MoneyField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <label className="text-xs font-medium text-slate-600">{label}<input required type="number" min="0" step="0.01" value={value} onChange={(e) => onChange(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" /></label>;
}

function StateTag({ ok, label }: { ok: boolean; label: string }) { return <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${ok ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900"}`}>{label}</span>; }
function ErrorText({ message }: { message: string }) { return <p role="alert" className="mt-3 text-sm text-rose-700">{message}</p>; }

function RelatedPartiesTab({ clientId, rows, isLoading, error, canManage, onChanged }: {
  clientId: string; rows: RelatedParty[]; isLoading: boolean; error: string; canManage: boolean; onChanged: () => void;
}) {
  const [editing, setEditing] = useState<RelatedParty | null>(null);
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [pan, setPan] = useState("");
  const [nature, setNature] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [effectiveTo, setEffectiveTo] = useState("");
  const create = useMutation({ mutationFn: () => api.createRelatedParty(clientId, { name, aliases: aliases.split(",").map((x) => x.trim()).filter(Boolean), pan: pan || undefined, relationship_nature: nature, effective_from: effectiveFrom || undefined, effective_to: effectiveTo || undefined }), onSuccess: () => { onChanged(); reset(); } });
  const update = useMutation({ mutationFn: () => api.updateRelatedParty(clientId, editing!.id, { name, aliases: aliases.split(",").map((x) => x.trim()).filter(Boolean), pan, relationship_nature: nature, effective_from: effectiveFrom || null, effective_to: effectiveTo || null, is_active: editing!.is_active }), onSuccess: () => { onChanged(); reset(); } });
  const deactivate = useMutation({ mutationFn: (id: string) => api.deactivateRelatedParty(clientId, id), onSuccess: onChanged });
  function reset() { setEditing(null); setName(""); setAliases(""); setPan(""); setNature(""); setEffectiveFrom(""); setEffectiveTo(""); }
  function startEdit(row: RelatedParty) { setEditing(row); setName(row.name); setAliases(row.aliases.join(", ")); setPan(row.pan ?? ""); setNature(row.relationship_nature); setEffectiveFrom(row.effective_from ?? ""); setEffectiveTo(row.effective_to ?? ""); }
  const saving = create.isPending || update.isPending;

  return <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
    <section className={`${panel} overflow-hidden`}>
      <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-5 py-4"><div><h2 className="text-base font-semibold text-slate-900">Client related-party master</h2><p className="mt-1 text-sm text-slate-600">Name and alias matches create mandatory review flags regardless of amount.</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">{rows.filter((row) => row.is_active).length} active</span></div>
      {isLoading && <p className="p-5 text-sm text-slate-500">Loading related parties…</p>}
      {error && <p role="alert" className="p-5 text-sm text-rose-700">{error}</p>}
      {!isLoading && rows.length === 0 && <p className="p-5 text-sm text-slate-600">No related-party entries yet.</p>}
      <div className="divide-y divide-slate-100">{rows.map((row) => <div key={row.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <div className="min-w-0"><div className="flex items-center gap-2"><h3 className="truncate text-sm font-semibold text-slate-900">{row.name}</h3><span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${row.is_active ? "bg-teal-50 text-teal-800" : "bg-slate-100 text-slate-500"}`}>{row.is_active ? "Active" : "Inactive"}</span></div><p className="mt-1 text-xs text-slate-600">{row.relationship_nature}</p><p className="mt-1 text-[11px] text-slate-500">{row.aliases.length ? `Aliases: ${row.aliases.join(", ")}` : "No aliases"}{row.pan ? ` · PAN ${row.pan}` : ""}</p></div>
        {canManage && <div className="flex gap-2"><button onClick={() => startEdit(row)} className="rounded-md border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">Edit</button>{row.is_active ? <button onClick={() => deactivate.mutate(row.id)} disabled={deactivate.isPending} className="rounded-md border border-rose-200 px-2.5 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50">Deactivate</button> : <button onClick={() => void api.updateRelatedParty(clientId, row.id, { is_active: true }).then(onChanged)} className="rounded-md border border-teal-200 px-2.5 py-1.5 text-xs font-medium text-teal-800 hover:bg-teal-50">Reactivate</button>}</div>}
      </div>)}</div>
      {deactivate.isError && <p role="alert" className="border-t border-rose-100 px-5 py-3 text-sm text-rose-700">{api.getErrorMessage(deactivate.error)}</p>}
    </section>
    {canManage && <aside className={`${panel} h-fit p-5`}><h2 className="text-base font-semibold text-slate-900">{editing ? "Edit related party" : "Add related party"}</h2><p className="mt-1 text-xs leading-5 text-slate-600">Maintain this master from verified client information. Matches are review prompts, not classification conclusions.</p>
      <form className="mt-4 space-y-3" onSubmit={(e) => { e.preventDefault(); if (editing) update.mutate(); else create.mutate(); }}>
        <TextField label="Name" required value={name} onChange={setName} placeholder="Legal or commonly used name" />
        <TextField label="Aliases" value={aliases} onChange={setAliases} placeholder="Comma-separated names" />
        <TextField label="PAN (optional)" value={pan} onChange={setPan} maxLength={10} placeholder="ABCDE1234F" />
        <label className="block text-xs font-medium text-slate-600">Nature of relationship<textarea required minLength={1} rows={3} value={nature} onChange={(e) => setNature(e.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="Describe the known relationship" /></label>
        <div className="grid grid-cols-2 gap-2"><TextField label="Effective from" type="date" value={effectiveFrom} onChange={setEffectiveFrom} /><TextField label="Effective to" type="date" value={effectiveTo} onChange={setEffectiveTo} /></div>
        {(create.isError || update.isError) && <ErrorText message={api.getErrorMessage(create.error ?? update.error)} />}
        <div className="flex gap-2"><button disabled={saving} className="flex-1 rounded-lg bg-teal-800 px-3 py-2 text-sm font-semibold text-white hover:bg-teal-900 disabled:opacity-50">{saving ? "Saving…" : editing ? "Save changes" : "Add to master"}</button>{editing && <button type="button" onClick={reset} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700">Cancel</button>}</div>
      </form>
    </aside>}
  </div>;
}

function TextField({ label, value, onChange, required = false, type = "text", placeholder, maxLength }: { label: string; value: string; onChange: (value: string) => void; required?: boolean; type?: string; placeholder?: string; maxLength?: number }) {
  return <label className="block text-xs font-medium text-slate-600">{label}<input type={type} required={required} maxLength={maxLength} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900" /></label>;
}

function CaroTab({ engagement, responses, loading, error, canAssociate, canManage, canPartner, currentUserId, onGenerate, generating, generateError, onChanged }: {
  engagement: AuditEngagement; responses: CAROClauseResponse[]; loading: boolean; error: string;
  canAssociate: boolean; canManage: boolean; canPartner: boolean; currentUserId: string;
  onGenerate: () => void; generating: boolean; generateError: string; onChanged: () => void;
}) {
  const partnerAssigned = canPartner && currentUserId === engagement.engagement_partner_id;
  const generationAllowed = ["FIELDWORK", "COMPLETION"].includes(engagement.status) && engagement.caro_applicable === true && !!engagement.caro_confirmed_by;
  return <div className="space-y-4">
    <section className={`${panel} flex flex-wrap items-center justify-between gap-4 p-5`}>
      <div className="max-w-3xl"><h2 className="text-base font-semibold text-slate-900">CARO clause responses</h2><p className="mt-1 text-sm leading-6 text-slate-600">System drafts are shown in amber and remain separate from CA-authored language. A manager reviews associate work; only the assigned engagement partner can finalize after manager review.</p></div>
      {canAssociate && <button onClick={onGenerate} disabled={!generationAllowed || generating} className="rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white hover:bg-teal-900 disabled:cursor-not-allowed disabled:opacity-50">{generating ? "Building drafts…" : responses.length ? "Refresh system drafts" : "Generate clause drafts"}</button>}
      {!generationAllowed && <p className="w-full rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">Draft generation unlocks after CARO applicability is partner-confirmed as applicable and the engagement reaches Fieldwork.</p>}
      {generateError && <ErrorText message={generateError} />}
    </section>
    {loading && <p className="text-sm text-slate-500">Loading CARO responses…</p>}
    {error && <ErrorText message={error} />}
    {!loading && !error && responses.length === 0 && <div className={`${panel} p-8 text-center`}><p className="text-sm font-medium text-slate-800">No clause drafts yet.</p><p className="mt-1 text-xs text-slate-500">Complete planning and begin fieldwork before generating the applicable clause set.</p></div>}
    {responses.map((response) => <CaroResponseCard key={response.id} engagementId={engagement.id} response={response} canAssociate={canAssociate} canManage={canManage} isAssignedPartner={partnerAssigned} currentUserId={currentUserId} onChanged={onChanged} />)}
  </div>;
}

function CaroResponseCard({ engagementId, response, canAssociate, canManage, isAssignedPartner, currentUserId, onChanged }: {
  engagementId: string; response: CAROClauseResponse; canAssociate: boolean; canManage: boolean;
  isAssignedPartner: boolean; currentUserId: string; onChanged: () => void;
}) {
  const [narrative, setNarrative] = useState(response.final_narrative ?? response.draft_narrative);
  const [reviewNotes, setReviewNotes] = useState("");
  const canEdit = canAssociate && currentUserId === response.prepared_by && response.status === "DRAFT";
  const canReview = canManage && currentUserId !== response.prepared_by && response.status === "DRAFT";
  const canFinalize = isAssignedPartner && response.status === "MANAGER_REVIEWED" && currentUserId !== response.prepared_by && currentUserId !== response.reviewed_by;
  const edit = useMutation({ mutationFn: () => api.editCaroResponse(engagementId, response.id, narrative), onSuccess: onChanged });
  const review = useMutation({ mutationFn: (decision: "approve" | "rework") => api.reviewCaroResponse(engagementId, response.id, decision, reviewNotes), onSuccess: onChanged });
  const finalize = useMutation({ mutationFn: () => api.finalizeCaroResponse(engagementId, response.id, narrative), onSuccess: onChanged });
  const statusLabel: Record<string, string> = { DRAFT: "Associate draft", MANAGER_REVIEWED: "Awaiting partner", FINALIZED: "Partner finalized", SUPERSEDED: "Superseded" };
  const statusStyle = response.status === "FINALIZED" ? "bg-emerald-100 text-emerald-900" : response.status === "MANAGER_REVIEWED" ? "bg-violet-100 text-violet-900" : "bg-amber-100 text-amber-900";
  const snapshot = response.source_snapshot;

  return <article className={`${panel} overflow-hidden`}>
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4"><div><p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Clause {response.clause_number} · version {response.version}</p><h3 className="mt-1 text-base font-semibold text-slate-900">CARO clause response</h3></div><span className={`rounded-full px-3 py-1.5 text-xs font-semibold ${statusStyle}`}>{statusLabel[response.status] ?? response.status}</span></header>
    <div className="space-y-4 p-5">
      <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 shadow-[inset_3px_0_0_0_#d97706]"><div className="flex items-center gap-2"><span className="rounded bg-amber-200 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-950">System draft</span><span className="text-[11px] text-amber-900">Not a conclusion · verify source evidence</span></div><p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-amber-950">{response.draft_narrative}</p></div>
      {response.status === "FINALIZED" ? <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4"><div className="flex items-center gap-2"><span className="rounded bg-emerald-200 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-emerald-950">Partner finalized</span><span className="text-[11px] text-emerald-900">CA-authored reporting language</span></div><p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-emerald-950">{response.final_narrative}</p></div> : <div><label className="block text-xs font-semibold text-slate-700">CA-authored / edited narrative<textarea rows={5} value={narrative} readOnly={!canEdit && !canFinalize} onChange={(e) => setNarrative(e.target.value)} className={`mt-1 w-full rounded-lg border px-3 py-2 text-sm leading-6 ${canEdit || canFinalize ? "border-slate-300 bg-white text-slate-900" : "border-slate-200 bg-slate-50 text-slate-600"}`} /></label><p className="mt-1 text-[11px] text-slate-500">This is separate from the system draft. It becomes finalized only after the required human approvals.</p></div>}
      {response.review_notes && <p className="rounded-lg bg-violet-50 px-3 py-2 text-xs text-violet-900"><strong>Manager note:</strong> {response.review_notes}</p>}
      <details className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2"><summary className="cursor-pointer text-xs font-semibold text-slate-700">Recorded evidence snapshot</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[10px] leading-4 text-slate-600">{JSON.stringify(snapshot, null, 2)}</pre></details>
      <div className="flex flex-wrap items-end gap-2">
        {canEdit && <button onClick={() => edit.mutate()} disabled={edit.isPending || !narrative.trim()} className="rounded-lg border border-teal-800 px-3 py-2 text-xs font-semibold text-teal-900 hover:bg-teal-50 disabled:opacity-50">{edit.isPending ? "Saving…" : "Save associate edits"}</button>}
        {canReview && <><label className="min-w-56 flex-1 text-[11px] font-medium text-slate-600">Review note (optional)<input value={reviewNotes} onChange={(e) => setReviewNotes(e.target.value)} className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2 text-xs" placeholder="Comments for preparer / audit trail" /></label><button onClick={() => review.mutate("rework")} disabled={review.isPending} className="rounded-lg border border-amber-300 px-3 py-2 text-xs font-semibold text-amber-900 hover:bg-amber-50">Return for rework</button><button onClick={() => review.mutate("approve")} disabled={review.isPending || !response.final_narrative} className="rounded-lg bg-violet-800 px-3 py-2 text-xs font-semibold text-white hover:bg-violet-900 disabled:opacity-50">{review.isPending ? "Reviewing…" : "Manager approve"}</button></>}
        {canFinalize && <button onClick={() => finalize.mutate()} disabled={finalize.isPending || !narrative.trim()} className="rounded-lg bg-emerald-800 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-900 disabled:opacity-50">{finalize.isPending ? "Finalizing…" : "Partner finalize"}</button>}
        {response.status === "MANAGER_REVIEWED" && !isAssignedPartner && <span className="text-xs text-slate-500">Only the assigned engagement partner can finalize.</span>}
        {(edit.isError || review.isError || finalize.isError) && <ErrorText message={api.getErrorMessage(edit.error ?? review.error ?? finalize.error)} />}
      </div>
    </div>
  </article>;
}

function EvidenceTab({ engagement, canUpload }: { engagement: AuditEngagement; canUpload: boolean }) {
  const { user, hasRole } = useAuth();
  const queryClient = useQueryClient();
  const [currentFile, setCurrentFile] = useState<File | null>(null);
  const [priorFile, setPriorFile] = useState<File | null>(null);
  const fyEndYear = Number(engagement.financial_year.slice(0, 4)) + 1;
  const [asOfDate, setAsOfDate] = useState(`${fyEndYear}-03-31`);
  const [ledgerFile, setLedgerFile] = useState<File | null>(null);
  const [taxType, setTaxType] = useState("GST");
  const [period, setPeriod] = useState(`${engagement.financial_year.slice(0, 4)}-04`);
  const [filingStatus, setFilingStatus] = useState("FILED");
  const [filingSource, setFilingSource] = useState("CLIENT_RECORD");
  const [dueDate, setDueDate] = useState("");
  const [sourceReference, setSourceReference] = useState("");
  const [filingNotes, setFilingNotes] = useState("");
  const invalidateEvidence = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["tb-uploads", engagement.id] }),
      queryClient.invalidateQueries({ queryKey: ["ledger-ageing", engagement.id] }),
      queryClient.invalidateQueries({ queryKey: ["risk-flags", engagement.id] }),
      queryClient.invalidateQueries({ queryKey: ["statutory-dues", engagement.id] }),
    ]);
  };
  const uploads = useQuery({ queryKey: ["tb-uploads", engagement.id], queryFn: () => api.listTrialBalanceUploads(engagement.id) });
  const schedules = useQuery({ queryKey: ["ledger-ageing", engagement.id], queryFn: () => api.listAgeingSchedules(engagement.id) });
  const flags = useQuery({ queryKey: ["risk-flags", engagement.id], queryFn: () => api.listAuditRiskFlags(engagement.id) });
  const dues = useQuery({ queryKey: ["statutory-dues", engagement.id], queryFn: () => api.getStatutoryDues(engagement.id) });
  const uploadTb = useMutation({ mutationFn: () => api.uploadComparativeTrialBalance(engagement.id, currentFile!, priorFile!), onSuccess: invalidateEvidence });
  const uploadLedger = useMutation({ mutationFn: () => api.uploadAuditLedger(engagement.id, asOfDate, ledgerFile!), onSuccess: invalidateEvidence });
  const recordFiling = useMutation({ mutationFn: () => api.recordStatutoryFiling(engagement.id, { tax_type: taxType, period, filing_status: filingStatus, filing_source: filingSource, statutory_due_date: filingStatus === "PENDING" || filingStatus === "ARREARS" ? dueDate : undefined, filed_at: filingStatus === "FILED" ? new Date().toISOString().slice(0, 10) : undefined, source_reference: sourceReference || undefined, notes: filingNotes || undefined }), onSuccess: invalidateEvidence });
  const reviewFiling = useMutation({ mutationFn: (recordId: string) => api.reviewStatutoryFiling(engagement.id, recordId), onSuccess: invalidateEvidence });
  const statusCanSave = filingStatus === "FILED" ? !!sourceReference : !!dueDate && (!!sourceReference || !!filingNotes);

  return <div className="space-y-5">
    <section className={`${panel} p-5`}><div className="flex flex-wrap justify-between gap-3"><div><h2 className="text-base font-semibold text-slate-900">Trial balances</h2><p className="mt-1 text-sm text-slate-600">Upload current and prior-year files together. Validation errors are returned before the upload is stored.</p></div><span className="text-xs text-slate-500">{uploads.data?.length ?? 0} versions</span></div>
      {canUpload && ["PLANNING", "FIELDWORK", "COMPLETION"].includes(engagement.status) && <form className="mt-4 grid gap-3 md:grid-cols-[1fr_1fr_auto]" onSubmit={(e) => { e.preventDefault(); uploadTb.mutate(); }}><FileField label="Current year trial balance" onChange={setCurrentFile} /><FileField label="Prior year trial balance" onChange={setPriorFile} /><div className="flex items-end"><button disabled={uploadTb.isPending || !currentFile || !priorFile} className="rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{uploadTb.isPending ? "Validating…" : "Upload & compare"}</button></div></form>}
      {uploadTb.isSuccess && (uploadTb.data.accepted === true ? <SuccessText text="Trial balances processed. Review the generated flags below." /> : <ErrorText message={uploadErrors(uploadTb.data) || "The trial-balance files were not accepted."} />)}{uploadTb.isError && <ErrorText message={api.getErrorMessage(uploadTb.error)} />}
      {uploads.isLoading ? <p className="mt-4 text-xs text-slate-500">Loading uploaded files…</p> : <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[540px] text-left text-xs"><thead><tr className="border-b border-slate-200 text-[10px] uppercase tracking-wide text-slate-500"><th className="py-2">Period</th><th>Version</th><th>Rows</th><th>File</th><th>Current</th></tr></thead><tbody>{(uploads.data ?? []).map((row) => <tr key={row.id} className="border-b border-slate-100"><td className="py-2">{row.period_type}</td><td>{row.version}</td><td>{row.row_count}</td><td>{row.original_filename}</td><td>{row.is_current ? "Yes" : "No"}</td></tr>)}</tbody></table></div>}
    </section>

    <section className={`${panel} p-5`}><h2 className="text-base font-semibold text-slate-900">Ledger and ageing schedules</h2><p className="mt-1 text-sm text-slate-600">Invoice-level ledger uploads generate versioned debtor and creditor ageing schedules.</p>
      {canUpload && ["PLANNING", "FIELDWORK", "COMPLETION"].includes(engagement.status) && <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); uploadLedger.mutate(); }}><FileField label="Ledger CSV / XLSX" onChange={setLedgerFile} /><label className="text-xs font-medium text-slate-600">Ageing as of<input required type="date" value={asOfDate} onChange={(e) => setAsOfDate(e.target.value)} className="mt-1 block rounded-lg border border-slate-300 px-3 py-2 text-sm" /></label><button disabled={uploadLedger.isPending || !ledgerFile} className="rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{uploadLedger.isPending ? "Uploading…" : "Upload ledger"}</button></form>}
      {uploadLedger.isSuccess && (uploadLedger.data.accepted === true ? <SuccessText text="Ledger accepted; ageing schedules are available below." /> : <ErrorText message={uploadErrors(uploadLedger.data) || "The ledger file was not accepted."} />)}{uploadLedger.isError && <ErrorText message={api.getErrorMessage(uploadLedger.error)} />}
      {!!schedules.data?.length && <div className="mt-4 grid gap-3 md:grid-cols-2">{schedules.data.slice(0, 4).map((schedule) => <div key={schedule.id} className="rounded-lg border border-slate-200 p-3"><div className="flex justify-between"><strong className="text-sm text-slate-900">{schedule.schedule_type}</strong><span className="text-[11px] text-slate-500">As of {schedule.as_of_date} · v{schedule.source_version}</span></div><dl className="mt-2 grid grid-cols-2 gap-1 text-xs">{Object.entries(schedule.data.bucket_totals ?? {}).map(([bucket, amount]) => <div key={bucket} className="flex justify-between gap-2 rounded bg-slate-50 px-2 py-1"><dt className="text-slate-600">{bucket} days</dt><dd className="font-medium text-slate-900">₹{Number(amount).toLocaleString("en-IN")}</dd></div>)}</dl></div>)}</div>}
      {schedules.isError && <ErrorText message={api.getErrorMessage(schedules.error)} />}
    </section>

    <section className={`${panel} p-5`}><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-base font-semibold text-slate-900">Statutory dues status</h2><p className="mt-1 text-sm text-slate-600">Filing status is source-reported until manager review. GSTR-2B appears only as invoice-reconciliation context, not proof of tax filing or payment.</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">{dues.data?.length ?? 0} period rows</span></div>
      {canUpload && <form className="mt-4 grid gap-2 rounded-lg bg-slate-50 p-3 sm:grid-cols-3 lg:grid-cols-6" onSubmit={(e) => { e.preventDefault(); recordFiling.mutate(); }}>
        <label className="text-[11px] font-medium text-slate-600">Tax type<select value={taxType} onChange={(e) => setTaxType(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs">{["GST", "TDS", "PF", "ESI"].map((x) => <option key={x}>{x}</option>)}</select></label>
        <label className="text-[11px] font-medium text-slate-600">Period<input required pattern="20\d{2}-(0[1-9]|1[0-2])" value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="2026-04" className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs" /></label>
        <label className="text-[11px] font-medium text-slate-600">Reported status<select value={filingStatus} onChange={(e) => setFilingStatus(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs">{["FILED", "PENDING", "ARREARS", "NOT_APPLICABLE"].map((x) => <option key={x}>{x}</option>)}</select></label>
        <label className="text-[11px] font-medium text-slate-600">Source<select value={filingSource} onChange={(e) => setFilingSource(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs">{[["CLIENT_RECORD", "Client records"], ["PORTAL_RECORD", "Portal record"], ["MANAGEMENT_REPRESENTATION", "Management representation"], ["OTHER", "Other"]].map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></label>
        {(filingStatus === "PENDING" || filingStatus === "ARREARS") && <label className="text-[11px] font-medium text-slate-600">Due date<input required type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs" /></label>}
        <label className="text-[11px] font-medium text-slate-600 sm:col-span-2">Source reference<input value={sourceReference} onChange={(e) => setSourceReference(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs" placeholder="Challan, portal period, document reference" /></label>
        <label className="text-[11px] font-medium text-slate-600 sm:col-span-2">Notes<input value={filingNotes} onChange={(e) => setFilingNotes(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2 text-xs" placeholder="Supporting context" /></label>
        <div className="flex items-end"><button disabled={recordFiling.isPending || !statusCanSave} className="w-full rounded bg-teal-800 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Record status</button></div>
        {recordFiling.isError && <ErrorText message={api.getErrorMessage(recordFiling.error)} />}{recordFiling.isSuccess && <SuccessText text="Filing status recorded as source-reported." />}
      </form>}
      {dues.isLoading ? <p className="mt-4 text-xs text-slate-500">Loading statutory periods…</p> : <div className="mt-4 max-h-80 overflow-auto rounded-lg border border-slate-200"><table className="w-full min-w-[670px] text-left text-xs"><thead className="sticky top-0 bg-slate-50"><tr className="text-[10px] uppercase tracking-wide text-slate-500"><th className="px-3 py-2">Period</th><th>Tax</th><th>Reported filing status</th><th>Review state</th><th>GSTR-2B context</th></tr></thead><tbody>{(dues.data ?? []).map((row) => <tr key={`${row.period}-${row.tax_type}`} className="border-t border-slate-100"><td className="px-3 py-2">{row.period}</td><td>{row.tax_type}</td><td>{row.filing_status ?? "Not recorded"}</td><td>{row.verification_status}{row.current_record && !row.current_record.reviewed_by && hasRole("MANAGER") && user?.id !== row.current_record.recorded_by && <button onClick={() => reviewFiling.mutate(row.current_record!.id)} disabled={reviewFiling.isPending} className="ml-2 rounded border border-violet-200 px-2 py-1 text-[10px] font-semibold text-violet-900">Manager review</button>}</td><td>{row.tax_type === "GST" ? row.gstr2b_context.map((x) => `${x.gstin}: ${x.reconciliation_available ? "reconciled data available" : x.gstr2b_available ? "2B available" : "no 2B data"}`).join(" · ") || "No GSTIN context" : "—"}</td></tr>)}</tbody></table></div>}
      {dues.isError && <ErrorText message={api.getErrorMessage(dues.error)} />}
      {reviewFiling.isError && <ErrorText message={api.getErrorMessage(reviewFiling.error)} />}
    </section>

    <section className={`${panel} p-5`}><div className="flex items-center justify-between gap-3"><div><h2 className="text-base font-semibold text-slate-900">System risk flags</h2><p className="mt-1 text-sm text-slate-600">Every item remains a review prompt until a human records a decision.</p></div><span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-900">{flags.data?.length ?? 0} flags</span></div>
      {flags.isError && <ErrorText message={api.getErrorMessage(flags.error)} />}
      {flags.data?.length === 0 && <p className="mt-4 text-sm text-slate-500">No system flags currently recorded.</p>}
      <div className="mt-3 space-y-2">{(flags.data ?? []).map((flag) => <div key={flag.id} className="rounded-lg border border-amber-200 bg-amber-50/70 p-3"><div className="flex flex-wrap items-center gap-2"><span className="rounded bg-amber-200 px-2 py-0.5 text-[10px] font-bold uppercase text-amber-950">{flag.flag_type}</span><span className="text-[10px] font-semibold text-amber-900">{flag.status} · {flag.severity}</span><time className="ml-auto text-[10px] text-slate-500">{new Date(flag.created_at).toLocaleString()}</time></div><p className="mt-2 text-xs leading-5 text-slate-800">{flag.system_note}</p></div>)}</div>
    </section>
  </div>;
}

function FileField({ label, onChange }: { label: string; onChange: (file: File | null) => void }) {
  return <label className="block text-xs font-medium text-slate-600">{label}<input type="file" accept=".csv,.xlsx" onChange={(e) => onChange(e.target.files?.[0] ?? null)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs file:mr-3 file:rounded file:border-0 file:bg-slate-100 file:px-2 file:py-1" /></label>;
}

function SuccessText({ text }: { text: string }) { return <p role="status" className="mt-3 text-xs font-medium text-emerald-800">{text}</p>; }

function uploadErrors(payload: Record<string, unknown>): string {
  if (!Array.isArray(payload.errors)) return "";
  return payload.errors.map((issue) => {
    if (!issue || typeof issue !== "object") return String(issue);
    const entry = issue as Record<string, unknown>;
    const where = [entry.field, entry.row ? `row ${entry.row}` : ""].filter(Boolean).join(", ");
    return `${where ? `${where}: ` : ""}${String(entry.message ?? "Invalid row")}`;
  }).join(" · ");
}

function BankTab({ engagement }: { engagement: AuditEngagement }) {
  const { user, hasRole } = useAuth(); const qc = useQueryClient(); const clientId = engagement.client_id;
  const fyEnd = `${Number(engagement.financial_year.slice(0, 4)) + 1}-03-31`;
  const [asOf, setAsOf] = useState(fyEnd); const [bank, setBank] = useState(""); const [branch, setBranch] = useState("");
  const [accountName, setAccountName] = useState(""); const [number, setNumber] = useState(""); const [ifsc, setIfsc] = useState("");
  const [receipt, setReceipt] = useState<{ id: string; books: string; confirmed: string } | null>(null);
  const [reconcile, setReconcile] = useState(""); const [reconcileNotes, setReconcileNotes] = useState("");
  const accounts = useQuery({ queryKey: ["bank-accounts", clientId], queryFn: () => api.listClientBankAccounts(clientId) });
  const confirmations = useQuery({ queryKey: ["bank-confirmations", engagement.id], queryFn: () => api.listBankConfirmations(engagement.id) });
  const refresh = async () => { await Promise.all([qc.invalidateQueries({ queryKey: ["bank-accounts", clientId] }), qc.invalidateQueries({ queryKey: ["bank-confirmations", engagement.id] }), qc.invalidateQueries({ queryKey: ["risk-flags", engagement.id] })]); };
  const add = useMutation({ mutationFn: () => api.createClientBankAccount(clientId, { bank_name: bank, branch_name: branch || undefined, account_name: accountName || undefined, account_number: number, ifsc_code: ifsc || undefined }), onSuccess: () => { setBank(""); setBranch(""); setAccountName(""); setNumber(""); setIfsc(""); void refresh(); } });
  const generate = useMutation({ mutationFn: () => api.generateBankConfirmations(engagement.id, asOf), onSuccess: refresh });
  const act = useMutation({ mutationFn: ({ id, step, payload }: { id: string; step: string; payload?: Record<string, string> }) => api.updateBankConfirmation(engagement.id, id, step, payload), onSuccess: () => { setReceipt(null); setReconcile(""); void refresh(); } });
  const canPrepare = ["PLANNING", "FIELDWORK", "COMPLETION"].includes(engagement.status);
  return <div className="space-y-5">
    <section className={`${panel} p-5`}><h2 className="text-base font-semibold text-slate-900">Client bank accounts</h2><p className="mt-1 text-sm text-slate-600">Numbers are encrypted in storage and shown here only as masked values.</p>
      {hasRole("MANAGER") && <form className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-6" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}><TextField label="Bank" required value={bank} onChange={setBank}/><TextField label="Branch" value={branch} onChange={setBranch}/><TextField label="Account name" value={accountName} onChange={setAccountName}/><TextField label="Account number" required maxLength={34} value={number} onChange={setNumber}/><TextField label="IFSC" maxLength={11} value={ifsc} onChange={setIfsc}/><div className="flex items-end"><button disabled={add.isPending} className="w-full rounded-lg bg-teal-800 px-3 py-2 text-xs font-semibold text-white">Add account</button></div>{add.isError && <ErrorText message={api.getErrorMessage(add.error)}/>}</form>}
      <div className="mt-4 flex flex-wrap items-end gap-3"><label className="text-xs text-slate-600">As of date<input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} className="mt-1 block rounded border border-slate-300 px-2 py-2"/></label>{hasRole("ASSOCIATE") && <button disabled={!canPrepare || !accounts.data?.length || generate.isPending} onClick={() => generate.mutate()} className="rounded-lg border border-teal-800 px-3 py-2 text-sm font-semibold text-teal-900 disabled:opacity-50">Draft confirmation letters</button>}<span className="text-xs text-slate-500">Letters are not sent by the platform.</span></div>
      {generate.isError && <ErrorText message={api.getErrorMessage(generate.error)}/>}<div className="mt-3 flex flex-wrap gap-2">{(accounts.data ?? []).map((a) => <span key={a.id} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs"><b>{a.bank_name}</b> · {a.masked_account_number}</span>)}</div>
    </section>
    <section className={`${panel} p-5`}><h2 className="text-base font-semibold text-slate-900">Confirmation status</h2><p className="mt-1 text-sm text-slate-600">Draft → manager approval → manually sent → received → manager reconciliation.</p>
      {confirmations.isError && <ErrorText message={api.getErrorMessage(confirmations.error)}/>}<div className="mt-4 space-y-3">{(confirmations.data ?? []).map((row) => <article key={row.id} className="rounded-xl border border-slate-200 p-4"><div className="flex justify-between"><div><b className="text-sm">Account ending {row.account_number_last4}</b><p className="mt-1 text-xs text-slate-500">{row.as_of_date} · version {row.version}</p></div><span className="h-fit rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold">{row.letter_status}</span></div><details className="mt-3 rounded bg-slate-50 p-3"><summary className="cursor-pointer text-xs font-semibold">Review draft letter</summary><pre className="mt-2 whitespace-pre-wrap text-xs leading-5">{row.draft_content}</pre></details>
        {row.discrepancy_amount != null && <p className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-900">Difference from books: ₹{Number(row.discrepancy_amount).toLocaleString("en-IN")}{row.discrepancy_notes ? ` · ${row.discrepancy_notes}` : ""}</p>}
        <div className="mt-3 flex flex-wrap gap-2">{row.letter_status === "DRAFTED" && hasRole("MANAGER") && !row.approved_by && user?.id !== row.drafted_by && <button onClick={() => act.mutate({ id: row.id, step: "approve" })} className="rounded bg-teal-800 px-3 py-2 text-xs font-semibold text-white">Manager approve</button>}{row.letter_status === "DRAFTED" && hasRole("MANAGER") && !!row.approved_by && <button onClick={() => act.mutate({ id: row.id, step: "send" })} className="rounded border border-teal-800 px-3 py-2 text-xs font-semibold text-teal-900">Mark manually sent</button>}{row.letter_status === "SENT" && hasRole("ASSOCIATE") && <button onClick={() => act.mutate({ id: row.id, step: "pending" })} className="rounded border border-slate-300 px-3 py-2 text-xs">Mark pending</button>}{["SENT", "PENDING"].includes(row.letter_status) && hasRole("ASSOCIATE") && <button onClick={() => setReceipt({ id: row.id, books: row.books_balance ?? "0", confirmed: row.confirmed_balance ?? "0" })} className="rounded border border-teal-800 px-3 py-2 text-xs font-semibold text-teal-900">Record receipt</button>}{row.letter_status === "RECEIVED" && hasRole("MANAGER") && user?.id !== row.drafted_by && <button onClick={() => setReconcile(row.id)} className="rounded bg-teal-800 px-3 py-2 text-xs font-semibold text-white">Manager reconcile</button>}</div>
        {receipt?.id === row.id && <form className="mt-3 grid gap-2 rounded-lg bg-sky-50 p-3 sm:grid-cols-3" onSubmit={(e) => { e.preventDefault(); act.mutate({ id: row.id, step: "receive", payload: { books_balance: receipt.books, confirmed_balance: receipt.confirmed } }); }}><label className="text-xs">Books balance<input type="number" step="0.01" required value={receipt.books} onChange={(e) => setReceipt({ ...receipt, books: e.target.value })} className="mt-1 w-full rounded border p-2"/></label><label className="text-xs">Bank balance<input type="number" step="0.01" required value={receipt.confirmed} onChange={(e) => setReceipt({ ...receipt, confirmed: e.target.value })} className="mt-1 w-full rounded border p-2"/></label><div className="flex items-end"><button className="rounded bg-teal-800 px-3 py-2 text-xs font-semibold text-white">Save receipt</button></div></form>}
        {reconcile === row.id && <div className="mt-3 flex gap-2"><input value={reconcileNotes} onChange={(e) => setReconcileNotes(e.target.value)} className="min-w-0 flex-1 rounded border px-3 py-2 text-xs" placeholder="Document treatment of any difference"/><button onClick={() => act.mutate({ id: row.id, step: "reconcile", payload: reconcileNotes ? { notes: reconcileNotes } : undefined })} className="rounded bg-teal-800 px-3 py-2 text-xs font-semibold text-white">Reconcile</button></div>}
      </article>)}</div>{act.isError && <ErrorText message={api.getErrorMessage(act.error)}/>}</section>
  </div>;
}

const RATIO_FIELDS: Array<[string, string]> = [["current_assets_account_codes", "Current assets"], ["inventory_account_codes", "Inventory"], ["current_liabilities_account_codes", "Current liabilities"], ["borrowings_account_codes", "Borrowings"], ["equity_account_codes", "Equity"], ["revenue_account_codes", "Revenue"], ["cost_of_goods_sold_account_codes", "Cost of goods sold"], ["credit_sales_account_codes", "Credit sales"], ["trade_receivables_account_codes", "Trade receivables"], ["net_profit_account_codes", "Net profit"]];

function RatiosTab({ engagement }: { engagement: AuditEngagement }) {
  const { user, hasRole } = useAuth(); const qc = useQueryClient();
  const uploads = useQuery({ queryKey: ["tb-uploads", engagement.id], queryFn: () => api.listTrialBalanceUploads(engagement.id) });
  const closing = uploads.data?.find((u) => u.period_type === "CLOSING" && u.is_current);
  const lines = useQuery({ queryKey: ["tb-lines", closing?.id], queryFn: () => api.listTrialBalanceLines(closing!.id), enabled: !!closing });
  const analyses = useQuery({ queryKey: ["ratio-analyses", engagement.id], queryFn: () => api.listAuditRatioAnalyses(engagement.id) });
  const [codes, setCodes] = useState<Record<string, string>>(() => Object.fromEntries(RATIO_FIELDS.map(([key]) => [key, ""])));
  const [priorInventory, setPriorInventory] = useState(""); const [priorReceivables, setPriorReceivables] = useState(""); const [source, setSource] = useState("");
  const refresh = async () => { await Promise.all([qc.invalidateQueries({ queryKey: ["ratio-analyses", engagement.id] }), qc.invalidateQueries({ queryKey: ["risk-flags", engagement.id] })]); };
  const generate = useMutation({ mutationFn: () => {
    const payload: Record<string, string | string[]> = Object.fromEntries(RATIO_FIELDS.map(([key]) => [key, codes[key].split(",").map((v) => v.trim()).filter(Boolean)]));
    if (priorInventory) payload.prior_opening_inventory = priorInventory; if (priorReceivables) payload.prior_opening_receivables = priorReceivables;
    if (priorInventory || priorReceivables) payload.prior_opening_source_reference = source;
    return api.generateAuditRatioAnalysis(engagement.id, payload);
  }, onSuccess: refresh });
  const act = useMutation({ mutationFn: ({ id, step }: { id: string; step: "review" | "approve" }) => api.updateAuditRatioAnalysis(engagement.id, id, step), onSuccess: refresh });
  const canReview = hasRole("MANAGER") && user?.id !== engagement.engagement_partner_id;
  const assignedPartner = user?.role === "PARTNER" && user.id === engagement.engagement_partner_id;
  return <div className="space-y-5"><section className={`${panel} p-5`}><h2 className="text-base font-semibold text-slate-900">Mapped ratio analysis</h2><p className="mt-1 text-sm text-slate-600">Map account codes explicitly. No classification is inferred from account names and no ratio is interpreted as normal or abnormal.</p>
    {!closing && <p className="mt-3 rounded bg-amber-50 p-3 text-xs text-amber-900">Upload a comparative trial balance first.</p>}
    {closing && <form className="mt-4" onSubmit={(e) => { e.preventDefault(); generate.mutate(); }}><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{RATIO_FIELDS.map(([key, label]) => <TextField key={key} label={`${label} account code(s)`} required value={codes[key]} onChange={(v) => setCodes((old) => ({ ...old, [key]: v }))} placeholder="Account codes, comma-separated" />)}</div>
      <details className="mt-3 rounded bg-slate-50 p-3"><summary className="cursor-pointer text-xs font-semibold">Current trial balance account codes</summary><div className="mt-2 max-h-40 overflow-auto">{(lines.data ?? []).map((line) => <p key={line.id} className="font-mono text-[10px]">{line.account_code} · {line.account_name} · {line.account_group ?? "Unclassified"}</p>)}</div></details>
      <div className="mt-3 grid gap-3 sm:grid-cols-3"><MoneyField label="Prior opening inventory (optional)" value={priorInventory} onChange={setPriorInventory}/><MoneyField label="Prior opening receivables (optional)" value={priorReceivables} onChange={setPriorReceivables}/><TextField label="Prior opening source reference" value={source} onChange={setSource}/></div>
      <button disabled={generate.isPending || !lines.data?.length || (Boolean(priorInventory || priorReceivables) && !source)} className="mt-4 rounded-lg bg-teal-800 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Calculate ratios</button>{generate.isError && <ErrorText message={api.getErrorMessage(generate.error)}/>}</form>}
  </section>
  {analyses.isError && <ErrorText message={api.getErrorMessage(analyses.error)}/ >}{(analyses.data ?? []).map((analysis) => <RatioCard key={analysis.id} analysis={analysis} canReview={canReview} assignedPartner={assignedPartner} userId={user?.id ?? ""} busy={act.isPending} onReview={() => act.mutate({ id: analysis.id, step: "review" })} onApprove={() => act.mutate({ id: analysis.id, step: "approve" })} error={act.isError ? api.getErrorMessage(act.error) : ""}/>)}</div>;
}

function RatioCard({ analysis, canReview, assignedPartner, userId, busy, onReview, onApprove, error }: { analysis: AuditRatioAnalysis; canReview: boolean; assignedPartner: boolean; userId: string; busy: boolean; onReview: () => void; onApprove: () => void; error: string }) {
  return <section className={`${panel} overflow-hidden`}><header className="flex justify-between border-b border-slate-100 p-4"><div><h3 className="text-sm font-semibold">Ratio analysis v{analysis.version}</h3><p className="mt-1 text-xs text-slate-500">{analysis.risk_flag_ids.length} linked system review flags</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold">{analysis.status}</span></header><div className="overflow-x-auto"><table className="w-full min-w-[650px] text-left text-xs"><thead className="bg-slate-50 text-[10px] uppercase text-slate-500"><tr><th className="px-4 py-2">Ratio</th><th>Current year</th><th>Prior year</th><th>Movement</th><th>Review flag</th></tr></thead><tbody>{Object.entries(analysis.ratio_results.ratios).map(([name, ratio]) => <tr key={name} className={`border-t ${ratio.threshold_exceeded ? "bg-amber-50" : "border-slate-100"}`}><td className="px-4 py-2 capitalize">{name.replaceAll("_", " ")}</td><td>{ratio.current_year ?? ratio.unavailable_reason ?? "—"}</td><td>{ratio.prior_year ?? "—"}</td><td>{ratio.movement_pct ?? "—"}</td><td>{ratio.threshold_exceeded ? `SYSTEM_FLAGGED · ${ratio.risk_flag_id ?? "review"}` : "—"}</td></tr>)}</tbody></table></div><div className="flex gap-2 border-t p-4">{canReview && analysis.status === "DRAFT" && userId !== analysis.prepared_by && <button disabled={busy} onClick={onReview} className="rounded bg-violet-800 px-3 py-2 text-xs font-semibold text-white">Manager review</button>}{assignedPartner && analysis.status === "MANAGER_REVIEWED" && userId !== analysis.prepared_by && userId !== analysis.reviewed_by && <button disabled={busy} onClick={onApprove} className="rounded bg-emerald-800 px-3 py-2 text-xs font-semibold text-white">Partner approve</button>}{error && <ErrorText message={error}/>}</div></section>;
}
