# moomoo OpenD (the gateway moomoo's API talks to). moomoo only ships it for Intel/AMD chips, so on the
# Raspberry Pi the final image runs under emulation.
#
# Nothing is INSTALLED under emulation: the emulator crashes on Ubuntu's package tools (ldconfig), so the
# download, unpacking and config are all done in a native stage, and the Intel stage only copies files in.

# ---- stage 1: native (runs at full speed on the Pi) ----
FROM --platform=$BUILDPLATFORM python:3.12-slim AS fetch
ARG OPEND_URL
COPY fetch.py patch_xml.py /build/
RUN python /build/fetch.py "$OPEND_URL" /out

# ---- stage 2: Intel image, files only ----
FROM ubuntu:22.04
COPY --from=fetch /out /opt/opend
COPY --from=fetch /etc/ssl/certs /etc/ssl/certs
COPY --chmod=755 entrypoint.sh send.sh login_helper.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
