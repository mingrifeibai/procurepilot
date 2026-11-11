"""在专用双节点 kind 集群实测 Helm、HTTPS S3 和并发采购。"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import time

import httpx

from verify_kubernetes import FIXTURE, ROOT, command, verify_worker

CLUSTER = "procurepilot-ha-check"
NS = "procurepilot-ha-acceptance"
IMAGE = "procurepilot/ha-fixture:local"
HOST = "s3-fixture"


def certificates(directory):
    """临时根证书只供验收 Pod 信任，服务私钥通过 Kubernetes Secret 注入。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def name(value):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, value)])

    def certificate(subject, issuer, public_key):
        now = datetime.now(timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
        )

    root_key, tls_key = key(), key()
    root_name = name("ProcurePilot temporary verification CA")
    root = (
        certificate(root_name, root_name, root_key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    tls = (
        certificate(name(HOST), root_name, tls_key.public_key())
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(root.public_bytes(serialization.Encoding.PEM))
    return {
        "tls.crt": tls.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "tls.key": tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    }


def workload(name, image, port, *, args=None, env=None, volumes=None, mounts=None):
    """统一定义验收用 Service 与单副本依赖。"""
    labels = {"app": name}
    container = {
        "name": name,
        "image": image,
        "imagePullPolicy": "Never",
        "ports": [{"containerPort": port}],
        "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 2},
        "resources": {"requests": {"cpu": "50m", "memory": "96Mi"}},
    }
    if args:
        container["command"] = args
    if env:
        container["env"] = [{"name": key, "value": value} for key, value in env.items()]
    if mounts:
        container["volumeMounts"] = mounts
    pod = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 10,
        "containers": [container],
    }
    if volumes:
        pod["volumes"] = volumes
    return [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {"selector": labels, "ports": [{"port": port}]},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": pod},
            },
        },
    ]


def secret(name, values):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "stringData": values,
    }


def provision(kube, tls, buyer, approver):
    password, access, s3_secret = (
        token_urlsafe(20),
        token_urlsafe(14),
        token_urlsafe(24),
    )
    aws = {"AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": s3_secret}
    runtime = {
        **aws,
        "AWS_CA_BUNDLE": "/opt/fixture-ca.pem",
        "DATABASE_URL": f"postgresql+psycopg://pilot:{password}@postgres-fixture:5432/procurepilot",
        "OBJECT_S3_ENDPOINT": f"https://{HOST}:8334",
        "OBJECT_S3_BUCKET": "procurepilot-fixture",
        "MODEL_BASE_URL": "http://model-fixture:8080/v1",
        "MODEL_NAME": "fixture",
        "MODEL_API_KEY": token_urlsafe(20),
        "API_TOKENS_JSON": json.dumps(
            {
                buyer: {"role": "buyer", "workspaces": ["*"]},
                approver: {"role": "approver", "workspaces": ["*"]},
            }
        ),
    }
    fixtures = [
        secret("s3-tls", tls),
        secret("procurepilot-runtime", runtime),
        *workload(
            "postgres-fixture",
            "postgres:17-alpine",
            5432,
            env={
                "POSTGRES_USER": "pilot",
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": "procurepilot",
            },
        ),
        *workload(
            HOST,
            IMAGE,
            8334,
            args=[
                "/usr/local/bin/weed",
                "mini",
                "-dir=/tmp",
                "-ip=127.0.0.1",
                "-ip.bind=0.0.0.0",
                "-s3.port.https=8334",
                "-s3.cert.file=/tls/tls.crt",
                "-s3.key.file=/tls/tls.key",
                "-admin.port.legacy=0",
            ],
            env={**aws, "S3_BUCKET": "procurepilot-fixture", "GOMAXPROCS": "2"},
            volumes=[{"name": "tls", "secret": {"secretName": "s3-tls"}}],
            mounts=[{"name": "tls", "mountPath": "/tls", "readOnly": True}],
        ),
        *workload("model-fixture", IMAGE, 8080, args=["python", "-c", FIXTURE]),
    ]
    kube("create", "namespace", NS)
    kube(
        "-n",
        NS,
        "create",
        "-f",
        "-",
        body=json.dumps({"apiVersion": "v1", "kind": "List", "items": fixtures}),
    )
    for name in ("postgres-fixture", HOST, "model-fixture"):
        kube("-n", NS, "rollout", "status", f"deployment/{name}", "--timeout=90s")


