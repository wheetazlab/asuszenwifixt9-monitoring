# k8s — Raw Kubernetes Manifests

These manifests deploy the exporter directly with `kubectl`, without Helm or Ansible.  
If you're using the Ansible role in [rpi5-talos-k8-ansible](https://github.com/wheetazlab/rpi5-talos-k8-ansible) you don't need these — the role generates everything automatically.

## Files

| File | Description |
|------|-------------|
| `namespace.yml` | `asus-monitoring` namespace with PSA `restricted` enforcement |
| `configmap.yml.template` | Non-sensitive runtime config (IPs, ports, log level) |
| `secret.yml.template` | SSH credentials — **never commit with real values** |
| `deployment.yml` | Single-replica exporter deployment (Recreate strategy) |
| `service.yml` | ClusterIP service exposing port 9100 |
| `servicemonitor.yml` | `monitoring.coreos.com/v1` ServiceMonitor for kube-prometheus-stack |

## Prerequisites

- Kubernetes cluster with `kube-prometheus-stack` installed (provides the `ServiceMonitor` CRD and Prometheus)
- SSH access enabled on your ASUS ZenWiFi XT9 router and extender (port 2222)

## Deploy

### 1. Namespace

```bash
kubectl apply -f k8s/namespace.yml
```

### 2. Credentials

Copy the template, fill in your SSH password, apply it, then delete the copy:

```bash
cp k8s/secret.yml.template k8s/secret.yml
# Edit SSH_PASSWORD in secret.yml
kubectl apply -f k8s/secret.yml
rm k8s/secret.yml
```

### 3. ConfigMap

Copy the template, set your router/extender IPs, apply it:

```bash
cp k8s/configmap.yml.template k8s/configmap.yml
# Edit ROUTER_SSH_HOST and EXTENDER_SSH_HOST in configmap.yml
kubectl apply -f k8s/configmap.yml
rm k8s/configmap.yml
```

### 4. Deployment, Service, and ServiceMonitor

```bash
kubectl apply -f k8s/deployment.yml
kubectl apply -f k8s/service.yml
kubectl apply -f k8s/servicemonitor.yml
```

### Verify

```bash
kubectl -n asus-monitoring get pods
kubectl -n asus-monitoring logs -l app=asus-router-exporter
```

The pod should reach `1/1 Running` within 30–60 seconds. Check `/metrics` directly:

```bash
kubectl -n asus-monitoring port-forward svc/asus-router-exporter 9100:9100
curl http://localhost:9100/metrics | grep asus_router
```

## Environment Variables

All configuration is provided via the ConfigMap and Secret.

| Variable | Source | Default | Description |
|----------|--------|---------|-------------|
| `ROUTER_SSH_HOST` | ConfigMap | `""` | Router IP address — **required** |
| `ROUTER_SSH_PORT` | ConfigMap | `2222` | Router SSH port |
| `EXTENDER_SSH_HOST` | ConfigMap | `""` | Extender IP address — **required** |
| `EXTENDER_SSH_PORT` | ConfigMap | `2222` | Extender SSH port |
| `METRICS_PORT` | ConfigMap | `9100` | Port the exporter listens on |
| `LOG_LEVEL` | ConfigMap | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `SSH_USERNAME` | Secret | — | SSH username (typically `router`) |
| `SSH_PASSWORD` | Secret | — | SSH password |

## Notes

- The ServiceMonitor uses label `release: kube-prometheus-stack` — if your release name differs, update the label before applying.
- The scrape interval is 60 s with a 55 s timeout to allow for the batched SSH calls.
- The deployment uses `strategy: Recreate` so there's never two pods trying to hold SSH connections simultaneously.
