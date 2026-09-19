# ShopFlow Web

Browser application for ShopFlow. It browses the product catalog, shows a product,
creates an order, shows order detail/status, cancels an unshipped order with a
reason, and performs the administrative mark-as-SHIPPED action.

The application talks to **one** API: the ShopFlow gateway (`contracts.md` §2).
It never calls `shopflow-orders` or `shopflow-inventory`, holds no business rules
of its own, and ships no external assets (no CDN, no web fonts, no analytics).

## Stack

- React 19 + TypeScript, built with Vite.
- Client side routing with `react-router-dom` (SPA, static hosting).
- Vitest + Testing Library + jsdom for the test suite.
- Plain CSS only (`src/styles.css`), no runtime configuration besides the gateway URL.

## Repository layout

```
README.md                 this file
Dockerfile                builds the SPA and serves the static bundle with nginx
nginx.conf                static host config: SPA fallback, cached assets
.env.example              VITE_GATEWAY_BASE_URL only
package.json              scripts and dependencies
package-lock.json         committed lockfile (install with npm ci)
vite.config.ts            Vite + Vitest configuration
tsconfig.json             strict TypeScript configuration
index.html                application entry document
src/main.tsx              mounts the app
src/App.tsx               app shell: layout, navigation, routes
src/api/client.ts         the only module that performs HTTP
src/api/types.ts          OrderView, ProductView, ErrorEnvelope and friends
src/api/errors.ts         error codes -> user messages, correlation ids, ApiError
src/hooks/useAsyncResource.ts  loading / error / reload / replace for a gateway read
src/lib/format.ts         money and timestamp display helpers
src/components/ErrorBanner.tsx shared error surface (code, message, correlation id)
src/features/catalog/ProductList.tsx     catalog screen
src/features/catalog/ProductDetail.tsx   product detail screen
src/features/orders/CreateOrder.tsx      order creation form
src/features/orders/OrderStatus.tsx      order detail and status screen
src/features/orders/CancelOrderDialog.tsx cancellation dialog (reason rules)
src/features/admin/AdminOrders.tsx       admin order list + ship action
src/test/setup.ts         test environment setup
src/test/testUtils.tsx    seeded fixtures and a recording fetch double
src/**/*.test.ts(x)       unit + component tests
```

## Configuration

Only one value is configured (no secrets):

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_GATEWAY_BASE_URL` | `http://localhost:8080` | Base URL of the ShopFlow gateway |

Copy `.env.example` to `.env` for local development. The value is inlined at
build time, so the Docker build accepts it as a build argument as well.

## Getting started

```bash
npm ci
cp .env.example .env       # then adjust VITE_GATEWAY_BASE_URL if needed
npm run dev                # http://localhost:5173
```

| Script | What it does |
| --- | --- |
| `npm run dev` | Vite dev server for the SPA |
| `npm run build` | `tsc --noEmit` and a production build into `dist/` |
| `npm run preview` | serves the built bundle locally |
| `npm test` | Vitest in watch mode |
| `npm test -- --run` | one-shot test run (used by CI) |
| `npm run typecheck` | strict type check only |

## Routes

| Route | Screen | Gateway call |
| --- | --- | --- |
| `/` | Catalog | `GET /api/products` |
| `/products/:productId` | Product detail | `GET /api/products/{productId}` |
| `/products/:productId/order` | Create order | `GET /api/products/{productId}`, `POST /api/orders` |
| `/orders/:orderId` | Order status | `GET /api/orders/{orderId}`, `POST /api/orders/{orderId}/cancel` |
| `/admin/orders` | Admin orders | `GET /api/orders`, `POST /api/orders/{orderId}/ship` |
| any other path | Not found | - |

## Gateway usage

`src/api/client.ts` is the only module that performs HTTP. It:

- reads `VITE_GATEWAY_BASE_URL` (trailing slashes trimmed, local default applied),
- sends `X-Correlation-Id` on every request and keeps the id returned in the error envelope,
- sends `Content-Type: application/json; charset=utf-8` for bodies,
- adds `X-ShopFlow-Role: admin` on `POST /api/orders/{orderId}/ship`,
- parses `{ items, total }` lists and the `{ error: { code, message, correlationId, details } }` envelope,
- throws a typed `ApiError` carrying `code`, `message`, `correlationId`, `details`, `retryable` and the HTTP status.

The typed functions are `listProducts`, `getProduct`, `createOrder`, `getOrder`,
`listOrders`, `cancelOrder` and `shipOrder`. No other endpoint is used.

## Behaviour and failure semantics

- **Optimistic-free.** Screens render the last gateway response and never invent
  state. A successful cancellation renders the order returned by
  `POST /cancel`; a failed one reloads from `GET /orders/{id}` before changing
  what is displayed.
