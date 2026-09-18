"""Bounded manufacturing scheduling. No network, persistence, or model authority."""
import copy
import hashlib
import json
import math
import re
import time
import uuid

MAX_BODY = 1_048_576
LIMITS = dict(orders=20, operations=60, resources=10, intervals=200, horizon=10080,
              solve_seconds=10, request_characters=2000, edits=4)
ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class ValidationError(ValueError):
    def __init__(self, path, code="invalid", status=400):
        self.fields = [{"path": path, "code": code}]
        self.code, self.status = code, status
        super().__init__(code)


def require(ok, path, code="invalid", status=400):
    if not ok:
        raise ValidationError(path, code, status)


def fields(value, names, path):
    require(type(value) is dict and set(value) == set(names.split()), path, "fields")


def integer(value, low, high, path):
    require(type(value) is int and low <= value <= high, path, "integer_bounds")


def identifier(value, path):
    require(type(value) is str and ID.fullmatch(value), path, "id")


def parse_json(raw):
    require(len(raw) <= MAX_BODY, "$", "size", 413)
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "$", "duplicate_key")
            out[key] = value
        return out
    def constant(_):
        raise ValidationError("$", "non_finite")
    def number(value):
        result = float(value)
        require(math.isfinite(result), "$", "non_finite")
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ValidationError("$", "invalid_json") from None


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def validate_scenario(data):
    fields(data, "version source horizon resources orders", "input")
    require(type(data["version"]) is int and data["version"] == 1, "version", "version")
    fields(data["source"], "kind reference rights_note", "source")
    require(data["source"]["kind"] in ("synthetic", "public"), "source.kind")
    for key in ("reference", "rights_note"):
        value = data["source"][key]
        require(type(value) is str and 1 <= len(value) <= 512 and not any(ord(c) < 32 for c in value),
                "source." + key, "text")
    h = data["horizon"]
    integer(h, 1, LIMITS["horizon"], "horizon")
    resources, orders = data["resources"], data["orders"]
    require(type(resources) is list and 1 <= len(resources) <= 10, "resources", "count")
    require(type(orders) is list and 1 <= len(orders) <= 20, "orders", "count")
    resource_ids, operation_ids, order_ids, interval_count = set(), set(), set(), 0
    for n, resource in enumerate(resources):
        path = f"resources[{n}]"
        fields(resource, "id availability", path)
        identifier(resource["id"], path + ".id")
        require(resource["id"] not in resource_ids, path + ".id", "duplicate")
        resource_ids.add(resource["id"])
        require(type(resource["availability"]) is list, path + ".availability")
        previous_end = 0
        for i, interval in enumerate(resource["availability"]):
            ip = f"{path}.availability[{i}]"
            require(type(interval) is list and len(interval) == 2, ip)
            start, end = interval
            integer(start, 0, h, ip + "[0]"); integer(end, 0, h, ip + "[1]")
            require(previous_end <= start < end, ip, "calendar_order")
            previous_end = end
            interval_count += 1
        require(interval_count <= 200, "resources", "interval_count")
    for n, order in enumerate(orders):
        path = f"orders[{n}]"
        fields(order, "id release due deadline operations", path)
        identifier(order["id"], path + ".id")
        require(order["id"] not in order_ids, path + ".id", "duplicate")
        order_ids.add(order["id"])
        for key in ("release", "due"):
            integer(order[key], 0, h, path + "." + key)
        if order["deadline"] is not None:
            integer(order["deadline"], 0, h, path + ".deadline")
        require(type(order["operations"]) is list and 1 <= len(order["operations"]) <= 60,
                path + ".operations", "count")
        for i, op in enumerate(order["operations"]):
            opath = f"{path}.operations[{i}]"
            fields(op, "id resource_id duration", opath)
            identifier(op["id"], opath + ".id")
            identifier(op["resource_id"], opath + ".resource_id")
            require(op["id"] not in operation_ids, opath + ".id", "duplicate")
            operation_ids.add(op["id"])
            require(op["resource_id"] in resource_ids, opath + ".resource_id", "unknown_resource")
            integer(op["duration"], 1, h, opath + ".duration")
        require(len(operation_ids) <= 60, "orders", "operation_count")
    return copy.deepcopy(data)


