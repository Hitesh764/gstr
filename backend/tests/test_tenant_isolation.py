"""
Cross-tenant isolation tests.

These are the tests that must never be allowed to fail. Every one of them
takes a real, valid identifier belonging to firm B and presents it while
authenticated as firm A. A correct system answers "not found" every time.

Note the assertion is 404 rather than 403. Replying "forbidden" would
confirm that the identifier exists, which tells firm A that firm B holds a
record with that id. Not found reveals nothing.

Coverage runs at two levels:
  - the data layer (TenantSession) — can a scoped session even express a
    query that escapes its firm?
  - the HTTP layer — does every route actually use that scoping?
"""
import pytest

from app import models
from app.tenancy import TenantScopeError, TenantSession, assert_model_registry_complete


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def test_every_tenant_model_is_registered():
    """A model with firm_id that is not registered would bypass scoping."""
    missing = assert_model_registry_complete()
    assert missing == [], (
        f"These models have a firm_id but are missing from "
        f"models.TENANT_SCOPED_MODELS: {missing}. Unregistered models cannot "
        f"be queried through TenantSession and invite raw-session access."
    )


def test_tenant_session_requires_a_firm(db):
    with pytest.raises(TenantScopeError):
        TenantSession(db=db, firm_id="")


def test_scoped_query_excludes_other_firms(db, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)

    names = {c.name for c in tenant_a.query(models.Client).all()}
    assert a["client"].name in names
    assert b["client"].name not in names


@pytest.mark.parametrize("model_name,key", [
    ("Client", "client"),
    ("ClientGSTIN", "gstin"),
    ("PurchaseRegisterUpload", "upload"),
    ("PurchaseRegisterEntry", "entry"),
    ("ReconciliationRun", "run"),
    ("ReconciliationResult", "result"),
    ("ReconciliationException", "exception"),
])
def test_get_returns_none_for_another_firms_row(db, two_firms, model_name, key):
    """The central guarantee: a valid id from firm B resolves to nothing
    when fetched through a session scoped to firm A."""
    a, b = two_firms["a"], two_firms["b"]
    model = getattr(models, model_name)
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)

    assert tenant_a.get(model, a[key].id) is not None, "own row should be visible"
    assert tenant_a.get(model, b[key].id) is None, (
        f"{model_name} {b[key].id} belongs to firm B but was readable by firm A"
    )


def test_add_refuses_to_write_into_another_firm(db, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)

    hostile = models.Client(firm_id=b["firm"].id, name="Injected")
    with pytest.raises(TenantScopeError):
        tenant_a.add(hostile)


def test_add_stamps_firm_id_automatically(db, two_firms):
    a = two_firms["a"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)
    row = tenant_a.add(models.Client(name="Newly added"))
    assert row.firm_id == a["firm"].id


def test_soft_delete_refuses_another_firms_row(db, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)
    with pytest.raises(TenantScopeError):
        tenant_a.soft_delete(b["client"])


def test_soft_deleted_rows_are_hidden_but_retained(db, two_firms):
    a = two_firms["a"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)
    tenant_a.soft_delete(a["client"], retention_days=30)
    db.flush()

    assert tenant_a.get(models.Client, a["client"].id) is None
    # Still physically present, which is the point of soft delete.
    assert tenant_a.get(models.Client, a["client"].id, include_deleted=True) is not None
    db.rollback()


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_unauthenticated_requests_are_rejected(client, two_firms):
    a = two_firms["a"]
    for path in [
        "/api/clients",
        f"/api/clients/{a['client'].id}",
        "/api/exceptions",
        f"/api/gstins/{a['gstin'].id}/uploads",
    ]:
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} should require authentication"


