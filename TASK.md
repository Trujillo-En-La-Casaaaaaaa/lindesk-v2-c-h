Create a complete, runnable multi-repository e-commerce ecosystem named ShopFlow.

ARCHITECTURAL CONSTRAINT

Implement the system at HIGH ARCHITECTURAL COMPLEXITY as a microservices architecture.

Create exactly these six repositories:

1. shopflow-web
2. shopflow-gateway
3. shopflow-orders
4. shopflow-inventory
5. shopflow-notifications
6. shopflow-infra

REQUIRED ARCHITECTURAL BOUNDARIES

- shopflow-web communicates through shopflow-gateway.
- shopflow-orders owns order lifecycle and order persistence.
- shopflow-inventory owns inventory and stock changes.
- shopflow-notifications owns customer-notification orchestration.
- services must interact through explicit service interfaces.
- services must not share writable database ownership.
- shopflow-infra provides Docker-based local execution and a deterministic external notification-provider emulator.

FUNCTIONAL REQUIREMENTS

The ecosystem must support:

1. Product catalog.
2. Product inventory.
3. Order creation.
4. Inventory validation before order creation.
5. Inventory decrement after successful order creation.
6. Order detail/status view.
7. Administrative action to mark an order as SHIPPED.
8. Order-confirmation notification.
9. Customer order cancellation.

ORDER CANCELLATION RULES

A customer may cancel an order that has not been shipped.

Cancellation must:

- require a non-empty reason of at most 200 characters;
- change the order status to CANCELLED;
- store cancelledAt;
- store cancellationReason;
- restore inventory exactly once;
- send a cancellation notification;
- reject cancellation of SHIPPED orders;
- safely handle repeated cancellation requests without restoring inventory twice.

The cancellation workflow must preserve the architectural ownership rules above.

REPRODUCIBILITY AND QUALITY REQUIREMENTS

- Provide deterministic seed data.
- Provide Docker-based local execution.
- Provide dependency lockfiles.
- Provide automated tests.
- Do not depend on production internet services.
- Do not create additional repositories.
- Do not leave TODOs or placeholder implementations.
- Verify the complete ecosystem before finishing.

Do not add business features that are not required by this task.
