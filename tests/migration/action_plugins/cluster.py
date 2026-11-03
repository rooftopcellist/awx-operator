"""In-memory Kubernetes boundary for the real migration task integration tests."""

import copy
import json
import os
from pathlib import Path

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        super().run(tmp, task_vars)
        path = Path(os.environ["MIGRATION_TEST_STATE"])
        state = json.loads(path.read_text())
        args = self._task.args
        action = self._task.action.rsplit(".", 1)[-1]
        result = {"changed": False}
        if action == "k8s_info":
            if args["kind"] == "Pod":
                if any("job-name=" in s for s in args.get("label_selectors", [])):
                    result["resources"] = [{"metadata": {"name": "check-pod"}}]
                else:
                    result["resources"] = state["pods"]
            else:
                jobs = list(state["jobs"].values())
                if args.get("name"):
                    jobs = [j for j in jobs if j["metadata"]["name"] == args["name"]]
                    for job in jobs:
                        if (self._task.until and not job["status"].get("succeeded")
                                and not job["status"].get("conditions")):
                            state["waited"].append(job["metadata"]["name"])
                            job["status"] = {"succeeded": 1}
                else:
                    for selector in args.get("label_selectors", []):
                        key, value = selector.split("=", 1)
                        jobs = [j for j in jobs if j["metadata"].get("labels", {}).get(key) == value]
                result["resources"] = copy.deepcopy(jobs)
        elif action == "k8s":
            if args.get("state") == "absent":
                state["deleted"].append(args["name"])
                state["jobs"].pop(args["name"], None)
            else:
                job = args["definition"]
                if isinstance(job, str):
                    import yaml
                    job = yaml.safe_load(job)
                job = copy.deepcopy(job)
                meta = job["metadata"]
                if "name" not in meta:
                    meta["name"] = meta["generateName"] + str(len(state["created"])).zfill(5)
                name = meta["name"]
                if name in state["jobs"]:
                    return {"failed": True, "msg": "attempted immutable Job reapply"}
                meta["uid"] = "new-" + name
                meta["creationTimestamp"] = "2026-11-02T22:00:00Z"
                job["status"] = {"succeeded": 1}
                command = job["spec"]["template"]["spec"]["containers"][0]["command"]
                if "showmigrations" in command and state.get("probe_error"):
                    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
                if "migrate" in command and state.get("migration_error"):
                    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
                state["jobs"][name] = job
                state["created"].append(name)
                result.update(changed=True, result=job)
        elif action == "k8s_exec":
            if "--version" in args["command"]:
                result.update(rc=0, stdout="4.8.12\n")
            elif state.get("probe_error"):
                state["execs"].append(args)
                result.update(failed=True, rc=1, msg="database unavailable")
            else:
                state["execs"].append(args)
                result.update(rc=0, stdout=state["plan"])
        elif action == "k8s_log":
            result["log"] = state["plan"]
        elif action == "k8s_status":
            state["status"].update(args["status"])
        path.write_text(json.dumps(state))
        return result
