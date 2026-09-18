"""Bounded scheduling core and loopback HTTP adapter; hosted WSGI lives in hosted.py."""
import importlib
import json
import multiprocessing
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scheduler import (LIMITS, MAX_BODY, ValidationError, baseline, candidate, check_assignments,
                       check_tuple, fields, make_tuple, parse_json, require, solve_child, validate_scenario)

ROOT = Path(__file__).resolve().parent
PORT = 54186
ASSETS = {"/": ("static/index.html", "text/html"),
          "/app.js": ("static/app.js", "text/javascript"),
          "/style.css": ("static/style.css", "text/css"),
          "/logo.svg": ("static/logo.svg", "image/svg+xml"),
          "/inter-latin-var.woff2": ("static/inter-latin-var.woff2", "font/woff2"),
          "/newsreader-latin-var.woff2": ("static/newsreader-latin-var.woff2", "font/woff2")}
EXAMPLES = [("calendar", "Calendar and route"), ("comparison", "Order sequence comparison"),
            ("deadlines", "Hard deadlines")]
for name, _ in EXAMPLES:
    ASSETS[f"/examples/{name}.json"] = (f"examples/{name}.json", "application/json")


class Jobs:
    def __init__(self, budget=10, grace=3, startup=30):
        for name, value, cap in (("startup", startup, 30), ("budget", budget, 10), ("grace", grace, 3)):
            require(type(value) in (int, float) and 0 <= value <= cap and (name == "grace" or value > 0),
                    name, "configuration")
        self.lock = threading.RLock()
        self.jobs, self.sessions, self.cookies = {}, {}, {}
        self.ai_lock = threading.Lock()
        self.budget, self.grace, self.startup = budget, grace, startup
        self.context = multiprocessing.get_context("spawn")
        self.closed = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True)
        self.reaper.start()

    def _reap(self):
        while not self.closed.wait(2):
            with self.lock:
                now = time.monotonic()
                for key, job in list(self.jobs.items()):
                    if now - job["created"] > 60:
                        self.cancel(key, job["tuple"]["session_id"])
                for key, session in list(self.sessions.items()):
                    if now - session["seen"] > 1800:
                        self.retire(key)

    def new_session(self):
        with self.lock:
            require(len(self.sessions) < 32, "session", "busy", 409)
            key = str(uuid.uuid4())
            cookie = secrets.token_urlsafe(32)
            self.sessions[key] = dict(head=None, seen=time.monotonic(), cookie=cookie, csrf=secrets.token_urlsafe(32))
            self.cookies[cookie] = key
            return key

    def session(self, key):
        require(type(key) is str and key in self.sessions, "session_id", "session", 403)
        if time.monotonic() - self.sessions[key]["seen"] > 1800:
            self.retire(key)
            raise ValidationError("session_id", "session", 403)
        self.sessions[key]["seen"] = time.monotonic()
        return self.sessions[key]

    def register(self, input_tuple):
        with self.lock:
            session = self.session(input_tuple["session_id"])
            previous = session["head"]
            require(previous is None or input_tuple == previous or input_tuple["revision"] > previous["revision"],
                    "tuple", "stale", 409)
            session["head"] = input_tuple

    def current(self, input_tuple):
        require(self.session(input_tuple["session_id"])["head"] == input_tuple, "tuple", "stale", 409)

    def start(self, scenario, input_tuple):
        with self.lock:
            self.current(input_tuple)
            require(len(self.jobs) < 16 and not any(j["state"] == "pending" for j in self.jobs.values()),
                    "solve", "busy", 409)
            job_id = str(uuid.uuid4())
            receive, send = self.context.Pipe(duplex=False)
            process = self.context.Process(target=solve_child, args=(scenario, send, self.budget), daemon=True)
            job = dict(tuple=input_tuple, state="pending", baseline=None, candidate=None,
                       created=time.monotonic(), process=process, receive=receive, cancelled=False,
                       last_incumbent=None, input=scenario)
            self.jobs[job_id] = job
            try:
                process.start()
            except Exception:
                del self.jobs[job_id]
                receive.close(); send.close()
                raise ValidationError("solve", "solver_unavailable", 503) from None
            send.close()
            threading.Thread(target=self._watch, args=(job_id, job), daemon=True).start()
            return job_id

    def _watch(self, job_id, job):
        process, connection, scenario = job["process"], job["receive"], job["input"]
        final = None
        ready = False
        deadline = job["created"] + self.startup
        absolute_deadline = deadline + self.budget + self.grace
        def expired(now=None):
            nonlocal final
            if (time.monotonic() if now is None else now) < deadline:
                return False
            incumbent = job["last_incumbent"] if ready else None
            final = dict(status="feasible" if incumbent else "timeout_no_solution",
                         reason="watchdog_timeout" if ready else "solver_startup_timeout",
                         assignments=incumbent, engine=None)
            return True
        try:
            base = baseline(scenario)
            while not job["cancelled"]:
                if expired():
                    break
                available = connection.poll(min(0.03, max(0, deadline - time.monotonic())))
                if job["cancelled"] or expired():
                    break
                if available:
                    try:
                        raw = connection.recv_bytes(65536)
                    except EOFError:
                        break
                    if job["cancelled"] or expired():
                        break
                    try:
                        message = parse_json(raw)
                    except ValidationError:
                        raise ValidationError("solver_message", "solver_protocol") from None
                    require(type(message) is dict and set(message) == {"kind", "value"},
                            "solver_message", "solver_protocol")
                    if message["kind"] == "ready":
                        require(not ready and message["value"] is None, "solver_message", "solver_protocol")
                        now = time.monotonic()
                        if expired(now):
                            break
                        ready = True
                        deadline = min(absolute_deadline, now + self.budget + self.grace)
                    elif message["kind"] == "incumbent":
                        require(ready, "solver_message", "solver_protocol")
                        check_assignments(scenario, message["value"])
                        if job["cancelled"] or expired():
                            break
                        job["last_incumbent"] = message["value"]
                    elif message["kind"] == "final":
                        value = message["value"]
                        require(type(value) is dict and set(value) == {"status", "reason", "assignments", "engine"}
                                and value["status"] in ("optimal", "feasible", "infeasible", "timeout_no_solution", "error"),
                                "solver_message", "solver_protocol")
                        require(ready or value == dict(status="error", reason="solver_failure", assignments=None, engine=None),
                                "solver_message", "solver_protocol")
                        if expired():
                            break
                        final = value
                        break
                    else:
                        raise ValidationError("solver_message", "solver_protocol")
                if not process.is_alive() and not connection.poll():
                    break
            if final is None:
                final = dict(status="error", reason="solver_exit", assignments=None, engine=None)
            result = candidate(scenario, job["tuple"], **final)
            if expired():
                result = candidate(scenario, job["tuple"], **final)
        except ValidationError as error:
            base = None
            result = candidate(scenario, job["tuple"], "error",
                               "solver_protocol" if error.code == "solver_protocol" else "invalid_assignment")
        except Exception:
            base = None
            result = candidate(scenario, job["tuple"], "error", "invalid_assignment")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill(); process.join(timeout=1)
            connection.close()
        with self.lock:
            if not job["cancelled"] and self.jobs.get(job_id) is job:
                job.update(state="complete", baseline=base, candidate=result,
                           elapsed_ms=round((time.monotonic() - job["created"]) * 1000))
                job.pop("input", None)
                job.pop("last_incumbent", None)

    def get(self, job_id, session_id):
        with self.lock:
            self.session(session_id)
            job = self.jobs.get(job_id)
            require(job is not None and job["tuple"]["session_id"] == session_id, "job_id", "not_found", 404)
            return dict(job_id=job_id, tuple=job["tuple"], state=job["state"],
                        baseline=job["baseline"], candidate=job["candidate"],
                        elapsed_ms=job.get("elapsed_ms", round((time.monotonic() - job["created"]) * 1000)))

    def cancel(self, job_id, session_id):
        with self.lock:
            job = self.jobs.get(job_id)
            require(job is not None and job["tuple"]["session_id"] == session_id, "job_id", "not_found", 404)
            job["cancelled"] = True  # Set first: late child output can never republish this job.
            del self.jobs[job_id]
            if job["process"].is_alive():
                job["process"].terminate()

    def retire(self, session_id):
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session:
                self.cookies.pop(session["cookie"], None)
            for key, job in list(self.jobs.items()):
                if job["tuple"]["session_id"] == session_id:
                    self.cancel(key, session_id)

    def reset(self, session_id):
        with self.lock:
            self.session(session_id)
            self.retire(session_id)
            return self.new_session()

    def close(self):
        self.closed.set()
        with self.lock:
            for key, job in list(self.jobs.items()):
                self.cancel(key, job["tuple"]["session_id"])
        self.reaper.join(timeout=3)


