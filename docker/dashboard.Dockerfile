FROM node:22-alpine AS builder

# vite.config.ts imports from ../shared, so we need the dashboard tree
WORKDIR /repo/dashboard/ui

COPY dashboard/ui/package.json dashboard/ui/package-lock.json* ./
RUN npm ci --prefer-offline

# Copy shared assets (referenced by ../shared in vite config)
COPY dashboard/shared/ ../shared/
# Copy ui source
COPY dashboard/ui/ .

# outDir '../dist/webview' → /repo/dashboard/dist/webview
RUN npm run build

FROM nginx:alpine
COPY --from=builder /repo/dashboard/dist/webview /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
