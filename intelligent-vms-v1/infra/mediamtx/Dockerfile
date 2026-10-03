FROM bluenviron/mediamtx:1.21.1 AS mediamtx

FROM alpine:3.22
RUN apk add --no-cache ca-certificates curl tzdata
COPY --from=mediamtx /mediamtx /mediamtx
ENTRYPOINT ["/mediamtx"]
CMD ["/mediamtx.yml"]
