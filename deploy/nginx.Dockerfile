FROM node:22-alpine AS frontend-build
ARG APP_RELEASE_SHA
RUN test "${#APP_RELEASE_SHA}" -eq 40 && printf '%s' "$APP_RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$'
ENV VITE_APP_RELEASE_SHA=${APP_RELEASE_SHA}
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
ARG APP_RELEASE_SHA
ENV APP_RELEASE_SHA=${APP_RELEASE_SHA}
LABEL org.opencontainers.image.revision=${APP_RELEASE_SHA}
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend-build /build/dist /usr/share/nginx/html
EXPOSE 8080
