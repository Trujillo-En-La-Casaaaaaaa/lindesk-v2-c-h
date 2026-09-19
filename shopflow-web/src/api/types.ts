/**
 * Wire types for the ShopFlow gateway API.
 *
 * These mirror `contracts.md` (§2 public gateway API, §3 order view, §1 error
 * envelope) exactly. They describe payload shapes only - no business rules and
 * no persistence concerns live in this module.
 */

/** Order lifecycle states reported by the orders service (contracts.md §3, §9). */
export type OrderStatus = 'CREATED' | 'CONFIRMED' | 'FAILED' | 'SHIPPED' | 'CANCELLED';

/** A single order line as returned by the gateway (contracts.md §3). */
export interface OrderLineView {
  productId: string;
  sku: string;
  name: string;
  quantity: number;
  unitPrice: number;
  lineTotal: number;
}

/** Order view returned by every order endpoint (contracts.md §3). */
export interface OrderView {
  orderId: string;
  status: OrderStatus;
  customerId: string;
  createdAt: string;
  updatedAt: string;
  confirmedAt: string | null;
  shippedAt: string | null;
  cancelledAt: string | null;
  cancellationReason: string | null;
  total: number;
  lines: OrderLineView[];
}

/** Product view returned by the gateway product routes (contracts.md §2, §4). */
export interface ProductView {
  productId: string;
  sku: string;
  name: string;
  description: string;
  price: number;
  currency: string;
  availableQuantity: number;
  inStock: boolean;
}

/** Lists are returned as `{ items, total }` (contracts.md §1). */
export interface ListResponse<T> {
  items: T[];
  total: number;
}

export type ProductListResponse = ListResponse<ProductView>;
export type OrderListResponse = ListResponse<OrderView>;

/** Body of `POST /api/orders` (contracts.md §2). */
export interface CreateOrderLineInput {
  productId: string;
  quantity: number;
}

export interface CreateOrderRequest {
  customerId: string;
  lines: CreateOrderLineInput[];
}

/** Body of `POST /api/orders/{orderId}/cancel` (contracts.md §2). */
export interface CancelOrderRequest {
  reason: string;
}

/** Error envelope shared by every service and the gateway (contracts.md §1). */
export interface ErrorBody {
  code: string;
  message: string;
  correlationId: string;
  details?: unknown;
}

export interface ErrorEnvelope {
  error: ErrorBody;
}

export interface ListProductsParams {
  limit?: number;
  offset?: number;
}

export interface ListOrdersParams {
  customerId?: string;
  limit?: number;
  offset?: number;
}
