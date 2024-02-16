# Wultiplexor


## Deploy server using docker compose

```shell
docker network create wultiplexor
DOMAIN=wultiplexor.example.com ACME_EMAIL=acme@example.com SECRET=sEcReTkEy docker compose -p wultiplexor -f compose.yaml up --build -d
```