@contextmanager
def forward(kube_args, pod):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        (
            *kube_args,
            "-n",
            NS,
            "port-forward",
            "--address",
            "127.0.0.1",
            f"pod/{pod}",
            f"{port}:8091",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
            for _ in range(30):
                if process.poll() is not None:
                    raise RuntimeError("kubectl port-forward exited")
                try:
                    if client.get("/api/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.4)
            else:
                raise TimeoutError("API pod did not respond")
            yield client, base
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def procurement(first, second, buyer, approver):
    """两个节点回读同一份 S3 原件，竞态提交只产生一个订单与 Outbox。"""
    for client in (first, second):
        client.headers["Authorization"] = f"Bearer {buyer}"
    wid = (
        first.post("/api/workspaces", json={"name": "跨节点合成验收"})
        .raise_for_status()
        .json()["id"]
    )
    prefix = f"/api/w/{wid}"
    raw = "跨节点 S3 证据原件\n".encode()
    attachment = (
        first.post(
            prefix + "/attachments",
            json={
                "filename": "fixture.txt",
                "content_base64": base64.b64encode(raw).decode(),
            },
        )
        .raise_for_status()
        .json()["id"]
    )
    assert (
        second.get(f"{prefix}/attachments/{attachment}/original")
        .raise_for_status()
        .content
        == raw
    )
    first.post(
        prefix + "/requirements",
        json={
            "title": "双节点电脑采购",
            "budget": "8000",
            "delivery_days": 10,
            "needs": [{"category": "computer", "quantity": 1, "min_memory_gb": 32}],
        },
    ).raise_for_status()
    plan = second.post(prefix + "/plans").raise_for_status().json()
    path = prefix + "/plans/" + plan["id"]
    first.post(
        path + "/approve", headers={"Authorization": f"Bearer {approver}"}
    ).raise_for_status()

    def execute(client):
        return (
            client.post(path + "/execute", json={"idempotency_key": "ha-fixture-order"})
            .raise_for_status()
            .json()["id"]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(execute, (first, second)))
    snapshot = second.get(prefix + "/snapshot").raise_for_status().json()
    quote = next(
        row for row in snapshot["quote"] if row["id"] == plan["lines"][0]["quote_id"]
    )
    assert len(set(orders)) == len(snapshot["order"]) == len(snapshot["outbox"]) == 1
    assert quote["stock"] == 99
    return wid, attachment, raw, orders[0]


def verify_api_replacement(kube, kube_args, api_names, live_api, buyer, fixture):
    """替换一个 API Pod，核对存活副本和新副本仍能读到已提交数据。"""
    wid, attachment, raw, order_id = fixture
    old_api = api_names[1]
    prefix = f"/api/w/{wid}"
    kube("-n", NS, "delete", "pod", old_api, "--timeout=40s")
    assert (
        live_api.get(f"{prefix}/attachments/{attachment}/original")
        .raise_for_status()
        .content
        == raw
    )
    deadline = time.monotonic() + 90
    selector = "app.kubernetes.io/instance=pilot,app.kubernetes.io/component=api"
    replacement = None
    while time.monotonic() < deadline:
        pods = json.loads(kube("-n", NS, "get", "pods", "-l", selector, "-o", "json"))[
            "items"
        ]
        for pod in pods:
            meta = pod["metadata"]
            if meta["name"] not in api_names and any(
                cond["type"] == "Ready" and cond["status"] == "True"
                for cond in pod.get("status", {}).get("conditions", [])
            ):
                replacement = meta["name"]
                break
        if replacement:
            break
        time.sleep(1)
    if replacement is None:
        raise TimeoutError("API replacement did not become ready")
    with forward(kube_args, replacement) as (client, _):
        client.headers["Authorization"] = f"Bearer {buyer}"
        assert (
            client.get(f"{prefix}/attachments/{attachment}/original")
            .raise_for_status()
            .content
            == raw
        )
        snapshot = client.get(prefix + "/snapshot").raise_for_status().json()
        assert [order["id"] for order in snapshot["order"]] == [order_id]
        assert len(snapshot["outbox"]) == 1
    return {"removed": old_api, "replacement": replacement, "retained_order": order_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--helm", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--weed", type=Path, required=True)
    parser.add_argument("--app-image", default="procurepilot/app:0.8.0-local")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    kube_args = ("kubectl", "--kubeconfig", args.kubeconfig)

    def kube(*params, body=None):
        return command(*kube_args, *params, body=body)

    if kube("config", "current-context").strip() != "kind-" + CLUSTER:
        raise RuntimeError("需要专用 ProcurePilot 双节点测试集群")
    nodes = {
        item["metadata"]["name"]
        for item in json.loads(kube("get", "nodes", "-o", "json"))["items"]
    }
    if nodes != {f"{CLUSTER}-control-plane", f"{CLUSTER}-worker"}:
        raise RuntimeError("验收集群节点不匹配")
    kube(
        "taint",
        "nodes",
        f"{CLUSTER}-control-plane",
        "node-role.kubernetes.io/control-plane:NoSchedule-",
    )
    with TemporaryDirectory() as directory:
        build = Path(directory)
        tls = certificates(build)
        shutil.copyfile(args.weed, build / "weed")
        (build / "Dockerfile").write_text(
            "ARG APP_IMAGE\nFROM ${APP_IMAGE}\nCOPY ca.pem /opt/fixture-ca.pem\n"
            "COPY --chmod=755 weed /usr/local/bin/weed\n",
            encoding="utf-8",
        )
        command(
            "docker",
            "build",
            "--network",
            "none",
            "--pull=false",
            "--build-arg",
            "APP_IMAGE=" + args.app_image,
            "-t",
            IMAGE,
            directory,
        )
        # 只归档已缓存的 amd64 层，避免容器运行时尝试导入缺失的其他平台。
        for image in (IMAGE, "postgres:17-alpine"):
            archive = build / "fixture.tar"
            command(
                "docker",
                "image",
                "save",
                "--platform",
                "linux/amd64",
                "-o",
                str(archive),
                image,
            )
            command(args.kind, "load", "image-archive", str(archive), "--name", CLUSTER)
            archive.unlink()
    buyer, approver = token_urlsafe(24), token_urlsafe(24)
    provision(kube, tls, buyer, approver)
    chart = ROOT / "deploy/helm/procurepilot"
    command(
        args.helm,
        "install",
        "pilot",
        str(chart),
        "--kubeconfig",
        args.kubeconfig,
        "-n",
        NS,
        "-f",
        str(chart / "values-multi-node.yaml"),
        "-f",
        str(chart / "values-s3.yaml"),
        "--set",
        "image.repository=procurepilot/ha-fixture",
        "--set",
        "image.tag=local",
        "--set",
        "image.pullPolicy=Never",
    )
    placed = {}
    for role in ("api", "worker"):
        kube(
            "-n", NS, "rollout", "status", f"deployment/pilot-{role}", "--timeout=110s"
        )
        selector = (
            f"app.kubernetes.io/instance=pilot,app.kubernetes.io/component={role}"
        )
        pods = json.loads(kube("-n", NS, "get", "pods", "-l", selector, "-o", "json"))[
            "items"
        ]
        assert len(pods) == 2 and {pod["spec"]["nodeName"] for pod in pods} == nodes
        placed[role] = {
            pod["metadata"]["name"]: pod["spec"]["nodeName"] for pod in pods
        }
    pdbs = json.loads(kube("-n", NS, "get", "pdb", "-o", "json"))["items"]
    assert len(pdbs) == 2 and all(item["spec"]["minAvailable"] == 1 for item in pdbs)
    with ExitStack() as stack:
        live_name, replaced_name = placed["api"]
        first, base = stack.enter_context(forward(kube_args, live_name))
        second, _ = stack.enter_context(forward(kube_args, replaced_name))
        fixture = procurement(first, second, buyer, approver)
        worker = verify_worker(base, buyer, fixture[0])
        replacement = verify_api_replacement(
            kube, kube_args, (live_name, replaced_name), first, buyer, fixture
        )
    report = {
        "passed": True,
        "nodes": sorted(nodes),
        "pods": placed,
        "pdbs": len(pdbs),
        "s3_https_cross_node": True,
        "idempotent_orders": 1,
        "inventory_decrement": 1,
        "outbox_events": 1,
        "worker": worker,
        "api_replacement": replacement,
        "app_image": args.app_image,
        "fixture_model": True,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        "Two-node Helm, HTTPS S3, concurrent procurement and external worker verified"
    )


if __name__ == "__main__":
    main()
