"""Deterministic seed command of shopflow-inventory (contracts.md §10).

    python -m app.seed

The command loads exactly the five documented products and their documented stock
levels. It is idempotent and non-inflating:

* a product is upserted by its primary key and its sku stays unique, so repeated
  runs never create duplicates;
* stock is set with ``GREATEST(current, documented)``: the documented level is
  restored (so a drained catalogue converges back to §10) but the run can never
  add on top of an existing quantity, so stock never exceeds the seeded value and
  ``p-1005`` stays at zero.

``shopflow-infra`` invokes exactly this entry point (``seed-inventory`` ->
``python -m app.seed``) and then verifies the five products and their stock.
Nothing else in the system writes the catalog.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, load_settings
from app.db.models import ProductRow, StockRow
from app.db.session import create_engine_from_settings, create_session_factory
from app.domain.product import Product


@dataclass(frozen=True, slots=True)
class SeedProduct:
    """One row of the deterministic seed table (contracts.md §10)."""

    product: Product
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("seeded stock must not be negative")


#: The five documented products and their documented stock levels (contracts.md §10).
#: ``p-1005`` has zero stock on purpose, so the insufficient-stock path is deterministic.
SEED_PRODUCTS: tuple[SeedProduct, ...] = (
    SeedProduct(
        product=Product(
            product_id="p-1001",
            sku="SF-TEE-001",
            name="ShopFlow Classic Tee",
            description="Heavyweight cotton t-shirt.",
            price=Decimal("19.99"),
            currency="USD",
        ),
        quantity=50,
    ),
    SeedProduct(
        product=Product(
            product_id="p-1002",
            sku="SF-MUG-002",
            name="ShopFlow Ceramic Mug",
            description="350 ml ceramic mug.",
            price=Decimal("12.50"),
            currency="USD",
        ),
        quantity=25,
    ),
    SeedProduct(
        product=Product(
            product_id="p-1003",
            sku="SF-HOOD-003",
            name="ShopFlow Hoodie",
            description="Brushed fleece hoodie.",
            price=Decimal("49.00"),
            currency="USD",
        ),
        quantity=10,
    ),
    SeedProduct(
        product=Product(
            product_id="p-1004",
            sku="SF-STICK-004",
            name="ShopFlow Sticker Pack",
            description="Die-cut vinyl stickers.",
            price=Decimal("4.25"),
            currency="USD",
        ),
        quantity=100,
    ),
    SeedProduct(
        product=Product(
            product_id="p-1005",
            sku="SF-CAP-005",
            name="ShopFlow Cap",
            description="Six-panel cotton cap.",
            price=Decimal("22.00"),
            currency="USD",
        ),
        quantity=0,
    ),
)


def seed_inventory(session_factory: sessionmaker[Session], *, now: datetime | None = None) -> dict[str, int]:
    """Upsert the documented catalogue and return the resulting stock levels."""

    timestamp = now or datetime.now(UTC)
    with session_factory() as session, session.begin():
        for entry in SEED_PRODUCTS:
            product = entry.product
            product_insert = pg_insert(ProductRow).values(
                id=product.product_id,
                sku=product.sku,
                name=product.name,
                description=product.description,
                price=product.price,
                currency=product.currency,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.execute(
                product_insert.on_conflict_do_update(
                    index_elements=[ProductRow.id],
                    set_={
                        "sku": product_insert.excluded.sku,
                        "name": product_insert.excluded.name,
                        "description": product_insert.excluded.description,
                        "price": product_insert.excluded.price,
                        "currency": product_insert.excluded.currency,
                        "updated_at": timestamp,
                    },
                )
            )

            stock_insert = pg_insert(StockRow).values(
                product_id=product.product_id,
                quantity=entry.quantity,
                updated_at=timestamp,
            )
            session.execute(
                stock_insert.on_conflict_do_update(
                    index_elements=[StockRow.product_id],
                    set_={
                        # never raise an existing level above the documented one
                        "quantity": func.greatest(StockRow.quantity, stock_insert.excluded.quantity),
                        "updated_at": timestamp,
                    },
                )
            )

    return read_seeded_levels(session_factory)


def read_seeded_levels(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """Read back the stock level of every seeded product."""

    with session_factory() as session:
        rows = session.execute(
            select(ProductRow.id, StockRow.quantity)
            .join(StockRow, StockRow.product_id == ProductRow.id, isouter=True)
            .where(ProductRow.id.in_([entry.product.product_id for entry in SEED_PRODUCTS]))
            .order_by(ProductRow.id)
        ).all()
    return {str(row.id): int(row.quantity) if row.quantity is not None else 0 for row in rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Load the deterministic ShopFlow inventory seed (contracts.md §10).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (contracts.md §11); defaults to the environment.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print nothing on success.")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    settings = load_settings() if args.database_url is None else Settings(database_url=args.database_url)
    engine = create_engine_from_settings(settings)
    try:
        levels = seed_inventory(create_session_factory(engine))
    except SQLAlchemyError as exc:
        print(f"seed: FAILED - {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    if not args.quiet:
        print(f"seed: {len(SEED_PRODUCTS)} products ensured (idempotent, stock never exceeds the seeded levels)")
        for entry in SEED_PRODUCTS:
            product = entry.product
            print(
                f"   {product.product_id}  {product.sku}  {product.name:<24} "
                f"{product.price:>6}  availableQuantity={levels.get(product.product_id, 0):>4}  "
                f"(seeded {entry.quantity})"
            )
    return 0


def seeded_product_ids() -> Sequence[str]:
    """The documented product ids, in seed order."""

    return tuple(entry.product.product_id for entry in SEED_PRODUCTS)


if __name__ == "__main__":
    raise SystemExit(main())
