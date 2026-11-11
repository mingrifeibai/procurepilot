"""在独立 kind 集群验证 Helm、共享 PVC、API 与独立 Worker。"""

import argparse
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import time
from uuid import uuid4

import httpx

from container_smoke import verify


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "procurepilot-acceptance"
CONTEXT = "kind-procurepilot-check"
NODE = "procurepilot-check-control-plane"
FIXTURE = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

class Model(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        context = json.loads(request['messages'][-1]['content'])
        assessment = {'summary':'合成夹具完成核验', 'evidence_ids':context['evidence_ids'][:1],
                      'missing_information':[], 'risks':[]}
        result = {'choices':[{'message':{'content':json.dumps(assessment)},
                             'finish_reason':'stop'}],
                  'usage':{'prompt_tokens':30, 'completion_tokens':15}}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

ThreadingHTTPServer(('0.0.0.0', 8080), Model).serve_forever()
"""


def command(*args, body=None):
    result = subprocess.run(
        args, input=body, encoding="utf-8", capture_output=True, timeout=150
    )
    if result.returncode:
        # Secret 通过 stdin 注入，错误输出可能引用原文，仅展示操作与退出码。
        if any("secret" in arg.lower() for arg in args) or body is not None:
            raise RuntimeError(
                f"{args[0]} fixture creation failed ({result.returncode})"
            )
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def provision(kube, image, buyer, approver):
    # 只允许独立临时集群。静态卷使用该容器内目录，UID 与 Helm securityContext 一致。
    directory = "/var/local/procurepilot-acceptance"
    command(
        "docker",
        "exec",
        NODE,
        "install",
        "-d",
        "-m",
        "0770",
        "-o",
        "10001",
        "-g",
        "10001",
        directory,
    )
    kube("create", "namespace", NAMESPACE)
    kube(
        "create",
        "-f",
        "-",
        body=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": "procurepilot-acceptance-data"},
                "spec": {
                    "capacity": {"storage": "1Gi"},
                    "accessModes": ["ReadWriteOnce"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": "",
                    "hostPath": {"path": directory, "type": "Directory"},
                },
            }
        ),
    )
    labels = {"app": "procurepilot-model-fixture"}
    fixtures = [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "procurepilot-data"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
                "storageClassName": "",
                "volumeName": "procurepilot-acceptance-data",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "procurepilot-runtime"},
            "stringData": {
                "DATABASE_URL": "sqlite:////data/kind-check.db",
                "MODEL_BASE_URL": "http://fixture-model:8080/v1",
                "MODEL_NAME": "smoke-fixture",
                "MODEL_API_KEY": token_urlsafe(24),
                "API_TOKENS_JSON": json.dumps(
                    {
                        buyer: {"role": "buyer", "workspaces": ["*"]},
                        approver: {"role": "approver", "workspaces": ["*"]},
                    }
                ),
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "fixture-model"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "model",
                                "image": image,
                                "imagePullPolicy": "Never",
                                "command": ["python", "-c", FIXTURE],
                                "ports": [{"containerPort": 8080}],
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "fixture-model"},
            "spec": {"selector": labels, "ports": [{"port": 8080}]},
        },
    ]
    kube(
        "-n",
        NAMESPACE,
        "create",
        "-f",
        "-",
        body=json.dumps({"apiVersion": "v1", "kind": "List", "items": fixtures}),
    )


def verify_worker(base, buyer, workspace):
    """用模拟供应商 HTTP 端点验证 Worker 独立领取 SQL 任务并回写账本。"""
    prefix = f"/api/w/{workspace}"
    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {buyer}"},
        trust_env=False,
        timeout=10,
    ) as client:
        status = client.get("/api/model-status").raise_for_status().json()
        assert status["worker_mode"] == "external" and status["configured"]
        queued = (
            client.post(
                prefix + "/analysis-jobs",
                json={"coordination_mode": "single", "idempotency_key": uuid4().hex},
            )
            .raise_for_status()
            .json()
        )
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            task = (
                client.get(prefix + "/analyses/" + queued["id"])
                .raise_for_status()
                .json()
            )
            if task["dispatch_state"] == "done":
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("Worker did not commit the queued job")
        assert task["state"] == "completed" and task["tokens"] == 45
        snapshot = client.get(prefix + "/snapshot").raise_for_status().json()
        assert len(snapshot["model_call"]) == 1 and len(snapshot["order"]) == 1
        return {"id": task["id"], "steps": task["steps"], "tokens": task["tokens"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--image", default="procurepilot/app:dynamic-local")
    parser.add_argument("--port", type=int, default=18192)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    kube_args = ("kubectl", "--kubeconfig", args.kubeconfig)

    def kube(*params, body=None):
        return command(*kube_args, *params, body=body)

    if kube("config", "current-context").strip() != CONTEXT:
        raise RuntimeError("Only the isolated ProcurePilot kind context is supported")
    buyer, approver = token_urlsafe(24), token_urlsafe(24)
    provision(kube, args.image, buyer, approver)
    repository, tag = args.image.rsplit(":", 1)
    chart = str(ROOT / "deploy/helm/procurepilot")
    values = (
        "--set",
        f"image.repository={repository}",
        "--set",
        f"image.tag={tag}",
        "--set",
        "image.pullPolicy=Never",
    )
    command(args.helm, "lint", chart, *values)
    command(
        args.helm,
        "install",
        "pilot",
        chart,
        *values,
        "--kubeconfig",
        args.kubeconfig,
        "--namespace",
        NAMESPACE,
    )
    for name in ("fixture-model", "pilot-api", "pilot-worker"):
        kube(
            "-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=120s"
        )
    forward = subprocess.Popen(
        (
            *kube_args,
            "-n",
            NAMESPACE,
            "port-forward",
            "--address",
            "127.0.0.1",
            "service/pilot-api",
            f"{args.port}:8091",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        base = f"http://127.0.0.1:{args.port}"
        result = verify(base, buyer=buyer, approver=approver, mode="configured")
        worker = verify_worker(base, buyer, result["workspace_id"])
    finally:
        forward.terminate()
        forward.wait(timeout=10)
    report = {
        "context": CONTEXT,
        "image": args.image,
        "storage": "single-node SQLite PVC",
        "fixture_model": True,
        "api": result,
        "external_worker": worker,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Helm API, simulated order, PVC and external worker verified")


if __name__ == "__main__":
    main()
