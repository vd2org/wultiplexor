FROM python:3.12-slim as build

RUN pip install pdm

RUN mkdir -p /opt/build

COPY ./src /opt/build/src
COPY pyproject.toml /opt/build/
COPY pdm.lock /opt/build/
COPY LICENSE /opt/build/
COPY README.md /opt/build/

WORKDIR /opt/build

RUN pdm build

FROM python:3.12-slim

COPY --from=build /opt/build/dist/*.whl /opt/
RUN pip install /opt/*.whl

RUN groupadd -r -g 999 wultiplexor
RUN useradd -r -u 999 -s /usr/sbin/nologin -g wultiplexor wultiplexor
USER wultiplexor
