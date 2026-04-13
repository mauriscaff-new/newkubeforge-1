# Parte 3 - Kubernetes: Conceitos Fundamentais

## Visao geral

Docker resolve o empacotamento. Kubernetes resolve operacao em escala:

- disponibilidade;
- auto-healing;
- escalonamento horizontal;
- declaracao de estado desejado.

## Arquitetura de cluster

- Control Plane: API Server, Scheduler, Controller Manager e etcd.
- Nodes: executam Pods e sao gerenciados por kubelet.

Tudo passa pelo API Server; o estado e persistido em etcd.

## Objetos essenciais usados no KubeForge

### Pod e Deployment

- Pod e a menor unidade de execucao.
- Deployment garante replicas e rollout.
- No projeto:
  - `k8s/deployment.yaml` define `replicas: 2`;
  - probes HTTP usam `/livez` e `/readyz`.

### Service

- `k8s/service.yaml` usa `ClusterIP`;
- expoe porta 80 para o container na 8000.

### ConfigMap e Secret

- `k8s/configmap.yaml`: variaveis nao sensiveis.
- `k8s/secret.yaml`: `KUBEFORGE_API_KEY`.

### HPA

- `k8s/hpa.yaml`:
  - `minReplicas: 2`
  - `maxReplicas: 6`
  - alvo de CPU media de 70%.

Requer Metrics Server ativo no cluster.

### NetworkPolicy

- `k8s/networkpolicy.yaml` restringe:
  - ingresso no app via namespace do ingress;
  - egresso do app para Redis e DNS;
  - ingresso no Redis apenas pelo app.

### Kustomize

`k8s/kustomization.yaml` centraliza aplicacao dos manifests:

```bash
kubectl diff -k k8s
kubectl apply -k k8s
```

## Loop de reconciliacao

Voce declara YAML (estado desejado) e o Kubernetes corrige divergencias continuamente
(por exemplo, recriando Pods apos falha de node).
