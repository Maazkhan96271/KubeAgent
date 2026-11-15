# KubeAgent on Vultr Kubernetes Engine (VKE): deployment guide

This guide covers a production install of KubeAgent on a VKE cluster, both with
plain `kubectl` against the manifests in `manifests/` and with the bundled Helm
chart in `manifests/helm/kubeagent`.

KubeAgent is one container serving four things from port `8000`:

| Path | Purpose |
| --- | --- |
| `/` | Mission Control dashboard (SSE live stream) |
| `/api/v1/...` | Incident webhook ingest, analyze/approve/resolve, SSE stream |
| `/api/v1/incidents/{id}/postmortem` | Markdown or `?format=html` RCA report |
| `/healthz` | Liveness and readiness probe |

> 🟢 **Active Live Deployment:**
> * **Public Load Balancer IP:** [http://139.84.220.82](http://139.84.220.82)
> * **Cluster Name:** `kubeagent-cluster`
> * **Docker Image:** `maazkhan5/kubeagent:v1`

## 1. Prerequisites

- A VKE cluster running Kubernetes **1.28+** (control plane is managed by
  Vultr, including the cloud controller manager and CSI driver).
- `kubectl` pointed at the cluster (`vultr-cli kubernetes config <cluster-id>`,
  or download the kubeconfig from the Vultr dashboard).
- `helm` 3.12+ if you use the chart path.
- Permission to create cluster-scoped RBAC (the agent needs a `ClusterRole`).
- An image registry you can push to (GHCR, Docker Hub, Vultr Container
  Registry).

Verify the cluster:

```bash
kubectl version
kubectl get nodes -o wide          # nodes should be Ready
kubectl get pods -n kube-system    # vultr-cloud-controller-manager + csi present
```

## 2. Build and publish the image

The repository `Dockerfile` is a multi-stage build that runs as UID/GID `10001`
with a read-only-friendly layout:

```bash
export IMAGE=ghcr.io/<your-org>/kubeagent:0.1.0

docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
```

`--platform linux/amd64` matters if you build on an Apple Silicon machine:
VKE worker nodes are x86_64.

If the registry is private, create a pull secret and reference it (the default
manifests assume a public or pre-authenticated image):

```bash
kubectl -n kubeagent create secret docker-registry ghcr-auth \
  --docker-server=ghcr.io \
  --docker-username=<user> \
  --docker-password=<token>
```

Then add to the Deployment pod spec / chart values:

```yaml
imagePullSecrets:
  - name: ghcr-auth
```

Update `image:` in `manifests/03-deployment.yaml` (or `image.repository` /
`image.tag` for Helm) to the tag you pushed.

## 3. Install with plain manifests (kubectl)

Apply in numeric order; the namespace must exist before the namespaced objects.

```bash
kubectl apply -f manifests/01-rbac.yaml          # namespace, ServiceAccount, RBAC
kubectl apply -f manifests/02-configmap.yaml     # ConfigMap + Secret placeholder
kubectl apply -f manifests/03-deployment.yaml
kubectl apply -f manifests/04-service.yaml       # ClusterIP + Vultr LoadBalancer
kubectl rollout status deployment/kubeagent -n kubeagent --timeout=180s
```

Or apply the whole directory at once (order is resolved by `kubectl`):

```bash
kubectl apply -f manifests/
```

### Set the LLM key

`02-configmap.yaml` ships `LLM_API_KEY: "REPLACE_WITH_YOUR_LLM_API_KEY"`. Never
commit a real key. Replace it out-of-band:

```bash
kubectl -n kubeagent create secret generic kubeagent-secrets \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n kubeagent rollout restart deployment/kubeagent
```

For production, prefer External Secrets Operator or Sealed Secrets so the key
never lands in a manifest or in shell history.

The key is referenced with `optional: true`, so the Pod still starts without
it: the agent falls back to rule-based reasoning instead of the LLM.

### Edit the cluster-specific values

Before applying, change at least:

- `CLUSTER_NAME` in `02-configmap.yaml` (`vke-prod` by default).
- `CORS_ORIGINS` to the hostname the dashboard is served from.
- `WATCH_NAMESPACES` to the namespaces the agent may inspect.
- `image:` in `03-deployment.yaml`.

## 4. Install with Helm

The chart lives in `manifests/helm/kubeagent` and produces the same objects as
the raw manifests.

```bash
helm upgrade --install kubeagent manifests/helm/kubeagent \
  --namespace kubeagent --create-namespace \
  --set image.repository=ghcr.io/<your-org>/kubeagent \
  --set image.tag=0.1.0 \
  --set config.CLUSTER_NAME=vke-prod \
  --set config.CORS_ORIGINS=https://kubeagent.example.com \
  --set config.WATCH_NAMESPACES=default,kubeagent \
  --set secrets.llmApiKey="$LLM_API_KEY" \
  --wait --timeout 5m
```

Notes:

- `--create-namespace` substitutes for the `Namespace` object that the raw
  manifests create. The chart does not render a Namespace.
- Prefer a values file over `--set` for a key, so it stays out of shell history:
  `--set secrets.llmApiKey="$(cat ./llm-key)"`.
- To keep the Secret outside Helm, leave `secrets.llmApiKey` empty and set
  `secrets.existingSecret` to a Secret you manage (`kubeagent-secrets` by
  default). The chart then skips creating one.
- The chart enables the Vultr `LoadBalancer` Service by default. Disable it with
  `--set loadBalancer.enabled=false` when using Ingress instead.
- Enable the fault injector for a demo with `--set chaosDemo.enabled=true`.

Useful values:

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository` / `image.tag` | `ghcr.io/maazkhan96271/kubeagent` / `0.1.0` | Image to deploy |
| `replicaCount` | `1` | Keep at 1: incident state is in-process |
| `config.SIMULATION_MODE` | `"false"` | `true` disables all real cluster calls |
| `config.AUTO_REMEDIATE` | `"false"` | Remediation still needs `/approve` |
| `config.WATCH_NAMESPACES` | `default,kubeagent` | Namespaces collected |
| `secrets.existingSecret` | `kubeagent-secrets` | Secret holding `LLM_API_KEY` |
| `loadBalancer.enabled` | `true` | Vultr managed LB Service |
| `ingress.enabled` | `false` | nginx Ingress alternative |
| `chaosDemo.enabled` | `false` | Faulty memory-hog Pod |

Render and inspect before installing:

```bash
helm lint manifests/helm/kubeagent
helm template kubeagent manifests/helm/kubeagent --namespace kubeagent \
  --set secrets.llmApiKey=dummy > /tmp/kubeagent-rendered.yaml
kubectl apply --dry-run=server -f /tmp/kubeagent-rendered.yaml
```

## 5. Verify the rollout

```bash
kubectl -n kubeagent get pods -l app.kubernetes.io/name=kubeagent
kubectl -n kubeagent logs deploy/kubeagent --tail=50
kubectl -n kubeagent get svc kubeagent-lb -w    # wait for EXTERNAL-IP
```

Port-forward for a private check before exposing anything:

```bash
kubectl -n kubeagent port-forward svc/kubeagent 8000:80
curl -s http://127.0.0.1:8000/healthz | jq
curl -s http://127.0.0.1:8000/api/v1 | jq
```

`/healthz` should report `"simulation_mode": false`, `"environment":
"production"`, and `"llm_configured": true` once the key is in place.

Open the dashboard:

```bash
kubectl -n kubeagent get svc kubeagent-lb \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'
# then browse to http://<EXTERNAL-IP>/
```

Expected probe/security posture:

```bash
kubectl -n kubeagent get pod -l app.kubernetes.io/name=kubeagent \
  -o jsonpath='{.items[0].spec.containers[0].securityContext}{"\n"}'
# {"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true}
```

## 6. Expose it properly

### Option A: Vultr managed Load Balancer (default)

The `kubeagent-lb` Service carries `service.beta.kubernetes.io/vultr-loadbalancer-*`
annotations; VKE's CCM creates a Vultr Load Balancer and fills in
`EXTERNAL-IP`. Supported annotation suffixes include `protocol`,
`backend-protocol`, `https-ports`, and `ssl` (the name of a certificate
uploaded in the Vultr console):

```yaml
metadata:
  annotations:
    service.beta.kubernetes.io/vultr-loadbalancer-protocol: "https"
    service.beta.kubernetes.io/vultr-loadbalancer-https-ports: "443"
    service.beta.kubernetes.io/vultr-loadbalancer-ssl: "kubeagent-example-com"
```

Do not edit the Vultr Load Balancer in the console: the CCM reconciles it back
to whatever the Service says.

The API has no authentication layer of its own. In production, restrict the
Load Balancer or put it behind a WAF/VPN; anyone who can reach `/api/v1` can
trigger analyses and approvals.

### Option B: nginx-ingress + cert-manager

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace

helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
  --set crds.enabled=true
```

Disable the LoadBalancer Service, then either uncomment the Ingress block in
`manifests/04-service.yaml` or install the chart with:

```bash
helm upgrade --install kubeagent manifests/helm/kubeagent -n kubeagent \
  --set loadBalancer.enabled=false \
  --set ingress.enabled=true \
  --set ingress.host=kubeagent.example.com \
  --set ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt-prod
```

The Ingress annotations set `proxy-read-timeout: "3600"` and
`proxy-buffering: "off"`. Both are required for the SSE stream at
`/api/v1/incidents/{id}/stream`; without them nginx buffers events and the
dashboard appears frozen.

## 7. Run a live chaos demo

```bash
kubectl apply -f manifests/chaos-demo-pod.yaml
kubectl -n default get pod kubeagent-chaos-memory-hog -w
```

The Pod prints `held=... MiB` every second, is OOMKilled (exit code 137) at its
128Mi limit, and restarts, producing the OOMKilled/CrashLoopBackOff signature
the collectors recognise.

Trigger the workflow against the running service:

```bash
BASE=http://<EXTERNAL-IP>
curl -s -X POST "$BASE/api/v1/incidents/simulate" \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"OOMKilled"}' | jq '.incident.id'
```

Then open the dashboard, watch the pipeline progress through inspection, and
approve remediation. Once the incident is `RESOLVED`, the **View Post-Mortem
Report** button appears; the same report is available as Markdown or HTML:

```bash
curl -s "$BASE/api/v1/incidents/<incident-id>/postmortem"
curl -s "$BASE/api/v1/incidents/<incident-id>/postmortem?format=html" -o report.html
```

Clean up the fault injector:

```bash
kubectl delete -f manifests/chaos-demo-pod.yaml
```

## 8. Production hardening checklist

- **RBAC**: `kubeagent-reader-remediator` can read pods/nodes/events and
  patch Deployments and delete Pods. It cannot read Secrets or exec. If you
  want a strictly read-only agent, remove the `delete` rule on pods and the
  `patch`/`update` rule on deployments, set `AUTO_REMEDIATE=false`, and treat
  remediation as human-run commands.
- **Namespace scope**: `WATCH_NAMESPACES` limits collection, but the
  `ClusterRole` is cluster-wide. For per-tenant isolation, replace it with
  RoleBindings in the watched namespaces.
- **Pod Security**: the namespace enforces `restricted`; the Deployment runs
  non-root UID 10001, drops all capabilities, uses `RuntimeDefault` seccomp and
  a read-only root filesystem with writable `/tmp`.
- **Secrets**: keep `LLM_API_KEY` out of Git. Rotate by patching the Secret and
  restarting the Deployment.
- **State**: incidents live in memory (last 50) and are lost on restart. Do not
  scale beyond one replica without replacing the in-process store.
- **Ingress protection**: add authentication at the edge (WAF, mTLS, VPN) since
  the API exposes incident approval.
- **Monitoring**: scrape `/healthz` from an uptime checker; the LoadBalancer
  health check uses it implicitly through Service endpoints.
- **Resources**: defaults are 100m/192Mi requests and 500m/512Mi limits. Raise
  the memory limit if you increase the incident store or run large log
  analysis.
- **Logs**: `kubectl logs` only. Ship container logs to your aggregator if you
  need retention beyond the node's log rotation.

## 9. Upgrade and rollback

```bash
# kubectl
kubectl apply -f manifests/03-deployment.yaml
kubectl rollout status deployment/kubeagent -n kubeagent
kubectl rollout history deployment/kubeagent -n kubeagent
kubectl rollout undo deployment/kubeagent -n kubeagent

# Helm
helm upgrade kubeagent manifests/helm/kubeagent -n kubeagent --set image.tag=0.2.0
helm rollback kubeagent 1 -n kubeagent
helm history kubeagent -n kubeagent
```

The Deployment uses `maxUnavailable: 0` / `maxSurge: 1` with HTTP readiness
probes, so a failing new revision never replaces a healthy Pod.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `CrashLoopBackOff`, `403 Forbidden` in logs | ServiceAccount token or RBAC missing | Re-apply `01-rbac.yaml`; check the Pod uses `serviceAccountName: kubeagent-sa` |
| `EXTERNAL-IP` stays `<pending>` | VKE CCM not running / quota | `kubectl -n kube-system logs deploy/vultr-cloud-controller-manager` |
| Readiness probe fails | App not listening on 8000 | `kubectl logs`; confirm `PORT` was not changed while the probe still targets `http` |
| `Read-only file system` errors | Writing outside `/tmp` | Mount another `emptyDir` or write to `/tmp` |
| Dashboard loads but events never stream | Proxy buffering (Ingress path) | Set `proxy-buffering: off` and a long read timeout |
| `llm_configured: false` on `/healthz` | Secret key missing | Patch `kubeagent-secrets` and restart the Deployment |
| Post-mortem returns 500 | `markdown` package missing in the image | Rebuild from `requirements.txt` (it is pinned there) |
| Pods evicted / OOMKilled | Limit too low for log analysis | Raise `resources.limits.memory` |

## 11. Uninstall

```bash
kubectl delete -f manifests/chaos-demo-pod.yaml   # if applied
kubectl delete -f manifests/04-service.yaml -f manifests/03-deployment.yaml
kubectl delete -f manifests/02-configmap.yaml -f manifests/01-rbac.yaml
# Helm
helm uninstall kubeagent -n kubeagent
```

Deleting the namespace also removes the Pods, ConfigMap and Secret, but not the
cluster-scoped `ClusterRole`/`ClusterRoleBinding`; the commands above remove
them explicitly.
