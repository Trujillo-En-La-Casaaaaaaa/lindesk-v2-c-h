"""PostgreSQL adapter for the catalog port: ``postgres-catalog-repository``.

Read-only: this repository owns the catalog rows, but reads never write.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ProductRow
from app.domain.product import Product


class PostgresCatalogRepository:
    """Implements :class:`app.ports.repositories.CatalogRepository`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_products(self, *, limit: int | None, offset: int) -> tuple[tuple[Product, ...], int]:
        with self._session_factory() as session:
            total = int(session.execute(select(func.count()).select_from(ProductRow)).scalar_one())
            statement = self._page(select(ProductRow).order_by(ProductRow.id), limit=limit, offset=offset)
            rows = session.execute(statement).scalars().all()
            products = tuple(_to_domain(row) for row in rows)
        return products, total

    def get_product(self, product_id: str) -> Product | None:
        with self._session_factory() as session:
            row = session.execute(select(ProductRow).where(ProductRow.id == product_id)).scalars().first()
            return _to_domain(row) if row is not None else None

    @staticmethod
    def _page(statement: Select, *, limit: int | None, offset: int) -> Select:
        if limit is not None:
            statement = statement.limit(limit)
        if offset:
            statement = statement.offset(offset)
        return statement


def _to_domain(row: ProductRow) -> Product:
    return Product(
        product_id=row.id,
        sku=row.sku,
        name=row.name,
        description=row.description or "",
        price=Decimal(row.price),
        currency=row.currency or "USD",
    )
