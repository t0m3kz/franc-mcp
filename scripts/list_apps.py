#!/usr/bin/env python3
"""List all applications and their components/segments.

Usage:
    uv run scripts/list_apps.py
    uv run scripts/list_apps.py --customer c001
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from franc.traversal import _neo4j_run


def main(customer: str | None) -> None:
    rows = _neo4j_run("""
MATCH (app:AppApplication)-[:HAS_ATTRIBUTE]->(an)-[:HAS_VALUE]->(vn)
  WHERE an.name = "name"
MATCH (app)-[:HAS_ATTRIBUTE]->(al)-[:HAS_VALUE]->(vl) WHERE al.name = "label"
RETURN vn.value AS name, vl.value AS label ORDER BY vn.value
""")

    for r in rows:
        name = r["name"]
        if customer and not name.startswith(customer):
            continue
        print(f"{r['label']:30s}  ({name})")

    comp_rows = _neo4j_run("""
MATCH (comp:AppComponent)-[:HAS_ATTRIBUTE]->(an)-[:HAS_VALUE]->(vn)
  WHERE an.name = "name"
MATCH (comp)-[:HAS_ATTRIBUTE]->(aslug)-[:HAS_VALUE]->(vslug) WHERE aslug.name = "slug"
OPTIONAL MATCH (comp)-[:HAS_ATTRIBUTE]->(act)-[:HAS_VALUE]->(vct) WHERE act.name = "component_type"
OPTIONAL MATCH (comp)-[:IS_RELATED]->(r_seg:Relationship {name:"app_component__network_segment"})
OPTIONAL MATCH (seg)-[:IS_RELATED]->(r_seg)
OPTIONAL MATCH (seg)-[:HAS_ATTRIBUTE]->(aseg)-[:HAS_VALUE]->(vseg)
  WHERE aseg.name = "name" AND vseg.value =~ "c[0-9].*"
RETURN DISTINCT vslug.value AS slug, vn.value AS comp_name,
       vct.value AS comp_type, vseg.value AS segment_name
ORDER BY vslug.value
""")

    print()
    print("Components:")
    for r in comp_rows:
        if customer and not r["slug"].startswith(customer):
            continue
        seg = r.get("segment_name") or "—"
        print(f"  {r['slug']:50s}  type={r.get('comp_type', '?'):10s}  seg={seg}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List applications and components.")
    parser.add_argument("--customer", help="Filter by customer prefix (e.g. c001)")
    args = parser.parse_args()
    main(args.customer)
