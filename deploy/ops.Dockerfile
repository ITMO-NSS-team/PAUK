FROM node:24-bookworm-slim AS frontend
WORKDIR /web
COPY pauk/gui/web/package*.json ./
RUN npm ci
COPY pauk/gui/web/ ./
RUN node --input-type=module -e 'import {build} from "vite"; await build({publicDir:false,base:"./",build:{outDir:"/ops-web",rolldownOptions:{input:"ops.html"}}});'

FROM docker:28-cli AS docker-cli

FROM paukbase
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=frontend /ops-web /app/ops-web
CMD ["uvicorn", "pauk.ops.app:build", "--factory", "--host", "0.0.0.0", "--port", "8506"]
