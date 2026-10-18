"""
One-off diagnostic: where does the item-service ID come from?

For "Journal of differential geometry" (QA641 .J67) the website used
54174258610006597. This fetches the search result and the full record the
same way the website does, then reports every place an ID shaped like
54...6597 appears, so the main script can read it from there.

Put this file next to uf_callnumber_search.py and run:  python find_service_id.py
"""
import json
import os
import re

import uf_callnumber_search as u

EXPECTED_ID = "54174258610006597"
CALL_NUMBER = "QA641 .J67"
ID_PATTERN = re.compile(r"54\d{11}6597")
OUT_DIR = "primo_debug"


def find_ids(obj, path="$"):
  """Yield (json_path, value) for every string containing a 54...6597 ID."""
  if isinstance(obj, dict):
    for k, v in obj.items():
      yield from find_ids(v, f"{path}.{k}")
  elif isinstance(obj, list):
    for i, v in enumerate(obj):
      yield from find_ids(v, f"{path}[{i}]")
  elif obj is not None:
    for match in ID_PATTERN.findall(str(obj)):
      yield path, match


def report(label, data):
  os.makedirs(OUT_DIR, exist_ok=True)
  fname = os.path.join(OUT_DIR, f"{label}.json")
  with open(fname, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
  hits = list(find_ids(data))
  print(f"\n--- {label} (saved {fname}) ---")
  if not hits:
    print("  no 54...6597 IDs found")
  for path, value in hits:
    mark = "   <-- MATCHES the website's ID" if value == EXPECTED_ID else ""
    print(f"  {path} = {value}{mark}")


def main():
  print(f"Searching for {CALL_NUMBER} ...")
  docs, total = u.fetch_page(CALL_NUMBER, u.build_facets(["per"]), 0, 5)
  doc = next((d for d in docs
              if "J67" in json.dumps(d.get("delivery", {}))), docs[0] if docs else None)
  if not doc:
    print("No search result found.")
    return
  report("search_result", doc)

  record_id = (doc.get("pnx", {}).get("control", {}).get("recordid") or [""])[0]
  print(f"\nRecord ID: {record_id}")

  # Full record, as the website loads it when a record is opened
  for vid in (u.VID, u.ITEMS_VID):
    url = f"{u.HOST}/primaws/rest/pub/pnxs/L/{record_id}"
    params = {"vid": vid, "lang": "en", "search_scope": "MyInstitution",
              "adaptor": "Local Search Engine", "lang_code": "en"}
    try:
      r = u.request_with_retry("GET", url, params=params, headers=u.HEADERS)
      print(f"\nFull record ({vid}): HTTP {r.status_code}")
      if r.ok:
        report(f"full_record_{vid.split(':')[1]}", r.json())
    except Exception as e:
      print(f"\nFull record ({vid}) failed: {e}")


if __name__ == "__main__":
  main()