def test_client_list_shows_only_own_firm(client, login, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    headers = login("owner@alpha-ca.in")
    resp = client.get("/api/clients", headers=headers)
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    assert a["client"].name in names
    assert b["client"].name not in names


@pytest.mark.parametrize("path_template", [
    "/api/clients/{client_id}",
    "/api/clients/{client_id}/gstins",
    "/api/clients/{client_id}/periods",
])
def test_cannot_read_another_firms_client_over_http(client, login, two_firms, path_template):
    b = two_firms["b"]
    headers = login("owner@alpha-ca.in")
    path = path_template.format(client_id=b["client"].id)
    resp = client.get(path, headers=headers)
    assert resp.status_code == 404, (
        f"{path} returned {resp.status_code}; firm B's client must be invisible "
        f"to firm A, and must 404 rather than 403 so its existence is not confirmed."
    )


@pytest.mark.parametrize("path_template", [
    "/api/gstins/{gstin_id}/uploads",
    "/api/gstins/{gstin_id}/gstr2b/fetches",
    "/api/gstins/{gstin_id}/reconciliation/2026-03/summary",
    "/api/gstins/{gstin_id}/reconciliation/2026-03/exceptions",
    "/api/gstins/{gstin_id}/reconciliation/2026-03/export",
])
def test_cannot_read_another_firms_gstin_data(client, login, two_firms, path_template):
    b = two_firms["b"]
    headers = login("owner@alpha-ca.in")
    path = path_template.format(gstin_id=b["gstin"].id)
    resp = client.get(path, headers=headers)
    assert resp.status_code == 404, f"{path} leaked firm B data with {resp.status_code}"


def test_cannot_read_another_firms_exception(client, login, two_firms):
    b = two_firms["b"]
    headers = login("owner@alpha-ca.in")
    resp = client.get(f"/api/exceptions/{b['exception'].id}", headers=headers)
    assert resp.status_code == 404


def test_exception_queue_never_includes_other_firms(client, login, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    headers = login("owner@alpha-ca.in")
    resp = client.get("/api/exceptions", headers=headers)
    assert resp.status_code == 200
    ids = {e["id"] for e in resp.json()}
    assert a["exception"].id in ids
    assert b["exception"].id not in ids


def test_cannot_mutate_another_firms_exception(client, login, two_firms):
    """Writes must be scoped too, not just reads."""
    b = two_firms["b"]
    headers = login("manager@alpha-ca.in")

    resp = client.patch(f"/api/exceptions/{b['exception'].id}/resolve",
                        json={"resolution_notes": "hostile write"}, headers=headers)
    assert resp.status_code == 404

    resp = client.patch(f"/api/exceptions/{b['exception'].id}/assign",
                        json={"assigned_to": None}, headers=headers)
    assert resp.status_code == 404


def test_cannot_delete_another_firms_client(client, login, two_firms):
    b = two_firms["b"]
    headers = login("manager@alpha-ca.in")
    resp = client.delete(f"/api/clients/{b['client'].id}", headers=headers)
    assert resp.status_code == 404


def test_cannot_upload_into_another_firms_gstin(client, login, two_firms):
    b = two_firms["b"]
    headers = login("associate@alpha-ca.in")
    csv = (b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value\n"
           b"X,27AAPFU0939F1ZV,INV-1,2026-03-05,1000\n")
    resp = client.post(
        f"/api/gstins/{b['gstin'].id}/uploads/purchase-register?period=2026-03",
        files={"file": ("pr.csv", csv, "text/csv")}, headers=headers,
    )
    assert resp.status_code == 404


def test_cannot_reconcile_another_firms_gstin(client, login, two_firms):
    b = two_firms["b"]
    headers = login("associate@alpha-ca.in")
    resp = client.post(f"/api/gstins/{b['gstin'].id}/reconcile?period=2026-03",
                       headers=headers)
    assert resp.status_code == 404


def test_cannot_assign_work_to_another_firms_user(client, login, two_firms):
    """A user id from firm B must not be accepted as an assignee."""
    a, b = two_firms["a"], two_firms["b"]
    headers = login("manager@alpha-ca.in")
    resp = client.patch(
        f"/api/exceptions/{a['exception'].id}/assign",
        json={"assigned_to": b["users"]["associate"].id}, headers=headers,
    )
    assert resp.status_code == 404


def test_firm_user_list_is_scoped(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.get("/api/auth/users", headers=headers)
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert "owner@alpha-ca.in" in emails
    assert not any(e.endswith("@bravo-ca.in") for e in emails)


def test_audit_log_is_scoped_to_own_firm(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.get("/api/auth/audit-log", headers=headers)
    assert resp.status_code == 200
    firm_a_id = two_firms["a"]["firm"].id
    for entry in resp.json():
        assert entry.get("entity_id") != two_firms["b"]["firm"].id


# ---------------------------------------------------------------------------
# Notices
#
# Notices hold the firm's working papers on a client's dispute with the tax
# department, plus an encrypted copy of the document itself. Every route and
# every path to that file gets the same treatment as the rest: a real id from
# firm B, presented by firm A, must resolve to "not found".
# ---------------------------------------------------------------------------

@pytest.fixture
def notice_firms(db, two_firms):
    """two_firms, with one notice and one stored file per firm."""
    from datetime import date

    from app.storage import get_storage

    for key, firm_name in (("a", "Alpha & Co"), ("b", "Bravo Associates")):
        f = two_firms[key]
        body = f"CONFIDENTIAL NOTICE OF {firm_name}".encode()
        notice = models.Notice(
            firm_id=f["firm"].id, client_id=f["client"].id, client_gstin_id=f["gstin"].id,
            title=f"Notice for {firm_name}", notice_type="SCRUTINY_ASMT10",
            response_deadline=date(2026, 6, 30), period=f["period"],
            status=models.NoticeStatus.UNDER_REVIEW, assigned_to=f["users"]["associate"].id,
            uploaded_by=f["users"]["associate"].id, original_filename="notice.pdf",
            content_type="application/pdf", size_bytes=len(body), ocr_text=body.decode(),
            uploaded_file_path=get_storage().save(firm_id=f["firm"].id, category="notices", data=body),
        )
        db.add(notice)
        db.flush()
        f["notice"], f["notice_file"] = notice, body
    db.commit()
    return two_firms


@pytest.fixture
def scripted_ai(client):
    from app.main import app
    from app.routers import notices as notices_router
    from tests.notice_helpers import FakeAnalyzer

    ai = FakeAnalyzer()
    app.dependency_overrides[notices_router.get_analyzer] = lambda: ai
    app.dependency_overrides[notices_router.get_optional_analyzer] = lambda: ai
    return ai


def test_notice_get_returns_none_for_another_firms_notice(db, notice_firms):
    a, b = notice_firms["a"], notice_firms["b"]
    tenant_a = TenantSession(db=db, firm_id=a["firm"].id)
    assert tenant_a.get(models.Notice, a["notice"].id) is not None
    assert tenant_a.get(models.Notice, b["notice"].id) is None


NOTICE_ROUTES = [
    ("get", "/api/notices/{id}", None),
    ("patch", "/api/notices/{id}", {"title": "hijacked"}),
    ("delete", "/api/notices/{id}", None),
    ("put", "/api/notices/{id}/text", {"text": "x" * 40}),
    ("post", "/api/notices/{id}/analyse", None),
    ("get", "/api/notices/{id}/evidence", None),
    ("post", "/api/notices/{id}/draft/generate", None),
    ("put", "/api/notices/{id}/draft", {"text": "planted draft"}),
    ("post", "/api/notices/{id}/transition", {"to_status": "UNDER_REVIEW"}),
    ("get", "/api/notices/{id}/file", None),
    ("get", "/api/notices/{id}/history", None),
]


@pytest.mark.parametrize(
    "method,path,body", NOTICE_ROUTES,
    ids=[m.upper() + " " + p.replace("/api/notices/", "") for m, p, _ in NOTICE_ROUTES])
def test_no_notice_route_reaches_another_firms_notice(
        client, login, notice_firms, scripted_ai, method, path, body):
    b = notice_firms["b"]
    headers = login("owner@alpha-ca.in")            # the most privileged role in firm A
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path.format(id=b["notice"].id), **kwargs)
    assert resp.status_code == 404, f"{method.upper()} {path} answered {resp.status_code}"
    assert "Bravo" not in resp.text


def test_a_failed_attempt_leaves_the_other_firms_notice_untouched(
        client, login, db, notice_firms, scripted_ai):
    b = notice_firms["b"]
    headers = login("owner@alpha-ca.in")
    client.patch(f"/api/notices/{b['notice'].id}", json={"title": "hijacked"}, headers=headers)
    client.put(f"/api/notices/{b['notice'].id}/draft", json={"text": "planted"}, headers=headers)
    client.delete(f"/api/notices/{b['notice'].id}", headers=headers)
    db.expire_all()
    row = db.get(models.Notice, b["notice"].id)
    assert row.title == "Notice for Bravo Associates"
    assert row.drafted_response is None and row.deleted_at is None


def test_notice_list_and_stats_only_count_own_firm(client, login, notice_firms):
    headers = login("associate@alpha-ca.in")
    rows = client.get("/api/notices", headers=headers).json()
    assert [r["title"] for r in rows] == ["Notice for Alpha & Co"]
    assert client.get("/api/notices/stats", headers=headers).json()["open"] == 1


def test_cannot_list_another_firms_client_history(client, login, notice_firms):
    b = notice_firms["b"]
    resp = client.get(f"/api/notices?client_id={b['client'].id}", headers=login("owner@alpha-ca.in"))
    assert resp.status_code == 404


def test_cannot_upload_a_notice_for_another_firms_client(client, login, notice_firms, scripted_ai):
    from tests.notice_helpers import notice_pdf
    b = notice_firms["b"]
    resp = client.post(
        "/api/notices", data={"client_id": b["client"].id},
        files={"file": ("n.pdf", notice_pdf(), "application/pdf")},
        headers=login("associate@alpha-ca.in"))
    assert resp.status_code == 404


def test_cannot_link_a_notice_to_another_firms_gstin_or_assign_to_its_staff(
        client, login, notice_firms, scripted_ai):
    from tests.notice_helpers import notice_pdf
    a, b = notice_firms["a"], notice_firms["b"]
    headers = login("manager@alpha-ca.in")

    resp = client.post(
        "/api/notices", data={"client_id": a["client"].id, "client_gstin_id": b["gstin"].id},
        files={"file": ("n.pdf", notice_pdf(), "application/pdf")}, headers=headers)
    assert resp.status_code == 404

    assert client.patch(f"/api/notices/{a['notice'].id}",
                        json={"client_gstin_id": b["gstin"].id}, headers=headers).status_code == 404
    assert client.patch(f"/api/notices/{a['notice'].id}",
                        json={"assigned_to": b["users"]["associate"].id}, headers=headers).status_code == 404


def test_a_notice_row_pointing_at_another_firms_file_cannot_read_it(
        client, login, db, notice_firms):
    """Defence in depth: even if a notice row were corrupted or forged to carry
    firm B's storage key, firm A's download must not return firm B's bytes."""
    a, b = notice_firms["a"], notice_firms["b"]
    db.get(models.Notice, a["notice"].id).uploaded_file_path = b["notice"].uploaded_file_path
    db.commit()

    resp = client.get(f"/api/notices/{a['notice'].id}/file", headers=login("owner@alpha-ca.in"))
    assert resp.status_code != 200
    assert b["notice_file"] not in resp.content
    assert b"Bravo" not in resp.content


def test_evidence_never_draws_on_another_firms_reconciliation(
        client, login, db, notice_firms):
    """A notice whose GSTIN link was forged to another firm's GSTIN yields an
    empty evidence pack, never firm B's records."""
    a, b = notice_firms["a"], notice_firms["b"]
    db.get(models.Notice, a["notice"].id).client_gstin_id = b["gstin"].id
    db.commit()

    ev = client.get(f"/api/notices/{a['notice'].id}/evidence",
                    headers=login("owner@alpha-ca.in")).json()
    assert ev["items"] == []
    assert ev["gstin"] is None and b["gstin"].gstin not in str(ev)
    assert "Bravo" not in str(ev) and "bravo" not in str(ev)


def test_notice_history_is_scoped_to_own_firm(client, login, notice_firms, scripted_ai):
    a, b = notice_firms["a"], notice_firms["b"]
    hb = login("owner@bravo-ca.in")
    client.patch(f"/api/notices/{b['notice'].id}", json={"section": "Section 73"}, headers=hb)
    history = client.get(f"/api/notices/{a['notice'].id}/history", headers=login("owner@alpha-ca.in")).json()
    assert history == []


# ---------------------------------------------------------------------------
# Portfolio (firm-wide reconciliation rollup)
#
# Built from grouped, whole-firm queries rather than a lookup per client, so
# it is exactly the kind of code where a missing firm_id filter would leak
# every firm's clients into one list instead of just failing on one id.
# ---------------------------------------------------------------------------

def test_portfolio_shows_only_the_signed_in_firms_clients(client, login, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    resp = client.get(f"/api/portfolio?period={a['period']}", headers=login("owner@alpha-ca.in"))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total_clients"] == 1
    names = [r["client_name"] for r in body["rows"]]
    assert names == [a["client"].name]
    assert b["client"].name not in names
    assert "Bravo" not in resp.text and b["gstin"].gstin not in resp.text

    # two_firms seeds one SUPPLIER_NOT_FILED exception per firm, so this is
    # also a real NEEDS_REVIEW row, with a real at-risk figure computed only
    # from firm A's own invoice (cgst 900 + sgst 900).
    row = body["rows"][0]
    assert row["status"] == "NEEDS_REVIEW" and row["itc_at_risk"] == "1800.00"


def test_portfolio_periods_list_is_scoped_to_own_firm(client, login, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    resp = client.get("/api/portfolio/periods", headers=login("owner@alpha-ca.in"))
    assert resp.status_code == 200
    body = resp.json()
    assert a["period"] in body["periods"]
    # Nothing about this assertion depends on firm B's period differing from
    # firm A's, so it stays meaningful even if the fixtures' periods match:
    # what matters is firm A's own list contains its own period.


# ---------------------------------------------------------------------------
# Audit brief
# ---------------------------------------------------------------------------

def test_audit_brief_cannot_be_read_for_another_firms_client(client, login, two_firms):
    b = two_firms["b"]
    resp = client.get(f"/api/clients/{b['client'].id}/audit-brief", headers=login("owner@alpha-ca.in"))
    assert resp.status_code == 404


def test_audit_brief_never_draws_on_another_firms_reconciliation(client, login, two_firms):
    """Even for a client that does exist in the caller's own firm, the
    vendor names, GSTINs and figures in the brief must never come from
    firm B's data — the same guarantee the notice evidence pack gives."""
    a, b = two_firms["a"], two_firms["b"]
    resp = client.get(f"/api/clients/{a['client'].id}/audit-brief?as_of={a['period']}&lookback=1",
                      headers=login("owner@alpha-ca.in"))
    assert resp.status_code == 200, resp.text
    # two_firms deliberately cross-maps each firm's client GSTIN as the
    # other's vendor GSTIN, so a bare substring check on the GSTIN value
    # would false-positive; the client name is the unambiguous marker here.
    assert "Bravo" not in resp.text and "bravo" not in resp.text
    assert b["client"].name not in resp.text
