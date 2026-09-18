"""Run: python -B -m unittest discover -s tests -v (inside this app)."""
import copy
from functools import partial
import http.client
import http.cookiejar
import importlib.util
import itertools
import json
import resource
import socket
import sys
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scheduler as s
import server
import hosted


def example(name="comparison"):
    return json.loads((server.ROOT / "examples" / (name + ".json")).read_text())


def identity(scenario):
    return s.make_tuple(str(uuid.uuid4()), 1, scenario)


def barrier_child(scenario, connection, budget, entered, release, solving):
    def send(kind, value):
        connection.send_bytes(json.dumps(dict(kind=kind, value=value)).encode())
    rows = s.baseline(scenario)["assignments"]
    if solving:
        send("ready", None)
        send("incumbent", rows)
    entered.set()
    if release.wait(5):
        send("incumbent", rows)
        send("final", dict(status="optimal", reason=None, assignments=rows, engine=None))
    connection.close()


class WatchdogChecks(unittest.TestCase):
    ready = {"kind": "ready", "value": None}

    def final(self):
        return dict(kind="final", value=dict(status="optimal", reason=None,
                    assignments=s.baseline(example())["assignments"], engine=None))

    def watch(self, events, initial=0, check_delay=None, candidate_delay=None, cancel_on_receive=False):
        clock, script = [initial], list(events)
        class Pipe:
            reads = 0
            closed = False
            def poll(self, timeout=0):
                if script:
                    clock[0] = max(clock[0], script[0][0])
                    return True
                clock[0] += timeout
                return False
            def recv_bytes(self, limit):
                _, received, message = script.pop(0)
                clock[0] = max(clock[0], received)
                self.reads += 1
                if cancel_on_receive: job["cancelled"] = True
                return json.dumps(message).encode()
            def close(self): self.closed = True
        pipe, process = Pipe(), Mock()
        process.is_alive.return_value = True
        process.terminate.side_effect = lambda: setattr(process.is_alive, "return_value", False)
        jobs = object.__new__(server.Jobs)
        jobs.lock = threading.RLock()
        jobs.startup, jobs.budget, jobs.grace = 30, 10, 3
        data = example()
        job = dict(tuple=identity(data), state="pending", candidate=None, created=0, process=process, receive=pipe,
                   cancelled=False, last_incumbent=None, input=data)
        jobs.jobs = {"job": job}
        check = server.check_assignments
        def delayed_check(*args):
            result = check(*args)
            if check_delay is not None: clock[0] = check_delay
            return result
        candidate = server.candidate
        def delayed_candidate(*args, **kwargs):
            result = candidate(*args, **kwargs)
            if result["status"] == "optimal" and candidate_delay is not None: clock[0] = candidate_delay
            return result
        with patch.object(server.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(server, "check_assignments", side_effect=delayed_check), \
                patch.object(server, "candidate", side_effect=delayed_candidate):
            jobs._watch("job", job)
        self.assertTrue(pipe.closed)
        process.terminate.assert_called_once()
        process.join.assert_called()
        return job, pipe

    def test_startup_stall_has_its_own_deadline_and_cleanup(self):
        job, pipe = self.watch([])
        self.assertEqual((job["candidate"]["status"], job["candidate"]["reason"], job["elapsed_ms"]),
                         ("timeout_no_solution", "solver_startup_timeout", 30000))
        self.assertIsNone(job["candidate"]["assignments"])
        self.assertEqual(pipe.reads, 0)

    def test_delayed_ready_receives_full_solve_budget(self):
        job, _ = self.watch([(20, 20, self.ready), (32.9, 32.9, self.final())])
        self.assertTrue(job["candidate"]["checked"])
        self.assertEqual((job["candidate"]["status"], job["elapsed_ms"]), ("optimal", 32900))

    def test_solve_expiry_preserves_only_checked_incumbent(self):
        rows = s.baseline(example())["assignments"]
        for incumbent in (None, rows):
            with self.subTest(incumbent=bool(incumbent)):
                events = [(29, 29, self.ready)]
                if incumbent: events.append((30, 30, dict(kind="incumbent", value=incumbent)))
                job, _ = self.watch(events)
                result = job["candidate"]
                self.assertEqual(result["reason"], "watchdog_timeout")
                self.assertEqual(result["status"], "feasible" if incumbent else "timeout_no_solution")
                self.assertEqual(result["checked"], bool(incumbent))
                self.assertEqual(job["elapsed_ms"], 42000)
        invalid = copy.deepcopy(rows); invalid[1]["start"] = 0
        job, _ = self.watch([(1, 1, self.ready), (2, 2, dict(kind="incumbent", value=invalid))])
        self.assertFalse(job["candidate"]["checked"])
        self.assertEqual(job["candidate"]["reason"], "invalid_assignment")

    def test_expired_messages_cannot_revive_or_promote_results(self):
        for poll, receive in ((30, 30), (29.9, 30), (31, 31)):
            with self.subTest(ready=(poll, receive)):
                job, _ = self.watch([(poll, receive, self.ready), (31, 31, self.final())])
                self.assertEqual(job["candidate"]["reason"], "solver_startup_timeout")
                self.assertFalse(job["candidate"]["checked"])
        job, pipe = self.watch([(29, 29, self.ready)], initial=30)
        self.assertEqual(pipe.reads, 0)
        self.assertEqual(job["candidate"]["reason"], "solver_startup_timeout")
        for kind in (self.final(), dict(kind="incumbent", value=s.baseline(example())["assignments"])):
            for poll, receive in ((33, 33), (32.9, 33)):
                with self.subTest(kind=kind["kind"], arrival=(poll, receive)):
                    job, _ = self.watch([(20, 20, self.ready), (poll, receive, kind)])
                    self.assertEqual(job["candidate"]["reason"], "watchdog_timeout")
                    self.assertFalse(job["candidate"]["checked"])
        job, _ = self.watch([(20, 20, self.ready), (32, 32, dict(kind="incumbent",
                            value=s.baseline(example())["assignments"]))], check_delay=33)
        self.assertFalse(job["candidate"]["checked"])
        job, _ = self.watch([(20, 20, self.ready), (32, 32, self.final())], candidate_delay=33)
        self.assertFalse(job["candidate"]["checked"])

    def test_cancelled_watch_never_publishes_late_messages(self):
        for message in (self.ready, self.final(), dict(kind="incumbent", value=s.baseline(example())["assignments"])):
            with self.subTest(kind=message["kind"]):
                job, _ = self.watch([(1, 1, message)], cancel_on_receive=True)
                self.assertEqual(job["state"], "pending")
                self.assertIsNone(job["candidate"])

    def test_ready_protocol_is_exact_and_cannot_reset_clock(self):
        early_error = dict(kind="final", value=dict(status="error", reason="solver_failure", assignments=None, engine=None))
        invalid_sequences = [
            [(1, 1, self.final())], [(1, 1, dict(kind="incumbent", value=[]))],
            [(1, 1, dict(kind="ready", value=True))], [(1, 1, {"kind": "ready"})],
            [(1, 1, dict(kind="ready", value=None, extra=True))],
            [(1, 1, self.ready), (2, 2, self.ready), (3, 3, self.final())],
            [(1, 1, dict(kind="unknown", value=None))]]
        for events in invalid_sequences:
            with self.subTest(events=events):
                job, _ = self.watch(events)
                self.assertEqual((job["candidate"]["status"], job["candidate"]["reason"]), ("error", "solver_protocol"))
        job, _ = self.watch([(1, 1, early_error)])
        self.assertEqual(job["candidate"]["reason"], "solver_failure")

    def test_constructor_bounds_fail_before_background_work(self):
        for key, cap in (("startup", 30), ("budget", 10), ("grace", 3)):
            values = [True, False, None, "1", float("nan"), float("inf"), -float("inf"), -1, cap + .1]
            if key != "grace": values.append(0)
            for value in values:
                with self.subTest(key=key, value=value), patch.object(server.threading, "Thread") as thread, \
                        patch.object(server.multiprocessing, "get_context") as context:
                    with self.assertRaises(s.ValidationError): server.Jobs(**{key: value})
                    thread.assert_not_called(); context.assert_not_called()
        with patch.object(server.threading, "Thread"):
            jobs = server.Jobs(startup=.1, budget=.1, grace=0)
            self.assertEqual((jobs.startup, jobs.budget, jobs.grace), (.1, .1, 0))

    def test_child_emits_ready_once_before_model_build(self):
        from ortools.sat.python import cp_model
        connection, messages = Mock(), []
        connection.send_bytes.side_effect = lambda raw: messages.append(json.loads(raw))
        model = cp_model.CpModel
        def build():
            self.assertEqual(messages, [self.ready])
            return model()
        with patch.object(cp_model, "CpModel", side_effect=build): s.solve_child(example(), connection)
        self.assertEqual(sum(message["kind"] == "ready" for message in messages), 1)
        self.assertEqual(messages[-1]["kind"], "final")
        self.assertTrue(s.candidate(example(), identity(example()), **messages[-1]["value"])["checked"])
        connection.close.assert_called_once()

    def test_child_import_failure_emits_only_early_error(self):
        connection, messages = Mock(), []
        connection.send_bytes.side_effect = lambda raw: messages.append(json.loads(raw))
        original_import = __import__
        def unavailable(name, *args, **kwargs):
            if name == "ortools": raise ImportError("unavailable")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable): s.solve_child(example(), connection)
        self.assertEqual(messages, [dict(kind="final", value=dict(status="error", reason="solver_failure", assignments=None, engine=None))])
        connection.close.assert_called_once()

    def test_cancel_and_session_expiry_during_startup_and_solving(self):
        for solving in (False, True):
            for expire in (False, True):
                with self.subTest(solving=solving, expire=expire):
                    jobs = server.Jobs()
                    entered, release = jobs.context.Event(), jobs.context.Event()
                    checked, finished = threading.Event(), threading.Event()
                    watch, check = jobs._watch, server.check_assignments
                    def watched(*args):
                        try: watch(*args)
                        finally: finished.set()
                    def observed(*args):
                        value = check(*args); checked.set(); return value
                    data = example(); sid = jobs.new_session(); value = s.make_tuple(sid, 1, data)
                    jobs.register(value)
                    try:
                        with patch.object(server, "solve_child", partial(barrier_child, entered=entered, release=release, solving=solving)), \
                                patch.object(jobs, "_watch", side_effect=watched), \
                                patch.object(server, "check_assignments", side_effect=observed):
                            job_id = jobs.start(data, value); job = jobs.jobs[job_id]
                            self.assertTrue(entered.wait(5))
                            if solving: self.assertTrue(checked.wait(5))
                            with self.assertRaises(s.ValidationError): jobs.start(data, value)
                            if expire:
                                jobs.sessions[sid]["seen"] = time.monotonic() - 1801
                                with self.assertRaises(s.ValidationError): jobs.session(sid)
                            else: jobs.cancel(job_id, sid)
                            self.assertTrue(finished.wait(3))
                            job["process"].join(1)
                            self.assertFalse(job["process"].is_alive())
                            self.assertNotIn(job_id, jobs.jobs)
                            self.assertEqual(job["state"], "pending")
                    finally:
                        # A terminated process may leave an Event lock held; never
                        # touch its barrier after termination. Closing Jobs kills it.
                        jobs.close()


