# Kubectl Guide — Running E2E Tests from the Dev Cluster

## Prerequisites

```bash
# Install Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Azure CLI and kubectl
brew install azure-cli kubectl
```

## Connect to the Dev Cluster

```bash
# Login to Azure
az login

# List AKS clusters to find the Dev cluster
az aks list --query "[].{name:name, resourceGroup:resourceGroup}" -o table

# Connect to the Dev cluster (replace with actual values)
az aks get-credentials --resource-group <resource-group> --name <cluster-name>

# Verify connection
kubectl cluster-info

# Verify you can see the terzo-squad namespace
kubectl get namespaces | grep terzo
```

## Explore the Cluster

```bash
# List all services in terzo-squad
kubectl get svc -n terzo-squad

# Find the auth service (check all namespaces)
kubectl get svc --all-namespaces | grep auth

# List running pods
kubectl get pods -n terzo-squad

# List all services across all namespaces (find internal URLs)
kubectl get svc --all-namespaces -o wide | grep -E "auth|document|ocr"

# Check ingress rules (may show internal routing)
kubectl get ingress --all-namespaces | grep auth

# Check DNS resolution from inside a pod
kubectl run tmp-debug --rm -it --image=busybox -n terzo-squad -- nslookup auth-service-dev.product-internal.terzocloud.com
```

## Run E2E Tests

```bash
# Delete any previous run
kubectl delete job nebula-e2e-tests -n terzo-squad --ignore-not-found

# Apply the job
kubectl apply -f k8s/e2e-job.yml

# Watch pod start (wait for Running status)
kubectl get pod -n terzo-squad -l app=nebula-e2e-tests -w

# Follow logs (Ctrl+C to stop following)
kubectl logs job/nebula-e2e-tests -n terzo-squad -f

# Save logs to file
kubectl logs job/nebula-e2e-tests -n terzo-squad > /tmp/e2e-logs.txt 2>&1
```

## Run Cleanup

```bash
kubectl delete job nebula-e2e-cleanup -n terzo-squad --ignore-not-found
kubectl apply -f k8s/e2e-cleanup-job.yml
kubectl logs job/nebula-e2e-cleanup -n terzo-squad -f
```

## Debug a Failed Run

```bash
# Check pod status and events
kubectl describe pod -n terzo-squad -l app=nebula-e2e-tests

# Check pod events for scheduling/pull issues
kubectl get events -n terzo-squad --sort-by='.lastTimestamp' | tail -20

# Get a shell into a debug pod (same namespace, test DNS/networking)
kubectl run tmp-debug --rm -it --image=python:3.14-slim -n terzo-squad -- bash

# Inside the debug pod:
#   pip install httpx
#   python -c "import httpx; print(httpx.get('https://mafia.terzocloud.com').status_code)"
#   python -c "import httpx; print(httpx.post('https://auth-service-dev.product-internal.terzocloud.com/auth/token', json={}).status_code)"
#   nslookup auth-service-dev.product-internal.terzocloud.com
```

## Find the Auth Service

The auth service may have a different internal name. Check:

```bash
# Search all namespaces for auth-related services
kubectl get svc --all-namespaces | grep -i auth

# Search for endpoints containing "auth"
kubectl get endpoints --all-namespaces | grep -i auth

# Check configmaps/secrets that might reference auth URLs
kubectl get configmap --all-namespaces -o json | grep -l "auth-service" 2>/dev/null

# Check if it's an ExternalName service or has an ingress
kubectl get svc --all-namespaces -o wide | grep -i auth
```

## Useful Commands

```bash
# List all jobs in namespace
kubectl get jobs -n terzo-squad

# Delete a completed job
kubectl delete job nebula-e2e-tests -n terzo-squad

# View pod resource usage
kubectl top pod -n terzo-squad -l app=nebula-e2e-tests

# Port-forward a service locally (useful for debugging)
kubectl port-forward svc/<service-name> 8080:80 -n terzo-squad
```
