FROM golang:1.26.8-alpine AS mediamtx
RUN apk add --no-cache python3 git
WORKDIR /vms
COPY infra/mediamtx/runtime.json infra/mediamtx/runtime.json
COPY tools/build_mediamtx.py tools/build_mediamtx.py
RUN python3 tools/build_mediamtx.py --work /source --output /mediamtx --target-os linux

FROM alpine:3.22
RUN apk add --no-cache ca-certificates curl tzdata
COPY --from=mediamtx /mediamtx /mediamtx
ENTRYPOINT ["/mediamtx"]
CMD ["/mediamtx.yml"]
