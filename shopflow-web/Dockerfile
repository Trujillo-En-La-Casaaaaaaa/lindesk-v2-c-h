# ShopFlow Web - build the SPA, then serve the static bundle.
#
# The gateway base URL is inlined at build time (Vite), so the stack passes it as
# a build argument; the default matches the local gateway (contracts.md §11).

FROM node:22-alpine AS build

WORKDIR /usr/src/app

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

ARG VITE_GATEWAY_BASE_URL=http://localhost:8080
ENV VITE_GATEWAY_BASE_URL=${VITE_GATEWAY_BASE_URL}

RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /usr/src/app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
