"""Single-process WSGI entry point behind an HTTPS ingress with a fixed public origin."""
import json
import logging
import os
from email.message import Message
from http import HTTPStatus

from scheduler import MAX_BODY, require
from server import Application, Jobs


# ponytail: one process keeps session and solver limits authoritative; do not add replicas.
WAITRESS_OPTIONS = dict(threads=4, connection_limit=16, backlog=16,
                       # Waitress rejects >= this limit; admit exactly MAX_BODY bytes.
                       max_request_header_size=16 * 1024, max_request_body_size=MAX_BODY + 1,
                       inbuf_overflow=2 * MAX_BODY, outbuf_overflow=2 * MAX_BODY,
                       channel_timeout=5, cleanup_interval=1, channel_request_lookahead=0,
                       expose_tracebacks=False, log_socket_errors=False,
                       clear_untrusted_proxy_headers=True, trusted_proxy=None,
                       ident="Scheduler")


class HostedApplication:
    def __init__(self, origin, jobs=None):
        self.jobs = jobs or Jobs()
        self.core = Application(self.jobs, origin, hosted=True)

    def __call__(self, environ, start_response):
        headers = Message()
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers[key[5:].replace("_", "-")] = value
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if key in environ and environ[key]:
                headers[key.replace("_", "-")] = environ[key]
        path = environ.get("PATH_INFO", "")
        if environ.get("QUERY_STRING"):
            path += "?" + environ["QUERY_STRING"]
        # Waitress buffers and caps the body before WSGI dispatch. Its inactive-channel
        # timeout is NOT an absolute header/body deadline; ingress must enforce that.
        status, response_headers, raw = self.core.handle(
            environ.get("REQUEST_METHOD", ""), path, headers, environ["wsgi.input"].read)
        start_response(f"{status} {HTTPStatus(status).phrase}", response_headers)
        return [raw]


class SafeServerLog(logging.Handler):
    def emit(self, record):
        # Unexpected server exceptions may contain attacker input; retain only severity.
        print(json.dumps(dict(event="http_server", level=record.levelname)), flush=True)


def main():
    from waitress import serve

    origin = os.environ.get("PUBLIC_ORIGIN", "")
    port = os.environ.get("PORT", "8080")
    require(port.isascii() and port.isdigit() and 1 <= int(port) <= 65535, "port", "configuration")
    app = HostedApplication(origin)
    logger = logging.getLogger("waitress")
    logger.handlers = [SafeServerLog()]
    logger.propagate = False
    try:
        print(json.dumps(dict(event="listening", mode="hosted", origin=origin)), flush=True)
        serve(app, host="127.0.0.1", port=int(port), **WAITRESS_OPTIONS)
    finally:
        app.jobs.close()


if __name__ == "__main__":
    main()
