# ShopFlow — creación · complejidad alta (C-H)

Paquete de entrevista (artefacto congelado). Contiene el árbol de ShopFlow del escenario **C-H**. **No es LinDesk**; es el software de dominio que se evalúa.

## Qué es ShopFlow

ShopFlow es una aplicación pequeña de **comercio electrónico / gestión de pedidos**. Es el dominio compartido de los nueve escenarios (no es LinDesk). El comportamiento de negocio previsto es:

1. Catálogo de productos.
2. Inventario (stock).
3. Creación de pedidos.
4. Validar inventario **antes** de aceptar un pedido.
5. Decrementar inventario **después** de un pedido exitoso.
6. Consulta de detalle / estado del pedido.
7. Acción administrativa para marcar un pedido como `SHIPPED`.
8. Notificación de confirmación del pedido.

Según el escenario, se pide además **cancelación de pedido por el cliente** (creación y evolución) o se prohíbe expresamente (migración).

Reglas de cancelación cuando sí aplica:

- el pedido no debe estar `SHIPPED`;
- motivo no vacío y de como máximo 200 caracteres;
- pasar a `CANCELLED`;
- guardar `cancelledAt` y `cancellationReason`;
- restaurar inventario **exactamente una vez**;
- enviar o registrar una notificación de cancelación;
- rechazar cancelar un pedido ya enviado;
- reintentos repetidos no deben restaurar stock dos veces.


## Este escenario (C-H)

| | |
|---|---|
| Código | `C-H` |
| Ciclo de vida | Creación (greenfield) |
| Complejidad arquitectónica | Alta — microservicios, seis repositorios |
| Identificador de corrida | `20260918-121608` (solo trazabilidad) |

No hay repositorio AS-IS: es **creación / greenfield** (se parte de cero).


### Arquitectura pedida

Exactamente seis repositorios. `shopflow-web` habla **solo** a través de `shopflow-gateway`. `shopflow-orders` es dueño del ciclo de vida y persistencia de pedidos. `shopflow-inventory` es dueño del stock. `shopflow-notifications` orquesta notificaciones. Los servicios no comparten escritura de base de datos. `shopflow-infra` aporta Compose y un emulador determinista del proveedor externo de notificaciones.

### Encargo (resumen)

Crear el ecosistema ShopFlow **desde cero** como microservicios, incluyendo **cancelación** respetando la propiedad de cada servicio. Semilla, Docker, lockfiles, pruebas. No crear repositorios extra ni features no pedidas.

El texto **exacto** del encargo está en [`TASK.md`](./TASK.md). Úselo como contrato.

### Carpetas de este árbol

- `shopflow-web/` — frontend React.
- `shopflow-gateway/` — entrada pública; no debe poseer lógica de pedidos/inventario.
- `shopflow-orders/` — pedidos.
- `shopflow-inventory/` — inventario.
- `shopflow-notifications/` — notificaciones.
- `shopflow-infra/` — Compose, datos y emulador.

## Cómo usarlo en la entrevista

1. Lea primero `TASK.md` (el encargo congelado; está en inglés porque así se le dio al sistema).
2. Recorra los directorios de producto listados arriba. Este paquete es **solo código fuente** (sin `node_modules`, builds ni informes de análisis).
3. Juzgue el código frente al encargo: requisitos funcionales **y** restricciones arquitectónicas. Un sistema que “parece funcionar” pero ignora los límites del escenario no cumple el contrato.
4. No trate este README como veredicto de calidad: es contexto. La puntuación es del experto sobre el código.
5. Etapa B (`AGENTS.md`, grafo C4) **no** está en este repositorio. LinDesk en ejecución se muestra por RDP, aparte de este árbol de GitHub.

