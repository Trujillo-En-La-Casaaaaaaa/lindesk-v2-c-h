"""Background relays of shopflow-orders.

* ``outbox_publisher`` - publishes unpublished outbox rows to RabbitMQ with
  publisher confirms (contracts.md §8);
* ``release_reconciler`` - retries pending inventory releases and ambiguous
  reservations, which is what makes cancelling durable across restarts.

Both run as background tasks of the service process (see ``app/main.py``) and can
also be started standalone with ``python -m app.workers.<module>``.
"""

from app.workers.outbox_publisher import OutboxPublisher, PublishReport
from app.workers.release_reconciler import ReconcileReport, ReleaseReconciler

__all__ = ["OutboxPublisher", "PublishReport", "ReconcileReport", "ReleaseReconciler"]
