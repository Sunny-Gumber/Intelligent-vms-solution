FROM nginx:1.30.6-alpine3.24-slim@sha256:dede8895648b461178c3bc604f7eed4423efe5220b2c4d839f5eb3d34ac3f8f7
RUN apk upgrade --no-cache
RUN apk add --no-cache curl
RUN sed -i 's#^[[:space:]]*pid[[:space:]].*;#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf \
    && grep -q '^pid /tmp/nginx.pid;' /etc/nginx/nginx.conf
COPY web /usr/share/nginx/html
COPY services/web/default.conf.template /etc/nginx/templates/default.conf.template
RUN chown -R 101:101 /usr/share/nginx/html /etc/nginx/templates /etc/nginx/conf.d /var/cache/nginx /var/run
ENV CONTROL_API_UPSTREAM=http://control-api:8000
USER 101:101
