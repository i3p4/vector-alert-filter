FROM python:3.13-slim

ARG WORK_UID="1000"
ARG WORK_GID="1000"
ARG WORK_USER="afilter"
ARG WORK_GROUP="afilter"
ARG WORK_HOME="/afilter"
ARG WORK_SHELL="/bin/bash"

ENV HOME="/afilter"

LABEL org.i3p4.title="i3p4/vector-alert-filter" \
    org.i3p4.version="1.2"

RUN groupadd -g "${WORK_GID}" "${WORK_GROUP}" \
    && useradd -m -d "${WORK_HOME}" -s "${WORK_SHELL}" -u "${WORK_UID}" -g "${WORK_GID}" "${WORK_USER}"

COPY requirements.txt ${WORK_HOME}/requirements.txt

RUN pip install -U pip --no-cache \
    && pip install --no-cache -r ${WORK_HOME}/requirements.txt

COPY app/ ${WORK_HOME}/app/
COPY data/ ${WORK_HOME}/data/
COPY main.py ${WORK_HOME}/main.py

RUN chmod ugo+x ${WORK_HOME}/main.py \
    && chown -R "${WORK_USER}":"${WORK_GROUP}" "${WORK_HOME}"

WORKDIR "${WORK_HOME}"
USER "${WORK_USER}":"${WORK_GROUP}"

ENTRYPOINT ["/afilter/main.py"]
#ENTRYPOINT ["tail", "-f", "/dev/null"]
