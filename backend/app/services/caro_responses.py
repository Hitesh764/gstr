"""CARO draft assembly from recorded artifacts; makes no clause conclusions."""
from datetime import date

from app import models
from app.tenancy import TenantSession


def _fy_start(financial_year: str) -> date:
    return date(int(financial_year[:4]), 4, 1)


def build_source_snapshot(tenant: TenantSession, engagement: models.AuditEngagement) -> dict:
    flags = tenant.query(models.RiskFlag).filter(
        models.RiskFlag.engagement_id == engagement.id,
    ).order_by(models.RiskFlag.created_at).all()
    filings = tenant.query(models.StatutoryFilingRecord).filter(
        models.StatutoryFilingRecord.engagement_id == engagement.id,
        models.StatutoryFilingRecord.is_current.is_(True),
    ).order_by(models.StatutoryFilingRecord.period, models.StatutoryFilingRecord.tax_type).all()
    schedules = tenant.query(models.AgeingSchedule).filter(
        models.AgeingSchedule.engagement_id == engagement.id,
    ).order_by(models.AgeingSchedule.generated_at.desc()).all()
    latest_schedule = {}
    for row in schedules:
        latest_schedule.setdefault(row.schedule_type.value, row)
    confirmations = tenant.query(models.BankConfirmation).filter(
        models.BankConfirmation.engagement_id == engagement.id,
    ).all()
    ratios = tenant.query(models.AuditRatioAnalysis).filter(
        models.AuditRatioAnalysis.engagement_id == engagement.id,
    ).order_by(models.AuditRatioAnalysis.version.desc()).first()
    return {
        "disclaimer": "Recorded system data only; not independently verified or a CARO conclusion.",
        "risk_flags": [{"id": row.id, "type": row.flag_type.value, "status": row.status.value,
                        "note": row.system_note, "trigger_data": row.trigger_data} for row in flags],
        "statutory_filing_records": [{"id": row.id, "tax_type": row.tax_type.value,
             "period": row.period, "reported_status": row.filing_status.value,
             "source": row.filing_source.value, "source_reference": row.source_reference,
             "notes": row.notes} for row in filings],
        "latest_ageing_schedules": [{"id": row.id, "type": kind,
             "source_version": row.source_version, "data": row.data}
             for kind, row in latest_schedule.items()],
        "bank_confirmations": [{"id": row.id, "status": row.letter_status.value,
             "account_number_last4": row.account_number_last4,
             "reconciled_amount": str(row.confirmed_balance)
             if row.confirmed_balance is not None else None,
             "discrepancy_notes": row.discrepancy_notes} for row in confirmations],
        "ratio_analysis": ({"id": ratios.id, "version": ratios.version,
                            "status": ratios.status.value, "ratio_results": ratios.ratio_results}
                           if ratios else None),
    }


def make_draft(clause_number: int, title: str, clause_text: str,
               source_snapshot: dict) -> str:
    """Create clearly labelled language for CA consideration, never a conclusion."""
    counts = {
        "system risk flags": len(source_snapshot["risk_flags"]),
        "current statutory filing records": len(source_snapshot["statutory_filing_records"]),
        "ageing schedules": len(source_snapshot["latest_ageing_schedules"]),
        "bank confirmations": len(source_snapshot["bank_confirmations"]),
        "ratio analyses": int(source_snapshot["ratio_analysis"] is not None),
    }
    available = ", ".join(f"{count} {label}" for label, count in counts.items() if count)
    if not available:
        available = "no linked evidence records currently captured in this module"
    return (
        f"SYSTEM DRAFT — CARO clause {clause_number}: {title}. "
        f"Versioned clause reference: {clause_text} "
        f"The system has recorded {available} for CA consideration. "
        "Verify completeness and source evidence, assess the clause requirements and applicability, "
        "and author the reporting language. These counts do not establish compliance, non-compliance, "
        "or any other audit conclusion. This draft must not be used in a report."
    )
