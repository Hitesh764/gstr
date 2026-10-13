"""
Regression tests for the audit hash chain under several entries per request.

The chain was written assuming one `record()` per session. Sessions run with
autoflush off, so a second call in the same request could not see the first,
read the same head, and fork the chain. `verify_chain` then reported tampering
that had not happened. Requests that audit twice (an upload followed by its
analysis, an approval followed by an assignment) hit it.
"""
from datetime import datetime
from unittest import mock

from app import audit, models


def test_several_entries_in_one_session_still_form_one_chain(db, two_firms):
    firm_id = two_firms["a"]["firm"].id
    for n in range(6):
        audit.record(db, firm_id=firm_id, action="TEST_ACTION", metadata={"n": n})
    db.commit()

    report = audit.verify_chain(db, firm_id)
    assert report["valid"] is True, report
    assert report["checked"] == 6

    rows = (db.query(models.AuditLog).filter(models.AuditLog.firm_id == firm_id)
            .order_by(models.AuditLog.created_at.asc()).all())
    assert len({r.prev_hash for r in rows}) == 6           # every entry has its own predecessor


def test_entries_in_the_same_clock_tick_keep_a_strict_order(db, two_firms):
    """Identical timestamps would be tie-broken by random ids, which need not
    match the order the entries chain in."""
    firm_id = two_firms["a"]["firm"].id
    frozen = datetime(2026, 1, 1, 12, 0, 0)
    with mock.patch("app.audit.datetime") as fake:
        fake.utcnow.return_value = frozen
        for n in range(20):
            audit.record(db, firm_id=firm_id, action="TICK", metadata={"n": n})
    db.commit()

    rows = (db.query(models.AuditLog).filter(models.AuditLog.firm_id == firm_id)
            .order_by(models.AuditLog.created_at.asc()).all())
    stamps = [r.created_at for r in rows]
    assert stamps == sorted(set(stamps))                    # strictly increasing, no ties
    assert audit.verify_chain(db, firm_id)["valid"] is True


def test_tampering_is_still_detected(db, two_firms):
    firm_id = two_firms["a"]["firm"].id
    for n in range(4):
        audit.record(db, firm_id=firm_id, action="TEST_ACTION", metadata={"n": n})
    db.commit()
    victim = (db.query(models.AuditLog).filter(models.AuditLog.firm_id == firm_id)
              .order_by(models.AuditLog.created_at.asc()).all())[1]
    victim.meta = {"n": 999}
    db.commit()
    assert audit.verify_chain(db, firm_id)["valid"] is False
