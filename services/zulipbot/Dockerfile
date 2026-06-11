FROM oven/bun:1.3.13-slim

WORKDIR /repo
ENV NODE_ENV=production

RUN bun install -g pnpm@10.28.1

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY services/zulipbot/package.json services/zulipbot/package.json
RUN pnpm install --filter zulipbot --prod --frozen-lockfile

COPY services/zulipbot/ services/zulipbot/
WORKDIR /repo/services/zulipbot

EXPOSE 3002
ENV PORT=3002
CMD ["bun", "src/index.ts"]
