# Data Center Deployment Flow (Infrahub)

Use this prompt when a user asks to stand up a data center using Infrahub data and sample datasets in `.dev/data`. Keep interactions concise and avoid guessing values.

## Goals

- Collect the minimum required inputs to create a data center deployment.
- Offer sensible defaults from existing bootstrap/demo data.
- Create a dedicated branch and apply the deployment there.
- Avoid hallucinating: use tool-derived options; if missing, ask.

## Dataset References

- Bootstrap patterns (ready to propose):
  - PHYSICAL DC CISCO S WITH BORDER LEAFS
  - PHYSICAL DC ARISTA L3 CLOSET
  - SIMPLIFIED LAB FABRIC
- DC design patterns (TopologyDataCenterDesign from .dev/data/bootstrap/07_dc_templates.yml): S-Standard, S-Hierarchical, S-Flat; M-Standard, M-Hierarchical, M-Flat; L-Standard, L-Hierarchical, L-Flat; XL-Standard, XL-Hierarchical, XL-Flat (use these exact names and their max limits when confirming size/scale).
- Fabric building blocks (TopologyElement from .dev/data/bootstrap/14_design_elements.yml): roles leaf/tor/border-leaf/firewall/load-balancer/oob/console with device templates for Cisco, Arista, Dell, Edgecore and quantity presets (2/4/8 as listed). Use these as the allowed fabric_templates entries when mapping racks; quantity is required per schema.
- Addressing/ASN pools (used by helper defaults): Management-IPv4 (172.16.0.0/18), Customer-IPv4 (10.0.0.0/17), Technical-IPv4 (1.0.0.0/16), Loopback-IPv4 (1.127.0.0/16), Global-DC-IPv6 (fd00:1000::/32), Technical-IPv6 (fd00:2100::/40), Loopback-IPv6 (fd00:2000::/32); private ASN pools PRIVATE-ASN4 (64512-65534) and PRIVATE-ASN32 (4200000000-4294967294).
- Demo seed options (offer these exact names): dc1, dc2, dc3, dc4, dc5, dc6
  - Seed content notes (no guessing):
  - Each seed contains `00_topology.yml` (fabric, pods, spines/leafs), `01_suites.yml` (suites/rows), and `02_racks.yml` (rack layout/types per row). Use these file names when describing what will be loaded.

## Interaction Script

1) **Phase 1 — Required inputs (collect all 5 + optional branch)**
   - Ask exactly for: site_name, metro_location (e.g., BER-1, FRA-1, LON-1, MUC-1), design (choose from TopologyDataCenterDesign list above: S/M/L/XL variants), strategy (ebgp-evpn | isis-ibgp | ospf-ibgp | ebgp-ibgp), provider (free-form; suggest Internal | Lab Environment | Technology Partner).
   - Do not ask for management/customer/technical subnets; addressing is allocated from bootstrap pools automatically.
   - Optional: branch_name (if omitted, helper auto-generates `dc-deploy-<site>-<timestamp>`).
   - Offer defaults: strategy → `ebgp-ibgp` (schema default; if user wants EVPN use `ebgp-evpn`), provider → `Internal`, design → `M-Standard` (or another from the S/M/L/XL list if they hint at size).
   - Sorting defaults from schema: fabric/spine/leaf interface sorting → `bottom_up` unless user requests `top_down`.
   - Ask for branch name (suggest `dc-<site>-<suffix>`); confirm git sync (default false).
   - Example inputs to propose when user is unsure:
      - site_name: DC-BER-1
      - metro_location: BERLIN
      - design: M-Standard
      - strategy: ebgp-evpn
      - provider: Technology Partner
      - demo seed: dc1 (optional)
      - branch: dc-berlin-1 (sync_with_git: false)
2) **Phase 2 — Pod planning (iterate per pod)**
   - Ask for pod count and per-pod details: deployment_type (middle_rack/tor/mixed), spine_template, amount_of_spines (schema max 4, default 4), number_of_rows (default 1, max 20), maximum_leafs_per_row, maximum_tors_per_row.
   - Map pod/DC scale to a TopologyDataCenterDesign size (S/M/L/XL variants) when helpful; echo which name matches their requested max pods/spines/switches.
