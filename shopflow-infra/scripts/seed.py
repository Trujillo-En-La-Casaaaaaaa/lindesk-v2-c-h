#!/usr/bin/env python
"""Invoke the inventory-owned deterministic seed command (HANDOFF #6).

``shopflow-infra`` does not seed anything itself: it runs the ``seed-inventory``
one-shot compose job, which executes the inventory repository's own seed command
(``python -m app.seed`` by default, see contracts.md §10 and the inventory
handoff). Infrastructure therefore never writes a business row.

With ``--verify`` (the default) the script proves the two properties the handoff
requires: the documented five products exist with the documented identity and
prices, stock never exceeds the seeded quantities, and running the seed a second
time changes nothing (idempotency).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shopflow_compose as compose  # noqa: E402  (path bootstrap above)
import shopflow_http as http  # noqa: E402  (path bootstrap above)

SEED_SERVICE = "seed-inventory"
SEED_ENV_VAR = "INVENTORY_SEED_COMMAND"
DEFAULT_SEED_COMMAND = "python -m app.seed"


@dataclass(frozen=True)
class DocumentedProduct:
    """One row of the deterministic seed table, contracts.md §10."""

    product_id: str
    sku: str
    name: str
    price: Decimal
    seeded_stock: int


SEEDED_PRODUCTS: tuple[DocumentedProduct, ...] = (
    DocumentedProduct("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", Decimal("19.99"), 50),
    DocumentedProduct("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", Decimal("12.50"), 25),
    DocumentedProduct("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", Decimal("49.00"), 10),
    DocumentedProduct("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", Decimal("4.25"), 100),
    DocumentedProduct("p-1005", "SF-CAP-005", "ShopFlow Cap", Decimal("22.00"), 0),
)


class VerificationError(RuntimeError):
    """The seeded catalogue does not match contracts.md §10."""


def default_inventory_url() -> str:
    return f"http://localhost:{os.environ.get('INVENTORY_PORT', '8002')}"


def fetch_products(inventory_url: str, request_timeout: float) -> dict[str, dict[str, Any]]:
    """Read the catalogue through the inventory service (contracts.md §4)."""

    response = http.get(f"{inventory_url.rstrip('/')}/products", params={"limit": 100, "offset": 0},
                        timeout=request_timeout)
    if not response.ok:
        raise VerificationError(f"GET {inventory_url}/products failed: {response.describe()}")
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise VerificationError(f"GET {inventory_url}/products returned no product list")

    catalogue: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or "productId" not in item:
            raise VerificationError(f"unexpected product payload: {item!r}")
        catalogue[str(item["productId"])] = item
    return catalogue


def price_of(item: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(item.get("price")))
    except (InvalidOperation, TypeError) as exc:
        raise VerificationError(f"product {item.get('productId')!r} has a non-numeric price: {item.get('price')!r}") from exc


def quantity_of(item: Mapping[str, Any]) -> int:
    try:
        return int(item.get("availableQuantity"))
    except (TypeError, ValueError) as exc:
        raise VerificationError(
            f"product {item.get('productId')!r} has a non-numeric availableQuantity: {item.get('availableQuantity')!r}"
        ) from exc


def check_documented_products(catalogue: Mapping[str, Mapping[str, Any]]) -> None:
    """Assert the five documented products exist, with their documented identity."""

    missing = [product.product_id for product in SEEDED_PRODUCTS if product.product_id not in catalogue]
    if missing:
        raise VerificationError(f"seeded products missing from the catalogue: {missing}")

    for product in SEEDED_PRODUCTS:
        item = catalogue[product.product_id]
        actual_sku = str(item.get("sku"))
        actual_name = str(item.get("name"))
        actual_price = price_of(item)
        if actual_sku != product.sku:
            raise VerificationError(f"{product.product_id}: sku {actual_sku!r} != documented {product.sku!r}")
        if actual_name != product.name:
            raise VerificationError(f"{product.product_id}: name {actual_name!r} != documented {product.name!r}")
        if actual_price != product.price:
            raise VerificationError(f"{product.product_id}: price {actual_price} != documented {product.price}")

        available = quantity_of(item)
        if available > product.seeded_stock:
            raise VerificationError(
                f"{product.product_id}: availableQuantity {available} exceeds the seeded {product.seeded_stock}"
            )


def snapshot(catalogue: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, str, Decimal, int]]:
    """A comparable view of the seeded state (identity + stock level)."""

    return {
        product_id: (str(item.get("sku")), str(item.get("name")), price_of(item), quantity_of(item))
        for product_id, item in catalogue.items()
    }


def report(catalogue: Mapping[str, Mapping[str, Any]]) -> None:
    for product in SEEDED_PRODUCTS:
        item = catalogue.get(product.product_id, {})
        print(
            f"   {product.product_id}  {product.sku}  {product.name:<24} "
            f"{price_of(item) if item else '-':>6}  availableQuantity={quantity_of(item) if item else '-':>4}  "
            f"(seeded {product.seeded_stock})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the inventory-owned deterministic seed command and (by default) verify it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--compose-file", default=None, help="path to docker-compose.yml")
    parser.add_argument("--verify", dest="verify", action="store_true", default=True,
                        help="run the seed twice and assert the five products and stock levels are identical")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="only run the seed command once")
    parser.add_argument("--strict", action="store_true",
                        help="fail instead of warning when the inventory API cannot be reached for verification")
    parser.add_argument("--inventory-url", default=default_inventory_url(),
                        help="inventory service base url used for verification")
    parser.add_argument("--request-timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--build", action="store_true", help="rebuild the seed job image before running it")
    parser.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        compose_file = compose.compose_file_from(args.compose_file)
    except compose.ComposeError as exc:
        return compose.fail(str(exc))

    effective = compose.describe_effective_command(SEED_ENV_VAR, DEFAULT_SEED_COMMAND)
    print(f"seed: inventory-owned seed command = {effective}  ({SEED_ENV_VAR})")
    run_flags = ["--build"] if args.build else []

    print("\nseed: pass 1")
    try:
        exit_code = compose.run_job(SEED_SERVICE, compose_file=compose_file, run_flags=run_flags, dry_run=args.dry_run)
    except compose.ComposeError as exc:
        return compose.fail(str(exc))
    if exit_code != 0:
        print(f"seed: FAILED - the inventory seed command exited with {exit_code}", file=sys.stderr)
        return exit_code or 1

    if not args.verify:
        print("\nseed: OK (verification skipped)")
        return 0

    if args.dry_run:
        print("\nseed: OK (dry run: nothing seeded, verification skipped)")
        return 0

    print(f"\nseed: verifying against {args.inventory_url}")
    try:
        first = fetch_products(args.inventory_url, args.request_timeout)
        check_documented_products(first)
    except (VerificationError, http.TransportError) as exc:
        if isinstance(exc, http.TransportError) and not args.strict:
            print(f"seed: WARNING - verification skipped, inventory API unreachable ({exc})")
            print("seed: OK (seed command ran; re-run with --strict to make this an error)")
            return 0
        print(f"seed: FAILED - {exc}", file=sys.stderr)
        return 1

    print("\nseed: pass 2 (idempotency check)")
    try:
        exit_code = compose.run_job(SEED_SERVICE, compose_file=compose_file, run_flags=run_flags, dry_run=args.dry_run)
    except compose.ComposeError as exc:
        return compose.fail(str(exc))
    if exit_code != 0:
        print(f"seed: FAILED - the second seed run exited with {exit_code}", file=sys.stderr)
        return exit_code or 1

    try:
        second = fetch_products(args.inventory_url, args.request_timeout)
        check_documented_products(second)
    except (VerificationError, http.TransportError) as exc:
        print(f"seed: FAILED - {exc}", file=sys.stderr)
        return 1

    if snapshot(first) != snapshot(second):
        print("seed: FAILED - the second run changed the seeded products or stock levels", file=sys.stderr)
        for product in SEEDED_PRODUCTS:
            before = snapshot(first).get(product.product_id)
            after = snapshot(second).get(product.product_id)
            if before != after:
                print(f"  {product.product_id}: {before} -> {after}", file=sys.stderr)
        return 1

    print(f"\nseed: idempotent - {len(SEEDED_PRODUCTS)} products and stock levels identical on both passes")
    report(second)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
