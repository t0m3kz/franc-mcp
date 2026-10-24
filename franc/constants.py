NAMESPACES_INTERNAL = ["Internal", "Profile", "Template"]

# GCF compression threshold — lower = fewer tokens per response
GCF_AUTO_THRESHOLD_ITEMS = 5  # Auto-compress when result has >5 items

# Default page size for list tools (get_nodes); -1 means "return everything"
DEFAULT_PAGE_SIZE = 50

schema_attribute_type_mapping = {
    "Text": "String",
    "String": "String",
    "Integer": "Int",
    "Float": "Float",
    "Boolean": "Boolean",
    "DateTime": "DateTime",
}

# ---------------------------------------------------------------------------
# Schema kind constants
# ---------------------------------------------------------------------------
KIND_TOPOLOGY_DC = "TopologyDataCenter"
KIND_TOPOLOGY_POD = "TopologyPod"
KIND_TOPOLOGY_DC_DESIGN = "TopologyDataCenterDesign"
KIND_DEVICE = "DcimDevice"
KIND_VIRTUAL_DEVICE = "DcimVirtualDevice"
KIND_INTERFACE = "DcimInterface"
KIND_CABLE = "DcimCable"
KIND_NAMESPACE = "BuiltinIPNamespace"
KIND_PREFIX = "IpamPrefix"
KIND_BGP_PEERING = "ManagedBGPPeering"
KIND_VXLAN_SEGMENT = "ManagedVxlanSegment"
KIND_VLAN_SEGMENT = "ManagedVlanSegment"
KIND_APP_PORTFOLIO = "AppPortfolio"
KIND_APP_APPLICATION = "AppApplication"
KIND_APP_COMPONENT = "AppComponent"
KIND_CLOUD_VM = "CloudInstance"
KIND_CUSTOMER = "OrganizationCustomer"
KIND_CUSTOMER_DC = "TopologyCustomerDC"

# ---------------------------------------------------------------------------
# Cloud infrastructure kinds
# ---------------------------------------------------------------------------
KIND_CLOUD = "TopologyCloud"
KIND_CLOUD_REGION = "TopologyCloudRegion"
KIND_CLOUD_ZONE = "TopologyCloudZone"
KIND_CLOUD_ACCOUNT = "CloudAccount"
KIND_CLOUD_INSTANCE = "CloudInstance"
KIND_CLOUD_VPN = "CloudVirtualNetwork"
KIND_CLOUD_SEGMENT = "CloudNetworkSegment"
KIND_CLOUD_LB = "CloudLoadBalancer"
KIND_CLOUD_DX = "CloudDirectConnect"
KIND_CLOUD_ER = "CloudExpressRoute"
KIND_CLOUD_VIF = "CloudVirtualInterface"
KIND_VIRT_CLUSTER = "VirtCluster"
KIND_VIRT_NODE_POOL = "VirtNodePool"

# ---------------------------------------------------------------------------
# Colocation kinds
# ---------------------------------------------------------------------------
KIND_COLO = "TopologyColocation"
KIND_COLO_METRO = "TopologyColocationMetro"
KIND_COLO_ZONE = "TopologyColocationZone"
KIND_PHYS_CIRCUIT = "TopologyPhysicalCircuit"

# ---------------------------------------------------------------------------
# Office/branch kinds
# ---------------------------------------------------------------------------
KIND_OFFICE = "TopologyOffice"

# ---------------------------------------------------------------------------
# External connectivity kinds
# ---------------------------------------------------------------------------
KIND_EXTERNAL = "TopologyExternal"
KIND_EXTERNAL_PROVIDER = "TopologyExternalProviders"
KIND_EXT_PEER = "ManagedExternalPeer"

# ---------------------------------------------------------------------------
# Datacenter topology defaults
# ---------------------------------------------------------------------------
DEFAULT_STRATEGIES = ["ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"]
DEFAULT_DESIGNS = [
    "S_OSPF_IBGP",
    "S_EBGP_IBGP",
    "S_EBGP_EBGP",
    "M_OSPF_IBGP",
    "M_EBGP_EBGP",
    "M_EBGP_IBGP",
    "L_EBGP_EBGP",
    "XL_EBGP_EBGP",
]
DEFAULT_PROVIDERS = ["Technology Partner", "Customer 1", "Internal", "Lab Environment"]

# ---------------------------------------------------------------------------
# Customer boarding defaults
# ---------------------------------------------------------------------------
DEFAULT_FIREWALL_TYPES = ["CloudGuard", "FortiGate", "PAN-OS"]
DEFAULT_FIREWALL_PLATFORMS = ["checkpoint_gaia", "fortios", "panos"]
DEFAULT_ENVIRONMENTS = ["p", "n", "d"]  # production, non-prod, dev
DEFAULT_SEGMENT_ENVIRONMENTS = {
    "p": "production",
    "n": "non-production",
    "d": "development",
}

# ---------------------------------------------------------------------------
# Neo4j graph database connection
# ---------------------------------------------------------------------------
NEO4J_HTTP_URL = "http://localhost:7474"
NEO4J_BOLT_URL = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "admin"
NEO4J_TX_ENDPOINT = "/db/neo4j/tx/commit"
NEO4J_DEFAULT_MAX_HOPS = 15

# Connector types supported by BFS traversal
GRAPH_CONNECTOR_TYPES = ["cable", "physical_circuit", "virtual_circuit", "cloud_circuit"]

# ---------------------------------------------------------------------------
# Application deployment defaults
# ---------------------------------------------------------------------------
DEFAULT_APP_CRITICALITIES = ["critical", "high", "medium", "low"]
DEFAULT_APP_ENVIRONMENTS = ["p", "n", "d"]