3) **Phase 3 — Rack planning (per pod → per row)**
   - For each pod row, ask: rack count and rack_type (compute/storage/tor/network as per schema), then the fabric_templates list. Keep fabric_templates limited to the TopologyElement options above (roles leaf/tor/border-leaf/firewall/load-balancer/oob/console with quantity presets 2/4/8).
   - Capture suite/row placement: number of suites, rows per suite, rack counts per row, and any row-specific rack_type overrides. Confirm any deviations (e.g., high-density rows).
4) **Confirm plan**
   - Echo collected values and which tools will run: `branch_create`, then `create_datacenter_deployment` (and optional seed step if supported externally).
   - Proceed only after user confirms.
5) **Execute**
   - Call `branch_create` with chosen name, `sync_with_git` per user.
   - Call `create_datacenter_deployment` with collected params (branch_name set to the created branch).
6) **Seed demo data (optional)**
    - If user opts for a demo seed (dc1–dc6):
       - Describe contents explicitly: `00_topology.yml` (pods/super-spines/spines/leafs and connectivity), `01_suites.yml` (suites and rows), `02_racks.yml` (rack rows, rack type per row, rack count).
       - Apply the seed to the chosen branch by loading those three files in order: topology first (`00_topology.yml`), suites (`01_suites.yml`), then racks (`02_racks.yml`). Mention that these live under `.dev/data/demos/01_data_center/<choice>/`.
       - Inline content for dc1 (use as a reference template; adjust only if the user selects a different seed):

            `00_topology.yml`

            ```yaml
            apiVersion: infrahub.app/v1
            kind: Object
            spec:
               kind: TopologyDataCenter
               data:
                  - name: DC1
                     index: 1
                     amount_of_super_spines: 2
                     super_spine_template: N9K_C9336C_FX2_SUPER_SPINE
                     fabric_interface_sorting_method: top_down
                     design_pattern: L-Hierarchical
                     member_of_groups:
                        - topologies_dc
                     children:
                        kind: TopologyPod
                        data:
                           - index: 1
                              deployment_type: middle_rack
                              spine_template: N9K-C9364C-GX_SPINE
                              amount_of_spines: 3
                              number_of_rows: 4
                              maximum_leafs_per_row: 4
                              maximum_tors_per_row: 4
                           - index: 2
                              deployment_type: mixed
                              spine_template: N9K-C9364C-GX_SPINE
                              amount_of_spines: 3
                              number_of_rows: 4
                              maximum_leafs_per_row: 4
                              maximum_tors_per_row: 10
                           - index: 3
                              deployment_type: tor
                              spine_template: N9K-C9364C-GX_SPINE
                              amount_of_spines: 2
                              number_of_rows: 2
                              maximum_leafs_per_row: 0
                              maximum_tors_per_row: 12
            ```

            `01_suites.yml`

            ```yaml
            apiVersion: infrahub.app/v1
            kind: Object
            spec:
               kind: LocationSuite
               data:
                  - suite_name: "Suite-1"
                     shortname: "muc-1-s-1"
                     parent: muc-1
                  - suite_name: "Suite-2"
                     shortname: "muc-1-s-2"
                     parent: muc-1
                  - suite_name: "Suite-3"
                     shortname: "muc-1-s-3"
                     parent: muc-1
            ```

            `02_racks.yml` (abbreviated sample; full file lists all racks for pods/rows)

            ```yaml
            apiVersion: infrahub.app/v1
            kind: Object
            spec:
               kind: LocationRack
               data:
                  - index: 5
                     row_index: 1
                     rack_type: network
                     shortname: muc-1-s-1-r-1-1
                     pod: DC1-1-POD-1
                     parent: muc-1-s-1
                     fabric_templates:
                        - ["2", "leaf", "N9K_C9336C_FX2_LEAF_MR"]
                        - ["2", "tor", "N9K_C9336C_FX2_TOR"]
                  - index: 5
                     row_index: 1
                     rack_type: network
                     shortname: muc-1-s-2-r-1-5
                     pod: DC1-1-POD-2
                     parent: muc-1-s-2
                     fabric_templates:
                        - ["2", "leaf", "N9K_C9336C_FX2_LEAF_MIXED"]
                  - index: 1
                     row_index: 1
                     rack_type: tor
                     shortname: muc-1-s-2-r-1-1
                     pod: DC1-1-POD-2
                     parent: muc-1-s-2
                     fabric_templates:
                        - ["2", "tor", "N9K_C9336C_FX2_TOR"]
                  - index: 1
                     row_index: 1
                     rack_type: tor
                     shortname: muc-1-s-3-r-1-1
                     pod: DC1-1-POD-3
                     parent: muc-1-s-3
                     fabric_templates:
                        - ["2", "tor", "N9K_C9336C_FX2_TOR"]
                  # ... additional racks cover remaining rows and pods (see file for full list)
            ```

    - If user opts for the baric deployment (same file layout; load in order topology → suites → racks):

       `00_topology.yml`

       ```yaml
       apiVersion: infrahub.app/v1
       kind: Object
       spec:
          kind: TopologyDataCenter
          data:
             - name: DC1
                index: 1
                amount_of_super_spines: 2
                super_spine_template: N9K_C9336C_FX2_SUPER_SPINE
                fabric_interface_sorting_method: top_down
                design_pattern: L-Hierarchical
                member_of_groups:
                   - topologies_dc
                children:
                   kind: TopologyPod
                   data:
                      - index: 1
                         deployment_type: middle_rack
                         spine_template: N9K-C9364C-GX_SPINE
                         amount_of_spines: 3
                         number_of_rows: 4
                         maximum_leafs_per_row: 4
                         maximum_tors_per_row: 4
                         member_of_groups:
                            - topologies_pod
                      - index: 2
                         deployment_type: mixed
                         spine_template: N9K-C9364C-GX_SPINE
                         amount_of_spines: 3
                         number_of_rows: 4
                         maximum_leafs_per_row: 4
                         maximum_tors_per_row: 10
                         member_of_groups:
                            - topologies_pod
                      - index: 3
                         deployment_type: tor
                         spine_template: N9K-C9364C-GX_SPINE
                         amount_of_spines: 2
                         number_of_rows: 2
                         maximum_leafs_per_row: 0
                         maximum_tors_per_row: 12
                         member_of_groups:
                            - topologies_pod
       ```

       `01_suites.yml`

       ```yaml
       apiVersion: infrahub.app/v1
       kind: Object
       spec:
          kind: LocationSuite
          data:
             - suite_name: "Suite-1"
                shortname: "muc-1-s-1"
                parent: muc-1
             - suite_name: "Suite-2"
                shortname: "muc-1-s-2"
                parent: muc-1
             - suite_name: "Suite-3"
                shortname: "muc-1-s-3"
                parent: muc-1
       ```

       `02_racks.yml` (abridged; load full file for complete racks across pods/rows)

       ```yaml
       apiVersion: infrahub.app/v1
       kind: Object
       spec:
          kind: LocationRack
          data:
             - index: 5
                row_index: 1
                shortname: muc-1-s-1-r-1-1
                rack_type: network
                pod: DC1-1-POD-1
                parent: muc-1-s-1
                fabric_templates:
                   - ["2", "leaf", "N9K_C9336C_FX2_LEAF_MR"]
                   - ["2", "tor", "N9K_C9336C_FX2_TOR"]
             - index: 5
                row_index: 2
                rack_type: network
                shortname: muc-1-s-1-r-2-1
                pod: DC1-1-POD-1
                parent: muc-1-s-1
                fabric_templates:
                   - ["4", "leaf", "N9K_C9336C_FX2_LEAF_MR"]
                   - ["4", "tor", "N9K_C9336C_FX2_TOR"]
             - index: 1
                row_index: 1
                rack_type: tor
                shortname: muc-1-s-2-r-1-1
                pod: DC1-1-POD-2
                parent: muc-1-s-2
                fabric_templates:
                   - ["2", "tor", "N9K_C9336C_FX2_TOR"]
             - index: 1
                row_index: 1
                rack_type: tor
                shortname: muc-1-s-3-r-1-1
                pod: DC1-1-POD-3
                parent: muc-1-s-3
                fabric_templates:
                   - ["2", "tor", "N9K_C9336C_FX2_TOR"]
             # ... full file includes all racks for pods 1–3 and rows 1–4/2; load entire document.
       ```

7) **Summarize**

   - Report branch name, deployment status, and any follow-ups (e.g., pending manual seed). Avoid embellishment.

## Guardrails

- If any required value is missing, ask; do not invent.
- Keep branch and deployment on the same branch value.
- When unsure of available designs/providers, say so and fall back to the suggested defaults.
- Keep outputs short: status + next steps.
