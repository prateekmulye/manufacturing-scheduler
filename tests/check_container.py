"""Native-runner smoke check at the intended free service limits; no AI calls."""
import argparse
import http.client
import json
import re
import subprocess
import sys
import time
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=25).strip()


def resource_usage(container):
    peak = int(docker("exec", container, "cat", "/sys/fs/cgroup/memory.peak"))
    events = dict(line.split() for line in docker("exec", container, "cat", "/sys/fs/cgroup/memory.events").splitlines())
    return dict(peak_memory_bytes=peak, oom=int(events["oom"]), oom_kill=int(events["oom_kill"]))


def import_profile(container):
    logs = subprocess.run(["docker", "logs", "--tail=1000", container],
                          text=True, capture_output=True, timeout=25, check=True)
    pattern = re.compile(r"import time:[ \t]+([0-9]{1,12})[ \t]+\|[ \t]+([0-9]{1,12})[ \t]+\|[ \t]+"
                         r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
    lines = (logs.stdout + "\n" + logs.stderr).splitlines()
    rows = []
    for line in lines:
        match = pattern.fullmatch(line) if len(line) <= 256 else None
        if match:
            rows.append(dict(self_us=int(match[1]), cumulative_us=int(match[2]), module=match[3]))
    return dict(import_time_rows=rows[-200:], import_profile_truncated=len(rows) > 200 or len(lines) >= 1000)


def main(image, profile_imports=False):
    owner = uuid.uuid4().hex
    name = "scheduler-check-" + owner[:12]
    label = "dev.prateekmulye.scheduler-qa"
    headers = {"Host": "scheduler.prateekmulye.dev", "Origin": "https://scheduler.prateekmulye.dev"}
    report = {"image": image, "cpu": 0.1, "memory_bytes": 268435456, "model_calls": 0, "passed": False}
    if profile_imports:
        report["profiling"] = True
    try:
        docker("run", "-d", "--pull=never", "--name", name, "--memory=256m",
               "--label", label + "=" + owner,
               "--memory-swap=256m", "--cpus=0.1", "--pids-limit=128",
               "--security-opt=no-new-privileges", "--publish", "127.0.0.1::8080",
               "--env", "AI_GATEWAY_SECRET=" + "unused_test_credential_" + "a" * 43,
               *(["--env", "PYTHONPROFILEIMPORTTIME=1"] if profile_imports else []), image)
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
        report.update(solve_seconds=round(time.monotonic() - started, 3),
                      job_elapsed_ms=result["elapsed_ms"], checked=candidate["checked"],
                      status=candidate["status"], reason=candidate["reason"])
        assert time.monotonic() - started < 20, "solver_did_not_finish"
        assert candidate["checked"] and candidate["status"] in ("optimal", "feasible")
        assert candidate["metrics"]["total_tardiness"] == 0
        report["metrics"] = candidate["metrics"]
        report.update(resource_usage(name))
        assert report["oom"] == report["oom_kill"] == 0
        assert report["peak_memory_bytes"] <= report["memory_bytes"]
        assert request("GET", "/health")["status"] == "ok"
        report["passed"] = True
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            inspected = subprocess.run(["docker", "container", "inspect", name],
                                       text=True, capture_output=True, timeout=25)
            if inspected.returncode == 0:
                container = json.loads(inspected.stdout)[0]
                if (container["Config"].get("Labels") or {}).get(label) != owner:
                    raise RuntimeError("cleanup_owner_mismatch")
                if failed and "oom_kill" not in report:
                    try:
                        report.update(resource_usage(container["Id"]))
                    except Exception:
                        report["resource_diagnostics"] = "unavailable"
                if profile_imports:
                    try:
                        report.update(import_profile(container["Id"]))
                    except Exception:
                        report["import_profile"] = "unavailable"
                # Remove the inspected immutable ID, never a potentially reused name.
                docker("rm", "--force", container["Id"])
            elif "No such container" not in inspected.stderr:
                raise RuntimeError("cleanup_inspect_failed")
        except Exception:
            report["passed"] = False
            if not failed:
                raise
            print("Owned-container cleanup could not be verified; preserving original failure.", file=sys.stderr)
        finally:
            print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--profile-imports", action="store_true")
    args = parser.parse_args()
    main(args.image, args.profile_imports)