def make_tuple(session_id, revision, scenario):
    require(type(session_id) is str, "session_id")
    try:
        require(str(uuid.UUID(session_id)) == session_id, "session_id")
    except (ValueError, AttributeError):
        raise ValidationError("session_id") from None
    integer(revision, 1, 2**31 - 1, "revision")
    return dict(session_id=session_id, revision=revision, input_hash=canonical_hash(scenario))


def check_tuple(value, scenario):
    fields(value, "session_id revision input_hash", "tuple")
    expected = make_tuple(value["session_id"], value["revision"], scenario)
    require(value == expected, "tuple", "stale", 409)
    return expected


def check_assignments(scenario, assignments):
    """Independent raw-data traversal; does not consult CP-SAT model or solver claims."""
    require(type(assignments) is list and len(assignments) <= 60, "assignments", "invalid_assignment")
    by_id = {}
    calendars = {r["id"]: r["availability"] for r in scenario["resources"]}
    occupied = {key: [] for key in calendars}
    for index, row in enumerate(assignments):
        path = f"assignments[{index}]"
        fields(row, "operation_id resource_id start end", path)
        identifier(row["operation_id"], path + ".operation_id")
        identifier(row["resource_id"], path + ".resource_id")
        require(row["operation_id"] not in by_id, path, "invalid_assignment")
        integer(row["start"], 0, scenario["horizon"], path + ".start")
        integer(row["end"], 1, scenario["horizon"], path + ".end")
        by_id[row["operation_id"]] = row
    metrics = []
    expected = set()
    for order in scenario["orders"]:
        earliest = order["release"]
        for op in order["operations"]:
            expected.add(op["id"])
            row = by_id.get(op["id"])
            require(row is not None, op["id"], "invalid_assignment")
            start, end = row["start"], row["end"]
            require(row["resource_id"] == op["resource_id"] and end - start == op["duration"]
                    and start >= earliest, op["id"], "invalid_assignment")
            require(any(a <= start and end <= b for a, b in calendars[op["resource_id"]]),
                    op["id"], "invalid_assignment")
            occupied[op["resource_id"]].append((start, end))
            earliest = end
        require(order["deadline"] is None or earliest <= order["deadline"], order["id"], "invalid_assignment")
        metrics.append(dict(order_id=order["id"], completion=earliest,
                            tardiness=max(0, earliest - order["due"])))
    require(set(by_id) == expected, "assignments", "invalid_assignment")
    for rows in occupied.values():
        rows.sort()
        require(all(left[1] <= right[0] for left, right in zip(rows, rows[1:])),
                "assignments", "invalid_assignment")
    return dict(total_tardiness=sum(m["tardiness"] for m in metrics),
                makespan=max(m["completion"] for m in metrics),
                late_orders=sum(m["tardiness"] > 0 for m in metrics), orders=metrics)


def baseline(scenario):
    occupied = {r["id"]: [] for r in scenario["resources"]}
    calendars = {r["id"]: r["availability"] for r in scenario["resources"]}
    assignments = []
    for order in scenario["orders"]:
        earliest = order["release"]
        for op in order["operations"]:
            chosen = None
            for begin, end in calendars[op["resource_id"]]:
                start = max(begin, earliest)
                for busy_start, busy_end in sorted(occupied[op["resource_id"]]):
                    if busy_end <= start:
                        continue
                    if start + op["duration"] <= busy_start:
                        break
                    start = busy_end
                if start + op["duration"] <= end:
                    chosen = start
                    break
            if chosen is None:
                return dict(status="could_not_place_all_work", checked=False, assignments=None, metrics=None)
            earliest = chosen + op["duration"]
            occupied[op["resource_id"]].append((chosen, earliest))
            assignments.append(dict(operation_id=op["id"], resource_id=op["resource_id"], start=chosen, end=earliest))
        if order["deadline"] is not None and earliest > order["deadline"]:
            return dict(status="could_not_place_all_work", checked=False, assignments=None, metrics=None)
    return dict(status="complete", checked=True, assignments=assignments,
                metrics=check_assignments(scenario, assignments))