- **Branch on `code`, not on the HTTP status.** `order_shipped`,
  `order_not_cancellable`, `invalid_reason`, `insufficient_stock`,
  `validation_error` and `invalid_status_transition` each have their own message.
- **Retryable failures.** `503` responses and an unreachable gateway
  (`network_unavailable`, raised with the correlation id of the failed request)
  render the banner with a `Retry` button and a statement that nothing was
  confirmed. A partially succeeded action is never assumed.
- **Details are used where they add information.** `409 insufficient_stock`
  messages are built from the error `details` (requested vs. available) and
  `400 validation_error` messages are mapped to the matching form field when the
  inner shape exposes one.
- **No business rules in the UI.** Stock limits, cancellation eligibility and
  status transitions are decided by the services. The UI only reflects them and
  avoids obviously invalid actions (out-of-stock products cannot start an order,
  `SHIPPED`/`CANCELLED`/`FAILED` orders are not offered a cancel action, only
  `CONFIRMED` orders are offered the ship action).
- **Timestamps** are shown exactly as the gateway returned them (ISO-8601 UTC) so
  a cancellation or shipment can be traced.
- **CORS.** The browser calls the gateway directly, so the gateway (or the proxy in
  front of it) must allow the web origin for `GET`/`POST` with the
  `X-Correlation-Id`, `X-ShopFlow-Role` and `Content-Type` headers. That is
  gateway configuration, deliberately not duplicated here.

## Loading, empty and error states

Every screen handles all three:

| Screen | Loading | Empty | Error |
| --- | --- | --- | --- |
| Catalog | "Loading products…" | "No products are available yet." | banner + retry |
| Product detail | "Loading product…" | not-found notice for `404 not_found` | banner + retry |
| Create order | "Loading product…" | out-of-stock notice, submit disabled | banner (+ retry when retryable), field errors |
| Order status | "Loading order…" | "This order has no lines." | banner + retry |
| Admin orders | "Loading orders…" | "No orders have been created yet." | banner + retry, per-order ship errors |
| Cancel dialog | submitting state on the button | client-side reason validation | gateway error banner inside the dialog |

Keyboard and assistive technology support: skip link, landmarks (`header`,
`nav`, `main`, `footer`), one `h1` per screen, labelled inputs and buttons, table
captions and scoped headers, `role="alert"` for failures, `role="status"` for
loading and announcements, `aria-invalid`/`aria-describedby` on invalid fields,
visible focus outlines, `Escape` closes the cancellation dialog and focus moves
into it when it opens.

## Cancellation reason rules

Enforced before any request is sent (mirroring the server rule):

- the reason must be non-empty after trimming,
- the reason must be at most 200 characters (`MAX_CANCELLATION_REASON_LENGTH`),
- a live character counter is shown; both failures block the request and explain
  themselves next to the field.

## Testing

```bash
npm ci
npm run build
npm test -- --run
```

The suite covers the gateway client (URLs, headers, correlation id, envelope
parsing, retryability), the error mapping, the catalog (five seeded products,
`p-1005` unavailable with a disabled action, empty/error states), product detail,
order creation (validation, `201` navigation, `409 insufficient_stock` details,
`400 validation_error` field feedback, `503` retryable), the cancellation dialog
(empty, whitespace-only and 201-character reasons refused without a request,
valid reason posted, `invalid_reason`/`order_shipped`/`order_not_cancellable`
mirrored), the order status screen (status, line items, total, `shippedAt`,
`cancelledAt`, `cancellationReason`, the `SHIPPED` guard and the race where the
order ships while the dialog is open) and the admin screen (ship only for
`CONFIRMED`, admin role header, `409 invalid_status_transition`).

Two guard tests read the real source tree: only `src/api/client.ts` may perform
HTTP, only `VITE_GATEWAY_BASE_URL` may be read from the environment, and no
service base URL other than the gateway appears anywhere in `src/`.

## Docker

```bash
docker build -t shopflow-web --build-arg VITE_GATEWAY_BASE_URL=http://localhost:8080 .
docker run --rm -p 3000:80 shopflow-web
```

The image builds the SPA with `npm ci` + `npm run build` and serves `dist/`
statically with nginx (SPA fallback to `index.html`, immutable caching for
`/assets`, `Cache-Control: no-cache` for the document, container health check on
`/`). `shopflow-infra` builds and serves this image in the local stack.

## Out of scope

Accounts and login, cart persistence, payments, wishlists, search filters,
reviews, recommendations, internationalisation, and any direct call to a
backend service (including any proxy configuration in front of the gateway).