class Application:
    def __init__(self, jobs, origin, hosted=False):
        scheme = "https://" if hosted else "http://"
        require(origin.startswith(scheme), "origin", "configuration")
        host = origin.removeprefix(scheme)
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?", host)), "origin", "configuration")
        self.jobs, self.origin, self.hosted, self.host = jobs, origin, hosted, host
        self.cookie_name = "__Host-scheduler" if hosted else "scheduler_local"

    def cookie(self, headers):
        values = headers.get_all("Cookie", [])
        require(len(values) <= 1, "cookie", "session", 403)
        require(not values or "," not in values[0], "cookie", "session", 403)
        found = []
        for piece in (values[0].split(";") if values else []):
            name, sep, value = piece.strip().partition("=")
            if name == self.cookie_name:
                require(sep and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None,
                        "cookie", "session", 403)
                found.append(value)
        require(len(found) <= 1, "cookie", "session", 403)
        return found[0] if found else None

    def set_cookie(self, value):
        return f"{self.cookie_name}={value}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.hosted else "")

    def reply(self, status, payload, content_type="application/json", cookie=None):
        raw = payload if type(payload) is bytes else json.dumps(payload, allow_nan=False).encode()
        require(len(raw) <= MAX_BODY, "response", "response_size", 500)
        headers = [("Content-Type", content_type + "; charset=utf-8"), ("Content-Length", str(len(raw))),
                   ("Cache-Control", "no-store"), ("Referrer-Policy", "same-origin"),
                   ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
        if self.hosted:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        if cookie:
            headers.append(("Set-Cookie", cookie))
        return status, headers, raw

    def handle(self, method, path, headers, read_body):
        jobs, started = self.jobs, time.monotonic()
        status, code = 200, "ok"
        try:
            require(headers.get_all("Host") == [self.host], "host", "origin", 403)
            require("?" not in path and "#" not in path, "path", "not_found", 404)
            if method == "GET" and path in ASSETS:
                filename, mime = ASSETS[path]
                require((ROOT / filename).is_file(), "asset", "not_found", 404)
                return self.reply(200, (ROOT / filename).read_bytes(), mime)
            if method == "GET" and path == "/health":
                return self.reply(200, dict(status="ok"))
            if method == "GET" and path == "/api/config":
                require(headers.get_all("Origin") in (None, [self.origin])
                        and headers.get("Sec-Fetch-Site") in (None, "none", "same-origin"),
                        "origin", "origin", 403)
                cookie = self.cookie(headers)
                with jobs.lock:
                    session_id = jobs.cookies.get(cookie)
                    if session_id and time.monotonic() - jobs.sessions[session_id]["seen"] > 1800:
                        jobs.retire(session_id)
                        session_id = None
                    session_id = session_id or jobs.new_session()
                    session = jobs.session(session_id)
                    csrf = session["csrf"]
                    set_cookie = self.set_cookie(session["cookie"])
                try:
                    model = importlib.import_module("ai").availability()
                except ImportError:
                    model = dict(identity="qwen3.5:9b", available=False, error_code="ai_unavailable")
                return self.reply(200, dict(session_id=session_id, csrf_token=csrf, bounds=LIMITS, model=model,
                                           hosting="hosted" if self.hosted else "local",
                                           examples=[dict(id=n, title=t, url=f"/examples/{n}.json") for n, t in EXAMPLES]),
                                  cookie=set_cookie)
            with jobs.lock:
                session_id = jobs.cookies.get(self.cookie(headers))
                session = jobs.session(session_id)
                require(headers.get_all("X-CSRF-Token") == [session["csrf"]], "csrf", "token", 403)
            require(headers.get_all("Origin") in (None, [self.origin]), "origin", "origin", 403)
            require(headers.get_all("X-Session-ID") in (None, [session_id]), "session_id", "session", 403)
            if method != "GET":
                require(headers.get_all("Origin") == [self.origin], "origin", "origin", 403)
            if path.startswith("/api/jobs/") and method in ("GET", "DELETE"):
                job_id = path.removeprefix("/api/jobs/")
                if method == "GET":
                    return self.reply(200, jobs.get(job_id, session_id))
                jobs.cancel(job_id, session_id)
                return self.reply(200, dict(status="cancelled"))
            require(method == "POST", "path", "not_found", 404)
            require(headers.get("Content-Type", "").split(";")[0].strip() == "application/json",
                    "content_type", "content_type", 415)
            require(not headers.get("Transfer-Encoding") and len(headers.get_all("Content-Length", [])) == 1,
                    "body", "length")
            length = headers.get("Content-Length", "")
            require(length.isascii() and length.isdigit(), "body", "length")
            require(0 < int(length) <= MAX_BODY, "body", "size", 413)
            raw = read_body(int(length))
            require(len(raw) == int(length), "body", "length")
            data = parse_json(raw)
            require(type(data) is dict, "body")
            claimed = data.get("session_id") if "session_id" in data else (data.get("tuple") or {})
            if type(claimed) is dict:
                claimed = claimed.get("session_id")
            require(claimed == session_id, "session_id", "session", 403)
            if path == "/api/reset":
                fields(data, "session_id", "body")
                with jobs.lock:
                    session_id = jobs.reset(session_id)
                    session = jobs.session(session_id)
                    return self.reply(200, dict(reset=True, session_id=session_id, csrf_token=session["csrf"]),
                                      cookie=self.set_cookie(session["cookie"]))
            if path in ("/api/validate", "/api/solve"):
                fields(data, "session_id revision input", "body")
                scenario = validate_scenario(data["input"])
                input_tuple = make_tuple(data["session_id"], data["revision"], scenario)
                if path == "/api/validate":
                    jobs.register(input_tuple)
                    return self.reply(200, dict(input=scenario, tuple=input_tuple))
                job_id = jobs.start(scenario, input_tuple)
                status = 202
                return self.reply(202, dict(job_id=job_id, tuple=input_tuple))
            required = {"/api/propose": "tuple input request", "/api/apply": "tuple input request proposal",
                        "/api/explain": "tuple input checked_result approved_diffs"}
            require(path in required, "path", "not_found", 404)
            fields(data, required[path], "body")
            scenario = validate_scenario(data["input"])
            input_tuple = check_tuple(data["tuple"], scenario)
            with jobs.lock:
                jobs.current(input_tuple)
            try:
                ai = importlib.import_module("ai")
            except ImportError:
                raise ValidationError("ai", "ai_unavailable", 503) from None
            if path == "/api/apply":
                with jobs.lock:
                    jobs.current(input_tuple)
                    output = ai.apply(scenario, input_tuple, data["request"], data["proposal"])
                    updated = validate_scenario(output["input"])
                    next_tuple = make_tuple(input_tuple["session_id"], input_tuple["revision"] + 1, updated)
                    require(output["tuple"] == next_tuple, "tuple", "invalid_proposal")
                    jobs.register(next_tuple)
            else:
                require(jobs.ai_lock.acquire(blocking=False), "ai", "busy", 409)
                try:
                    if self.hosted:
                        with jobs.lock:
                            session = jobs.session(session_id)
                            require(time.monotonic() >= session.get("ai_after", 0), "ai", "busy", 409)
                            session["ai_after"] = time.monotonic() + 15
                    if path == "/api/propose":
                        output = ai.propose(scenario, input_tuple, data["request"])
                    else:
                        with jobs.lock:
                            # Optimality may only come from this session's still-retained solver receipt.
                            require(any(j["tuple"] == input_tuple and j["candidate"] == data["checked_result"]
                                        and j["candidate"] is not None and j["candidate"]["checked"]
                                        for j in jobs.jobs.values()), "checked_result", "unknown_result", 409)
                        output = ai.explain(scenario, input_tuple, data["checked_result"], data["approved_diffs"])
                    with jobs.lock:
                        jobs.current(input_tuple)
                finally:
                    jobs.ai_lock.release()
            return self.reply(200, output)
        except ValidationError as error:
            status, code = error.status, error.code
            return self.reply(status, dict(error=dict(code=code, fields=error.fields)))
        except socket.timeout:
            status, code = 408, "connection_timeout"
            return self.reply(status, dict(error=dict(code=code, fields=[])))
        except Exception:
            status, code = 500, "internal_error"
            return self.reply(status, dict(error=dict(code=code, fields=[])))
        finally:
            print(json.dumps(dict(event="request", status=status, code=code,
                                  elapsed_ms=round(1000 * (time.monotonic() - started)))), flush=True)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address=("127.0.0.1", PORT), jobs=None):
        require(address[0] == "127.0.0.1", "bind", "loopback_required")
        self.jobs = jobs or Jobs()
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.app = Application(self.jobs, self.origin)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_close(self):
        self.jobs.close()
        super().server_close()

    def handle_error(self, request, client_address):
        print(json.dumps(dict(event="connection_error", code="connection_closed")), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalScheduler"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Never log paths, scenario content, prompts, or arbitrary exception messages.

    def route(self, method):
        status, headers, raw = self.server.app.handle(method, self.path, self.headers, self.read_body)
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def read_body(self, length):
        remaining, deadline, chunks = length, time.monotonic() + 5, []
        while remaining:
            left = deadline - time.monotonic()
            require(left > 0, "body", "body_timeout", 408)
            self.connection.settimeout(left)
            chunk = self.rfile.read1(min(65536, remaining))
            require(bool(chunk), "body", "length")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    with LocalServer() as server:
        print(json.dumps(dict(event="listening", origin=server.origin)), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