class ContainerSmokeChecks(unittest.TestCase):
    def test_both_initial_and_followup_solve_must_be_useful_within_45_seconds(self):
        spec = importlib.util.spec_from_file_location("container_smoke", server.ROOT / "tests/check_container.py")
        smoke = importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
        for elapsed, second_checked, succeeds in ((25, True, True), (44.999, True, True), (45, True, False), (25, False, False)):
            with self.subTest(elapsed=elapsed, second_checked=second_checked):
                clock, solves, reports, removed = [0], [0], [], []
                class Connection:
                    def __init__(self, *args, **kwargs): pass
                    def request(self, method, path, *args): self.path = path
                    def getresponse(self):
                        if self.path == "/health": data = dict(status="ok")
                        elif self.path == "/api/config": data = dict(hosting="hosted", csrf_token="test", session_id="test")
                        elif self.path == "/examples/comparison.json": data = example()
                        elif self.path == "/api/validate": data = {}
                        elif self.path == "/api/solve":
                            solves[0] += 1; clock[0] += elapsed; data = dict(job_id=str(solves[0]))
                        else:
                            checked = solves[0] == 1 or second_checked
                            data = dict(state="complete", elapsed_ms=round(elapsed * 1000), candidate=dict(
                                checked=checked, status="optimal" if checked else "timeout_no_solution",
                                reason=None if checked else "watchdog_timeout", metrics=dict(total_tardiness=0)))
                        return Mock(status=202 if self.path == "/api/solve" else 200,
                                    read=lambda limit: json.dumps(data).encode(), getheader=lambda key: None)
                    def close(self): pass
                def docker(*args):
                    if args[0] == "inspect":
                        return json.dumps([dict(NetworkSettings={"Ports": {"8080/tcp": [{"HostPort": "12345"}]}},
                                                Image="test", HostConfig=dict(Memory=268435456, NanoCpus=100000000))])
                    if args[0] == "exec": return "100" if args[-1].endswith("memory.peak") else "oom 0\noom_kill 0"
                    if args[0] == "rm": removed.append(args[-1])
                    return "owned-id"
                inspected = Mock(returncode=0, stderr="", stdout=json.dumps([dict(Id="owned-id", Config={
                    "Labels": {"dev.prateekmulye.scheduler-qa": "a" * 32}})]))
                with patch.object(smoke, "docker", side_effect=docker), patch.object(smoke.subprocess, "run", return_value=inspected), \
                        patch.object(smoke.http.client, "HTTPConnection", Connection), \
                        patch.object(smoke.uuid, "uuid4", return_value=Mock(hex="a" * 32)), \
                        patch.object(smoke.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch("builtins.print", side_effect=lambda text, **kwargs: reports.append(json.loads(text))):
                    if succeeds: smoke.main("test-image")
                    else:
                        with self.assertRaises(AssertionError): smoke.main("test-image")
                self.assertEqual(reports[-1]["passed"], succeeds)
                self.assertEqual(removed, ["owned-id"])
                if elapsed < 45:
                    self.assertEqual(solves[0], 2)
                    self.assertEqual([item["attempt"] for item in reports[-1]["solves"]], ["initial", "followup"])


class SchedulerChecks(unittest.TestCase):
    def test_calendar_route_and_adjacent_intervals(self):
        data = s.validate_scenario(example("calendar"))
        baseline = s.baseline(data)
        self.assertEqual([(a["start"], a["end"]) for a in baseline["assignments"]], [(30, 90), (90, 150)])
        self.assertEqual(baseline["metrics"]["makespan"], 150)
        data["resources"][0]["availability"] = [[0, 30], [30, 60]]
        self.assertEqual(s.solve(data)["status"], "infeasible")  # Cannot bridge even adjacent intervals.

    def test_objective_and_useful_comparison(self):
        data = s.validate_scenario(example())
        baseline, raw = s.baseline(data), s.solve(data)
        checked = s.candidate(data, identity(data), **raw)
        self.assertEqual(baseline["metrics"]["total_tardiness"], 120)
        self.assertEqual((checked["status"], checked["metrics"]["total_tardiness"], checked["metrics"]["makespan"]),
                         ("optimal", 0, 180))
        data["resources"][0]["availability"] = [[60, 240]]
        checked = s.candidate(data, identity(data), **s.solve(data))
        self.assertEqual(checked["metrics"]["total_tardiness"], 60)
        self.assertEqual(checked["metrics"]["orders"][1]["completion"], 120)

    def test_hard_soft_and_valid_infeasible_inputs(self):
        data = example("deadlines")
        self.assertEqual(s.solve(s.validate_scenario(data))["status"], "infeasible")
        for order in data["orders"]:
            order["deadline"] = None
        result = s.candidate(data, identity(data), **s.solve(data))
        self.assertEqual(result["metrics"]["total_tardiness"], 60)
        data["orders"][0].update(release=90, deadline=60)
        self.assertEqual(s.solve(s.validate_scenario(data))["status"], "infeasible")

    def test_tiny_bruteforce_objective(self):
        data = example()
        data["horizon"] = 7
        data["resources"][0]["availability"] = [[0, 7]]
        for order, duration, due in zip(data["orders"], [3, 2], [6, 2]):
            order.update(due=due)
            order["operations"][0]["duration"] = duration
        feasible = []
        for a, b in itertools.product(range(5), range(6)):
            rows = [dict(operation_id="A1", resource_id="M", start=a, end=a+3),
                    dict(operation_id="B1", resource_id="M", start=b, end=b+2)]
            try:
                m = s.check_assignments(data, rows)
                feasible.append((8 * m["total_tardiness"] + m["makespan"], rows))
            except s.ValidationError:
                pass
        result = s.solve(data)
        self.assertEqual(result["engine"]["objective_value"], min(row[0] for row in feasible))

    def test_input_boundary_cases(self):
        original = example()
        changes = [lambda d: d["orders"].clear(), lambda d: d.update(version=True),
                   lambda d: d["orders"][0]["operations"][0].update(duration=True),
                   lambda d: d["orders"][0]["operations"][0].update(duration=0.5),
                   lambda d: d["orders"][0]["operations"][0].pop("duration"),
                   lambda d: d["orders"][0]["operations"][0].update(resource_id="unknown"),
                   lambda d: d["orders"][1]["operations"][0].update(id="A1"),
                   lambda d: d["resources"][0].update(availability=[[0, 100], [99, 240]]),
                   lambda d: d.update(extra="no"), lambda d: d.update(horizon=10081)]
        for change in changes:
            data = copy.deepcopy(original); change(data)
            with self.subTest(data=data), self.assertRaises(s.ValidationError):
                s.validate_scenario(data)
        for raw in (b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}',
                    b"9"*5000, b"x" * (s.MAX_BODY+1)):
            with self.assertRaises(s.ValidationError):
                s.parse_json(raw)
        valid = s.validate_scenario(original)
        valid["orders"][0]["due"] = 1
        self.assertEqual(original["orders"][0]["due"], 240)

    def test_independent_checker_and_stale_hash(self):
        data = example()
        rows = s.baseline(data)["assignments"]
        rows[1].update(start=0, end=60)
        result = s.candidate(data, identity(data), "optimal", assignments=rows)
        self.assertEqual((result["status"], result["reason"], result["checked"]), ("error", "invalid_assignment", False))
        value = identity(data); value["input_hash"] = "0"*64
        with self.assertRaises(s.ValidationError):
            s.check_tuple(value, data)

    def test_real_unknown_and_upper_admitted_case(self):
        self.assertEqual(s.solve(example(), budget=0)["status"], "timeout_no_solution")
        data = example(); data["horizon"] = 10080
        data["resources"] = [dict(id=f"M{i}", availability=[[j*504, j*504+400] for j in range(20)]) for i in range(10)]
        data["orders"] = [dict(id=f"O{i}", release=i*3, due=240+i*10, deadline=None,
                               operations=[dict(id=f"O{i}.{j}", resource_id=f"M{(i+j)%10}", duration=30+j*10)
                                           for j in range(3)]) for i in range(20)]
        data = s.validate_scenario(data)
        started = time.monotonic(); result = s.candidate(data, identity(data), **s.solve(data))
        self.assertTrue(result["checked"])
        print(json.dumps(dict(probe="20_orders_60_operations_10_resources_200_intervals",
                              status=result["status"], elapsed_seconds=round(time.monotonic()-started, 3),
                              peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              metrics=result["metrics"])))


class HTTPChecks(unittest.TestCase):
    def setUp(self):
        self.model = patch("ai.availability", return_value=dict(identity=None, available=False, error_code="ai_unavailable"))
        self.model.start(); self.addCleanup(self.model.stop)
        self.cookie = None
        self.server = server.LocalServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.config = self.request("GET", "/api/config")[1]
        self.sid = self.config["session_id"]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        base = {"Origin": self.server.origin, "Content-Type": "application/json"}
        if hasattr(self, "config"):
            base.update({"X-CSRF-Token": self.config["csrf_token"], "X-Session-ID": self.config["session_id"]})
        if self.cookie:
            base["Cookie"] = self.cookie
        base.update(headers or {})
        connection.request(method, path, None if payload is None else json.dumps(payload), base)
        response = connection.getresponse()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        raw = response.read(); connection.close()
        return response.status, json.loads(raw)

    def validated(self, revision=1, data=None):
        data = data or example()
        request = dict(session_id=self.sid, revision=revision, input=data)
        code, response = self.request("POST", "/api/validate", request)
        self.assertEqual(code, 200)
        return request, response["tuple"]

    def test_origin_csrf_host_and_bootstrap(self):
        for headers in ({"Origin":"https://evil.test"}, {"Sec-Fetch-Site":"cross-site"}, {"Sec-Fetch-Site":"same-site"}):
            self.assertEqual(self.request("GET", "/api/config", headers=headers)[0], 403)
        payload = dict(session_id=self.sid, revision=1, input=example())
        for headers in ({"Origin":"null"}, {"X-CSRF-Token":"wrong"}, {"Host":"localhost"}):
            self.assertEqual(self.request("POST", "/api/validate", payload, headers)[0], 403)

    def test_stale_input_and_invalid_import_preserve_head(self):
        request, old = self.validated()
        changed = example(); changed["orders"][0]["due"] = 200
        self.validated(2, changed)
        self.assertEqual(self.request("POST", "/api/solve", request)[0], 409)
        request["input"]["orders"] = []
        self.assertEqual(self.request("POST", "/api/validate", request)[0], 400)
        self.assertEqual(self.server.jobs.sessions[self.sid]["head"]["revision"], 2)
        forged = dict(tuple=old, input=example(), checked_result={}, approved_diffs=[])
        self.assertEqual(self.request("POST", "/api/explain", forged)[0], 409)

    def test_real_job_repeat_reads_reset_and_session_isolation(self):
        request, value = self.validated()
        status, response = self.request("POST", "/api/solve", request)
        self.assertEqual(status, 202)
        path = "/api/jobs/" + response["job_id"]
        until = time.monotonic()+45
        while time.monotonic() < until:
            _, result = self.request("GET", path)
            if result["state"] == "complete":
                break
            time.sleep(0.03)
        self.assertTrue(result["candidate"]["checked"])
        self.assertEqual(result, self.request("GET", path)[1])
        wrong_session = self.server.jobs.new_session()
        self.assertEqual(self.request("GET", path, headers={"X-Session-ID":wrong_session})[0], 403)
        status, reset = self.request("POST", "/api/reset", {"session_id": self.sid})
        self.assertEqual(status, 200)
        self.assertNotEqual(reset["session_id"], self.sid)
        self.assertEqual(self.request("GET", path)[0], 403)
        self.assertNotIn(self.sid, self.server.jobs.sessions)

    def test_total_body_deadline(self):
        connection = socket.create_connection(self.server.server_address, timeout=7)
        header = (f"POST /api/validate HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                  f"Origin: {self.server.origin}\r\nX-CSRF-Token: {self.config['csrf_token']}\r\n"
                  f"Cookie: {self.cookie}\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n")
        connection.sendall(header.encode())
        stopped = threading.Event()
        def drip():
            while not stopped.wait(0.1):
                try:
                    connection.sendall(b" ")
                except OSError:
                    break
        sender = threading.Thread(target=drip, daemon=True); sender.start()
        started = time.monotonic()
        try:
            response = connection.recv(65536)
            self.assertIn(b"408", response.split(b"\r\n")[0])
            self.assertLess(time.monotonic()-started, 6)
        finally:
            stopped.set(); sender.join(1); connection.close()

class HostedEntrypointChecks(unittest.TestCase):
    def test_native_backend_only_listens_behind_proxy(self):
        with patch.dict("os.environ", {"PUBLIC_ORIGIN": "https://scheduler.test", "PORT": "8081"}), \
                patch("hosted.HostedApplication") as application, patch("waitress.serve") as serve:
            hosted.main()
            serve.assert_called_once_with(application.return_value, host="127.0.0.1", port=8081,
                                          **hosted.WAITRESS_OPTIONS)
            application.return_value.jobs.close.assert_called_once()


class HostedChecks(unittest.TestCase):
    """Real Waitress HTTP requests; cookie policy sees the canonical HTTPS origin."""
    def setUp(self):
        from waitress.server import create_server
        self.model = patch("ai.availability", return_value=dict(identity=None, available=False, error_code="ai_unavailable"))
        self.availability = self.model.start(); self.addCleanup(self.model.stop)
        self.origin = "https://scheduler.test"
        self.app = hosted.HostedApplication(self.origin)
        self.socket_map = {}
        self.http = create_server(self.app, host="127.0.0.1", port=0, map=self.socket_map,
                                  asyncore_loop_timeout=0.1, **hosted.WAITRESS_OPTIONS)
        self.thread = threading.Thread(target=self.http.run, daemon=True); self.thread.start()

    def tearDown(self):
        self.app.jobs.close()
        self.http.task_dispatcher.shutdown()
        for connection in list(self.socket_map.values()):
            connection.close()
        self.thread.join(2)

    def browser(self):
        return dict(jar=http.cookiejar.CookieJar(), config=None)

    def request(self, browser, method, path, payload=None, headers=None, raw=None):
        request = urllib.request.Request(self.origin + path, method=method)
        request.add_header("Origin", self.origin)
        request.add_header("Host", "scheduler.test")
        request.add_header("Content-Type", "application/json")
        if browser["config"]:
            request.add_header("X-CSRF-Token", browser["config"]["csrf_token"])
        browser["jar"].add_cookie_header(request)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        connection = http.client.HTTPConnection("127.0.0.1", int(self.http.effective_port), timeout=5)
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        connection.request(method, path, body, dict(request.header_items()))
        response = connection.getresponse()
        browser["jar"].extract_cookies(response, request)
        response_headers = dict(response.getheaders())
        data = response.read(); connection.close()
        decoded = json.loads(data) if "application/json" in response_headers.get("Content-Type", "") else data
        return response.status, decoded, response_headers

    def bootstrap(self, browser):
        status, config, headers = self.request(browser, "GET", "/api/config")
        self.assertEqual(status, 200)
        browser["config"] = config
        return config, headers

    def test_cookie_isolation_csrf_tuple_reset_and_expiry(self):
        a, b = self.browser(), self.browser()
        ca, ha = self.bootstrap(a); cb, _ = self.bootstrap(b)
        cookie = next(iter(a["jar"]))
        self.assertTrue(cookie.secure)
        self.assertFalse(cookie.domain_specified)
        self.assertEqual(cookie.path, "/")
        for attr in ("__Host-scheduler=", "HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(attr, ha["Set-Cookie"])
        self.assertNotIn("Domain=", ha["Set-Cookie"])
        self.assertNotEqual(ca["csrf_token"], cb["csrf_token"])
        self.assertNotEqual(cookie.value, ca["session_id"])
        self.assertEqual(self.bootstrap(a)[0]["session_id"], ca["session_id"])
        payload = dict(session_id=ca["session_id"], revision=1, input=example())
        status, validated, _ = self.request(a, "POST", "/api/validate", payload)
        self.assertEqual(status, 200)
        for path in ("/api/validate", "/api/solve", "/api/reset"):
            self.assertEqual(self.request(b, "POST", path, payload)[0], 403)
        for path in ("/api/propose", "/api/apply", "/api/explain"):
            self.assertEqual(self.request(b, "POST", path, dict(tuple=validated["tuple"]))[0], 403)
        for headers in ({"X-CSRF-Token": cb["csrf_token"]}, {"Cookie": ""},
                        {"Cookie": f"{cookie.name}={cookie.value}; {cookie.name}={cookie.value}"},
                        {"Cookie": f"other=x,{cookie.name}={cookie.value}"},
                        {"Host": "evil.test", "X-Forwarded-Host": "scheduler.test"},
                        {"Origin": "https://evil.test"}):
            self.assertEqual(self.request(a, "POST", "/api/validate", payload, headers)[0], 403)
        for headers in ({"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "same-site"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request(a, "GET", "/api/config", headers=headers)[0], 403)
        # Forwarded headers never change canonical host/origin or cookie attributes.
        self.assertEqual(self.request(a, "POST", "/api/validate", payload,
                                      {"X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "http"})[0], 200)
        with patch("ai.propose", return_value=dict(test_only=True)) as model:
            action = dict(tuple=validated["tuple"], input=example(), request="Set due for A to 200")
            self.assertEqual(self.request(a, "POST", "/api/propose", action)[0], 200)
            self.assertEqual(self.request(a, "POST", "/api/propose", action)[0], 409)
            self.assertEqual(model.call_count, 1)
            self.assertFalse(self.app.jobs.ai_lock.locked())
        status, job, _ = self.request(a, "POST", "/api/solve", payload)
        self.assertEqual(status, 202)
        path = "/api/jobs/" + job["job_id"]
        self.assertEqual(self.request(a, "GET", path)[0], 200)
        for method in ("GET", "DELETE"):
            self.assertEqual(self.request(b, method, path)[0], 404)
            self.assertEqual(self.request(a, method, path, headers={"X-CSRF-Token": "bad"})[0], 403)
            self.assertEqual(self.request(b, method, path, headers={"X-Session-ID": ca["session_id"]})[0], 403)
        old_cookie, old_csrf = cookie.value, ca["csrf_token"]
        status, reset, _ = self.request(a, "POST", "/api/reset", dict(session_id=ca["session_id"]))
        self.assertEqual(status, 200)
        self.assertNotEqual(reset["session_id"], ca["session_id"])
        self.assertNotEqual(reset["csrf_token"], old_csrf)
        self.assertNotEqual(next(iter(a["jar"])).value, old_cookie)
        self.assertEqual(self.request(a, "GET", path)[0], 403)  # Old token, new cookie.
        a["config"] = reset
        self.assertEqual(self.request(a, "GET", path)[0], 404)
        self.assertEqual(self.request(a, "GET", path, headers={"Cookie": f"__Host-scheduler={old_cookie}"})[0], 403)
        with self.app.jobs.lock:
            self.app.jobs.sessions[reset["session_id"]]["seen"] -= 1801
        self.assertEqual(self.request(a, "GET", path)[0], 403)
        self.assertNotEqual(self.bootstrap(a)[0]["session_id"], reset["session_id"])

    def test_health_limits_body_cap_and_no_temporary_body_files(self):
        from waitress.buffers import TempfileBasedBuffer
        browser = self.browser()
        self.assertEqual(self.request(browser, "GET", "/health")[1], dict(status="ok"))
        self.assertEqual(len(self.app.jobs.sessions), 0)
        self.availability.assert_not_called()
        config, _ = self.bootstrap(browser)
        payload = json.dumps(dict(session_id=config["session_id"], revision=1, input=example())).encode()
        full = payload + b" " * (s.MAX_BODY - len(payload))
        with patch.object(TempfileBasedBuffer, "newfile", side_effect=AssertionError("body spilled to disk")) as tempfile:
            self.assertEqual(self.request(browser, "POST", "/api/validate", raw=full)[0], 200)
            self.assertEqual(self.request(browser, "GET", "/newsreader-latin-var.woff2")[0], 200)
            tempfile.assert_not_called()
        self.assertEqual(self.request(browser, "POST", "/api/validate", raw=b"",
                                      headers={"Content-Length": str(s.MAX_BODY + 1)})[0], 413)
        for _ in range(31):
            self.app.jobs.new_session()
        self.assertEqual(self.request(self.browser(), "GET", "/api/config")[0], 409)
        self.assertEqual(len(self.app.jobs.sessions), 32)
        self.assertEqual(len(self.app.jobs.cookies), 32)
        with self.assertRaises(s.ValidationError):
            self.app.core.reply(200, b"x" * (s.MAX_BODY + 1))

    def test_hosted_configuration_rejects_unsafe_origins(self):
        for origin in ("", "http://scheduler.test", "https://user:pass@scheduler.test", "https://scheduler.test/", "https://scheduler.test?x=1"):
            with self.assertRaises(s.ValidationError):
                server.Application(self.app.jobs, origin, hosted=True)
        # WSGI does not trust HTTPS/host assertions from forwarding headers.
        self.assertEqual(self.request(self.browser(), "GET", "/health",
                                      headers={"Host": "evil.test", "Forwarded": "host=scheduler.test;proto=https"})[0], 403)


if __name__ == "__main__":
    unittest.main()
