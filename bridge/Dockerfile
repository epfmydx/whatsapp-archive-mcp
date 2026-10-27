FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go test ./... && CGO_ENABLED=0 go vet ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /bridge .
FROM alpine:3.23.3
RUN apk add --no-cache ca-certificates && addgroup -g 10001 bridge && adduser -D -H -u 10001 -G bridge bridge && mkdir /spool && chown bridge:bridge /spool
COPY --from=build /bridge /usr/local/bin/bridge
USER 10001:10001
ENV SPOOL_DIR=/spool
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/bridge"]
