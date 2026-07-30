# syntax=docker/dockerfile:1.7

ARG OPENMAIC_REPOSITORY=https://github.com/xinlingzhifei/OpenMAIC.git
ARG OPENMAIC_COMMIT=0cf2a330411681190e89f48e20f305345ff99f87
ARG OPENMAIC_COMMIT_SHORT=0cf2a3304116
ARG OPENMAIC_APP_VERSION=0.3.1

FROM alpine/git:2.47.2 AS upstream

ARG OPENMAIC_REPOSITORY
ARG OPENMAIC_COMMIT

RUN git clone --filter=blob:none --no-checkout "$OPENMAIC_REPOSITORY" /src

WORKDIR /src

RUN git fetch --depth=1 origin "$OPENMAIC_COMMIT" && \
    git checkout --detach "$OPENMAIC_COMMIT" && \
    test "$(git rev-parse HEAD)" = "$OPENMAIC_COMMIT"

FROM node:22-alpine AS source

ARG OPENMAIC_APP_VERSION

RUN apk add --no-cache libc6-compat && \
    corepack enable && \
    corepack prepare pnpm@10.28.0 --activate

WORKDIR /app

COPY --from=upstream /src/ ./

RUN test "$(node -p "require('./package.json').version")" = "$OPENMAIC_APP_VERSION"

COPY integrations/openmaic/overlay/ /overlay/
RUN cp -R /overlay/. /app/

FROM source AS deps

RUN apk add --no-cache python3 build-base g++ cairo-dev pango-dev jpeg-dev giflib-dev librsvg-dev
RUN pnpm install --frozen-lockfile

FROM source AS builder

ARG NEXT_PUBLIC_PERSISTENCE
ARG NEXT_PUBLIC_PERSISTENCE_TOKEN
ARG NEXT_PUBLIC_MAIC_EDITOR_ENABLED
ARG NEXT_PUBLIC_PI_CHAT_ENABLED
ARG NEXT_PUBLIC_SHOW_VOCATIONAL_TEST_UI
ARG NEXT_PUBLIC_ENABLE_VIDEO_EXPORT
ARG NEXT_PUBLIC_ENABLE_PPTX_IMPORT

ENV NEXT_PUBLIC_PERSISTENCE=$NEXT_PUBLIC_PERSISTENCE
ENV NEXT_PUBLIC_PERSISTENCE_TOKEN=$NEXT_PUBLIC_PERSISTENCE_TOKEN
ENV NEXT_PUBLIC_MAIC_EDITOR_ENABLED=$NEXT_PUBLIC_MAIC_EDITOR_ENABLED
ENV NEXT_PUBLIC_PI_CHAT_ENABLED=$NEXT_PUBLIC_PI_CHAT_ENABLED
ENV NEXT_PUBLIC_SHOW_VOCATIONAL_TEST_UI=$NEXT_PUBLIC_SHOW_VOCATIONAL_TEST_UI
ENV NEXT_PUBLIC_ENABLE_VIDEO_EXPORT=$NEXT_PUBLIC_ENABLE_VIDEO_EXPORT
ENV NEXT_PUBLIC_ENABLE_PPTX_IMPORT=$NEXT_PUBLIC_ENABLE_PPTX_IMPORT

COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/packages ./packages
COPY --from=deps /app/public/vendor ./public/vendor

RUN pnpm build

FROM node:22-alpine AS runner

ARG OPENMAIC_COMMIT
ARG OPENMAIC_COMMIT_SHORT
ARG OPENMAIC_APP_VERSION

LABEL org.opencontainers.image.source="https://github.com/xinlingzhifei/OpenMAIC" \
      org.opencontainers.image.revision="$OPENMAIC_COMMIT" \
      org.opencontainers.image.revision-short="$OPENMAIC_COMMIT_SHORT" \
      org.opencontainers.image.version="$OPENMAIC_APP_VERSION"

WORKDIR /app

ENV NODE_ENV=production
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

RUN apk add --no-cache libc6-compat cairo pango jpeg giflib librsvg
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]
