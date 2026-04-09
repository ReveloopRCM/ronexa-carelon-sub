FROM node:18-alpine AS builder

WORKDIR /app

# Install dependencies
COPY package.json package-lock.json* ./
RUN npm ci

# Build
COPY . .

# API URLs injected at build time
# NEXT_PUBLIC_API_URL: used by client-side fetch in api.ts
# API_URL: used by next.config.js rewrites (server-side proxy)
ARG NEXT_PUBLIC_API_URL=""
ARG API_URL=https://backend-api.internal.purplestone-5b70379e.centralus.azurecontainerapps.io
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV API_URL=$API_URL

RUN npm run build

# Production image
FROM node:18-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production

# Copy built assets
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000

CMD ["node", "server.js"]
