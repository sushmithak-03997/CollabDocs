from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import AuditLog, Document


@receiver(pre_save, sender=Document)
def remember_if_adding(sender, instance, **kwargs):
    # Django flips `instance._state.adding` to False *before* post_save fires, so the
    # create-vs-update flag must be captured here, while it is still accurate.
    instance._was_adding = instance._state.adding


@receiver(post_save, sender=Document)
def write_document_audit_log(sender, instance, created, **kwargs):
    """Runs synchronously inside the caller's transaction.atomic() block, so the AuditLog row
    commits or rolls back together with the document save."""
    was_adding = getattr(instance, "_was_adding", created)
    actor = getattr(instance, "_actor", None) or instance.created_by
    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.Action.CREATED if was_adding else AuditLog.Action.UPDATED,
        model_name="Document",
        object_id=instance.pk,
        workspace_id=instance.workspace_id,
        details={"title": instance.title, "status": instance.status},
    )