def candidate(scenario, input_tuple, status, reason=None, assignments=None, engine=None):
    metrics = None
    checked = False
    if status in ("optimal", "feasible"):
        try:
            metrics = check_assignments(scenario, assignments)
            checked = True
        except ValidationError:
            status, reason, assignments = "error", "invalid_assignment", None
    else:
        assignments = None
    result = dict(status=status, reason=reason, assignments=assignments, metrics=metrics,
                  checked=checked, engine=engine)
    result["result_hash"] = canonical_hash(dict(tuple=input_tuple, status=status,
                                               assignments=assignments, metrics=metrics))
    return result


def check_result(scenario, input_tuple, result):
    fields(result, "status reason assignments metrics checked engine result_hash", "checked_result")
    require(result["status"] in ("optimal", "feasible") and result["checked"] is True,
            "checked_result", "unchecked_result")
    # Browser result history is untrusted: recheck feasibility, never establish optimality here.
    safe = candidate(scenario, input_tuple, result["status"], result["reason"], result["assignments"], result["engine"])
    require(safe["checked"] and safe["metrics"] == result["metrics"]
            and safe["result_hash"] == result["result_hash"], "checked_result", "invalid_assignment")
    return safe


def solve(scenario, budget=10, on_incumbent=None):
    """Native solve, called only inside an owned child by the HTTP service."""
    import ortools
    from ortools.sat.python import cp_model
    started = time.monotonic()
    h, model = scenario["horizon"], cp_model.CpModel()
    starts, ends = {}, {}
    intervals = {r["id"]: [] for r in scenario["resources"]}
    calendars = {r["id"]: r["availability"] for r in scenario["resources"]}
    tardiness, completion = [], []
    for order in scenario["orders"]:
        previous = order["release"]
        for op in order["operations"]:
            ranges = [[a, b - op["duration"]] for a, b in calendars[op["resource_id"]]
                      if b - a >= op["duration"]]
            if not ranges:
                model.add(False)
                ranges = [[0, h]]
            start = model.new_int_var_from_domain(cp_model.Domain.from_intervals(ranges), op["id"])
            end = model.new_int_var(0, h, op["id"] + ":end")
            interval = model.new_interval_var(start, op["duration"], end, op["id"] + ":interval")
            model.add(start >= previous)
            intervals[op["resource_id"]].append(interval)
            starts[op["id"]], ends[op["id"]] = start, end
            previous = end
        if order["deadline"] is not None:
            model.add(previous <= order["deadline"])
        late = model.new_int_var(0, h, order["id"] + ":late")
        model.add_max_equality(late, [0, previous - order["due"]])
        tardiness.append(late); completion.append(previous)
    for values in intervals.values():
        model.add_no_overlap(values)
    makespan = model.new_int_var(0, h, "makespan")
    model.add_max_equality(makespan, completion)
    model.minimize((h + 1) * sum(tardiness) + makespan)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = max(0, budget - (time.monotonic() - started))
    def assignments(reader):
        return [dict(operation_id=op["id"], resource_id=op["resource_id"],
                     start=reader.value(starts[op["id"]]), end=reader.value(ends[op["id"]]))
                for order in scenario["orders"] for op in order["operations"]]
    class Incumbent(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            if on_incumbent:
                on_incumbent(assignments(self))
    raw = solver.solve(model, Incumbent())
    statuses = {cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
                cp_model.INFEASIBLE: "infeasible", cp_model.UNKNOWN: "timeout_no_solution",
                cp_model.MODEL_INVALID: "error"}
    status = statuses.get(raw, "error")
    full = status in ("optimal", "feasible")
    return dict(status=status, reason="time_limit" if status in ("feasible", "timeout_no_solution") else
                ("model_invalid" if status == "error" else None),
                assignments=assignments(solver) if full else None,
                engine=dict(name="OR-Tools CP-SAT", version=ortools.__version__, raw_status=solver.status_name(raw),
                            elapsed_ms=round(1000 * (time.monotonic() - started)),
                            objective_value=round(solver.objective_value) if full else None,
                            best_bound=solver.best_objective_bound if math.isfinite(solver.best_objective_bound) else None))


def solve_child(scenario, connection, budget=10):
    try:
        def send(kind, value):
            connection.send_bytes(json.dumps(dict(kind=kind, value=value), allow_nan=False).encode())
        send("final", solve(scenario, budget, lambda value: send("incumbent", value)))
    except Exception:
        try:
            send("final", dict(status="error", reason="solver_failure", assignments=None, engine=None))
        except (OSError, UnboundLocalError):
            pass
    finally:
        connection.close()
