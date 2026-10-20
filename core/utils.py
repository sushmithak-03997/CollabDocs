import uuid
from datetime import datetime, time

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from .exceptions import SimulatedFailure
from .models import AuditLog, DocumentVersion


def log_audit(actor, action, model_name, object_id, workspace=None, details=None):
    """Always call this inside the same transaction.atomic() block as the action it records."""
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        model_name=model_name,
        object_id=object_id,
        workspace=workspace,
        details=details or {},
    )


def create_version(document, user):
    """Snapshot the document. version_number = Document.versions.count() + 1 (per-document numbering).
    Call inside transaction.atomic()."""
    number = document.versions.count() + 1
    version = DocumentVersion.objects.create(
        document=document,
        version_number=number,
        title=document.title,
        content=document.content,
        edited_by=user,
    )
    document.version_count = number  # keep the annotated attribute fresh for the response
    return version


def maybe_simulate_failure(request, stage):
    """Demo helper: with DEBUG=True and header `X-Simulate-Failure: true`, blow up mid-transaction."""
    if settings.DEBUG and request.headers.get("X-Simulate-Failure", "").lower() == "true":
        raise SimulatedFailure(f"Simulated failure ({stage}). Transaction rolled back - nothing was saved.")


def parse_uuid_param(value, name):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValidationError({name: f"'{value}' is not a valid UUID."})


def parse_datetime_param(value, name, end_of_day=False):
    """Accepts ISO datetime or plain date. A plain date used as an upper bound means 'end of that day'."""
    try:
        dt = parse_datetime(value)
        if dt is None:
            d = parse_date(value)
            if d is None:
                raise ValueError
            dt = datetime.combine(d, time.max if end_of_day else time.min)
    except ValueError:
        raise ValidationError({name: "Invalid date. Use ISO format, e.g. 2026-01-31 or 2026-01-31T10:00:00Z."})
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt
