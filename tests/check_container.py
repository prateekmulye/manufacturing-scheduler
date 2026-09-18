"""Native-runner smoke check at the intended free service limits; no AI calls."""
import http.client
import json
import subprocess
import sys
import time
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=25).strip()


def main(image):
    name = "scheduler-check-" + uuid.uuid4().hex[:12]
    created = False
    headers = {"Host": "scheduler.prateekmulye.dev", "Origin": "https://scheduler.prateekmulye.dev"}
    report = {"image": image, "cpu": 0.1, "memory_bytes": 268435456, "model_calls": 0}
    try:
        docker("run", "-d", "--pull=never", "--name", name, "--memory=256m",
               "--memory-swap=256m", "--cpus=0.1", "--pids-limit=128",
               "--security-opt=no-new-privileges", "--publish", "127.0.0.1::8080",
               "--env", "AI_GATEWAY_SECRET=" + "unused_test_credential_" + "a" * 43, image)
        created = True
        config = json.loads(docker("inspect", name))[0]
        port = int(config["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"])
        report["image_id"] = config["Image"]
        assert config["HostConfig"]["Memory"] == report["memory_bytes"]
        assert config["HostConfig"]["NanoCpus"] == 100_000_000

        def request(method, path, body=None, expected=200):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                body = None if body is None else json.dumps(body).encode()
                connection.request(method, path, body, {**headers, "Content-Type": "application/json"})
                response = connection.getresponse()
                raw = response.read(1_048_577)
                assert response.status == expected, (path, response.status)
                assert len(raw) <= 1_048_576
                cookie = response.getheader("Set-Cookie")
                if cookie:
                    headers["Cookie"] = cookie.split(";", 1)[0]
                return json.loads(raw)
            finally:
                connection.close()

        started = time.monotonic()
        while True:
            try:
                assert request("GET", "/health")["status"] == "ok"
                break
            except (OSError, http.client.HTTPException, AssertionError):
                if time.monotonic() - started >= 90:
                    raise
                time.sleep(0.25)
        report["readiness_wait_seconds"] = round(time.monotonic() - started, 3)
        session = request("GET", "/api/config")
        assert session["hosting"] == "hosted"
        headers.update({"X-CSRF-Token": session["csrf_token"], "X-Session-ID": session["session_id"]})
        scenario = request("GET", "/examples/comparison.json")
        payload = {"session_id": session["session_id"], "revision": 1, "input": scenario}
        request("POST", "/api/validate", payload)
        started = time.monotonic()
        job = request("POST", "/api/solve", payload, expected=202)
        while True:
            result = request("GET", "/api/jobs/" + job["job_id"])
            if result["state"] == "complete":
                break
            assert time.monotonic() - started < 20, "solver_did_not_finish"
            time.sleep(0.2)
        candidate = result["candidate"]
        assert time.monotonic() - started < 20, "solver_did_not_finish"
        assert candidate["checked"] and candidate["status"] in ("optimal", "feasible")
        assert candidate["metrics"]["total_tardiness"] == 0
        report.update(solve_seconds=round(time.monotonic() - started, 3),
                      status=candidate["status"], metrics=candidate["metrics"])
        report["peak_memory_bytes"] = int(docker("exec", name, "cat", "/sys/fs/cgroup/memory.peak"))
        events = dict(line.split() for line in docker("exec", name, "cat", "/sys/fs/cgroup/memory.events").splitlines())
        assert int(events["oom"]) == int(events["oom_kill"]) == 0
        assert report["peak_memory_bytes"] <= report["memory_bytes"]
        assert request("GET", "/health")["status"] == "ok"
        report["passed"] = True
        print(json.dumps(report, sort_keys=True), flush=True)
    finally:
        if created:
            docker("rm", "--force", name)


if __name__ == "__main__":
    main(sys.argv[1])
