# Kubernetes Integration Testing

makethlm does not install Kubernetes tooling automatically. For local testing,
install:

- `kubectl`
- one local cluster provider: `minikube`, `kind`, or Docker Desktop Kubernetes

Install those tools using their official, platform-specific instructions:

- [Install kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Install minikube](https://minikube.sigs.k8s.io/docs/start/)

Prefer a pinned release and follow the vendor's checksum/signature verification
steps before installing a downloaded executable with elevated privileges.

Once both tools are installed:

```bash
minikube start
kubectl cluster-info
```

Then run:

```bash
makethlm -f examples/kubernetes/Promptfile context
makethlm -f examples/kubernetes/Promptfile diff
```

Use a disposable namespace for write tests:

```bash
kubectl create ns makethlm-test
makethlm -f examples/kubernetes/Promptfile -V namespace=makethlm-test apply
kubectl delete ns makethlm-test
```
