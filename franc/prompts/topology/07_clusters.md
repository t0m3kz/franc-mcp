# Cluster Topology — Diagram Layout Rules

## Infrahub schema

Compute clusters follow a two-level hierarchy:

- `VirtCluster` — the cluster itself
  - `name` — cluster identifier
  - `platform` — `kubernetes` | `eks` | `aks` | `vmware_vsphere` | `nutanix`
  - `version` — platform version
  - `ha_enabled` — whether HA is active
  - `status` — `active` | `maintenance` | `decommissioned`
  - `deployment` → `TopologyDeployment` — where the cluster lives (DC pod, colo zone, cloud region)
  - `node_pools` → list of `VirtNodePool`

- `VirtNodePool` — a pool of nodes within the cluster
  - `pool_type` — `control_plane` | `worker` | `hypervisor` | `storage` | `management`
  - `node_count` — static count (or null if autoscaling)
  - `instance_flavor` — VM size / hardware profile
  - `scaling_mode` — `static` | `auto`
  - `min_size` / `max_size` — autoscaling bounds (when `scaling_mode: auto`)
  - `os_type` — operating system
  - `cpu_per_node` / `memory_per_node_gb` — per-node specs

`VirtCluster` also exposes a generic `cluster_capabilities` relationship (peer
`ManagedGenericCluster`) for attaching `Managed*` capability nodes (e.g. HA domains),
but no CNI/storage-backend/ingress-controller node types exist in the current schema.

## Queries

```graphql
# All clusters
query {
  VirtCluster(limit: 50) {
    edges { node {
      name { value }
      platform { value }
      version { value }
      ha_enabled { value }
      status { value }
      deployment { node { name { value } } }
    } }
  }
}

# Node pools for a specific cluster
query {
  VirtNodePool(cluster__name__value: "MY-CLUSTER-01") {
    edges { node {
      name { value }
      pool_type { value }
      node_count { value }
      instance_flavor { value }
      scaling_mode { value }
      min_size { value }
      max_size { value }
      cpu_per_node { value }
      memory_per_node_gb { value }
    } }
  }
}

```

## Object loader example

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: VirtCluster
  data:
    - name: DC1-K8S-PROD
      platform: kubernetes
      version: "1.31"
      ha_enabled: true
      status: active
      deployment: DC1-1-POD-1
```

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: VirtNodePool
  data:
    - name: DC1-K8S-PROD-CTRL
      pool_type: control_plane
      node_count: 3
      scaling_mode: static
      cluster: DC1-K8S-PROD

    - name: DC1-K8S-PROD-WORKERS
      pool_type: worker
      scaling_mode: auto
      min_size: 2
      max_size: 10
      instance_flavor: m5.xlarge
      cluster: DC1-K8S-PROD
```

## Node pool types and their meaning

| pool_type | Meaning | Typical count |
|-----------|---------|---------------|
| `control_plane` | k8s API server / vCenter / Prism Central | 1-3 (odd for HA) |
| `worker` | k8s application workloads | 2-N (autoscaling) |
| `hypervisor` | ESXi hosts / AHV nodes | 4+ |
| `storage` | Ceph OSD / vSAN / Nutanix storage | 3-4 |
| `management` | NSX-T manager / Prism Central / monitoring | 1-3 |

## Platform-specific hierarchy

### Kubernetes (kubernetes / eks / aks)

```
Cluster
  ├── control_plane nodes   (3x HA — API server, etcd, scheduler)
  ├── worker nodes          (autoscaling or static workload nodes)
  └── storage nodes         (if Ceph/Rook-based persistent storage)
```

### VMware vSphere

```
Cluster
  ├── control_plane (1 node)  — vCenter management plane
  ├── hypervisor (4+ nodes)   — ESXi compute hosts
  ├── storage (3+ nodes)      — vSAN storage tier
  └── management (3 nodes)    — NSX-T network management
```

### Nutanix AHV

```
Cluster
  ├── management (3 nodes)    — Prism Central management plane
  ├── hypervisor (4+ nodes)   — AHV + CVM co-located compute/storage
  └── storage (3 nodes)       — dedicated storage if separate from hypervisor
```

## Draw.io layout for a single cluster (top → bottom)

```
Layer 0 — Cluster header box  (name, platform, version, HA status)
Layer 1 — control_plane pool  (dark header — management plane)
Layer 2 — worker / hypervisor (primary workload)
Layer 3 — storage pool        (storage nodes)
Layer 4 — management pool     (monitoring / orchestration)
```

## Draw.io layout for multi-cluster overview (side by side)

Each cluster = one swimlane column. Node pools = rows within the column.
Count shown as badge on pool box: `x4 nodes` or `2-8 (auto)`.

## Colour palette per platform

| Platform | Header colour |
|----------|--------------|
| kubernetes / eks / aks | `#326ce5` (k8s blue) |
| vmware_vsphere | `#717070` (VMware grey) |
| nutanix | `#024da1` (Nutanix blue) |

## Pool type colours (consistent across platforms)

| pool_type | Fill |
|-----------|------|
| control_plane | `#1a3a5c` dark blue |
| worker | `#2d6a8a` steel blue |
| hypervisor | `#1a5c3a` dark green |
| storage | `#7a5c1a` amber |
| management | `#4a148c` purple |
