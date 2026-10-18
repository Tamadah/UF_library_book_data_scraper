"""
Estimate where a call number sits on Marston's periodical shelves.

Uses the files from shelf_density.py, no catalog access needed:
  shelves.csv            your shelf list (shelf,start,end[,length])
  holdings_detail.csv    every journal with its call number and item count

How the estimate works
  Within a shelf, journals are in LC shelf order and each takes up space in
  proportion to its item count (bound volumes, issues, copies). Adding the
  items up along the shelf gives the shelf's distribution: a journal with
  177 items fills about 177 times the space of a single-item one, so the
  busy parts of the shelf stretch out and the sparse parts shrink.

  A call number's position = (items shelved before it) / (items on shelf).
  Times the shelf length, that's the distance from the start of the shelf.
  If the call number is one of the journals in the data, you also get the
  stretch its whole run covers.

Run
  python locate_on_shelf.py "QA76.6 .C662"      one call number
  python locate_on_shelf.py                     type call numbers one by one
  python locate_on_shelf.py --table             write shelf_positions.csv:
                                                each journal's start/end on
                                                its shelf (the full density map)

Needs uf_callnumber_search.py and shelf_density.py in the same folder.
"""
import argparse
import csv
import os
import sys

import shelf_density as sd
import uf_callnumber_search as u

SHELVES_FILE = "shelves.csv"
DETAIL_FILE = "holdings_detail.csv"
TABLE_FILE = "shelf_positions.csv"
BAR_WIDTH = 50


def load_holdings(path):
  if not os.path.exists(path):
    sys.exit(f"{path} not found. Run shelf_density.py first; it creates it.")
  holdings = []
  unknown_items = 0
  with open(path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
      parts = u.parse_lc(row["call_number"])
      if not parts:
        continue
      items = row.get("items", "")
      if items.strip():
        items = int(float(items))
      else:
        items = 1                      # count unknown: treat as one item
        unknown_items += 1
      holdings.append({"shelf": row["shelf"], "call_number": row["call_number"],
                       "title": row["title"], "items": max(items, 0),
                       "key": u.lc_key(parts)})
  return holdings, unknown_items


def build_layout(shelves, holdings):
  """
  For each shelf: its journals in shelf order, each with the fraction of the
  shelf where its run starts and ends.
  """
  by_shelf = {str(s["shelf"]): [] for s in shelves}
  for h in holdings:
    if h["shelf"] in by_shelf:
      by_shelf[h["shelf"]].append(h)

  layout = {}
  for s in shelves:
    name = str(s["shelf"])
    journals = sorted(by_shelf[name], key=lambda h: h["key"])
    total = sum(h["items"] for h in journals)
    running = 0
    for h in journals:
      h["start_frac"] = running / total if total else 0.0
      running += h["items"]
      h["end_frac"] = running / total if total else 0.0
    layout[name] = {"shelf": s, "journals": journals, "total_items": total}
  return layout


def find_shelf(shelves, parts):
  """Same rule as shelf_density.py: the last shelf whose start is at or before it."""
  key = u.lc_key(parts)
  ordered = sorted(shelves, key=lambda s: u.lc_key(s["start_parts"]))
  if key < u.lc_key(ordered[0]["start_parts"]):
    return None
  last = ordered[-1]
  if key > u.lc_key(last["start_parts"]) and not u.in_lc_range(
      _rebuild(parts), last["start_parts"], last["end_parts"]):
    return None                        # past the end of the last shelf
  return [s for s in ordered if u.lc_key(s["start_parts"]) <= key][-1]


def _rebuild(parts):
  """Turn parsed parts back into a call number string for in_lc_range."""
  letters, whole, decimal, cutters = parts
  text = f"{letters}{whole}" + (f".{decimal}" if decimal else "")
  for letter, digits in cutters:
    text += f" .{letter}{digits}"
  return text


def same_journal(query, journal):
  """
  True if the query is this journal's call number, ignoring anything after
  the cutters (a volume or year): 'QK1 .J6 v.12' matches 'QK1 .J6', but
  'QK1 .J67' doesn't.
  """
  parts = u.parse_lc(query)
  return bool(parts) and u.lc_key(parts) == journal["key"]


def locate(query, shelves, layout):
  parts = u.parse_lc(query)
  if not parts:
    return {"error": f"'{query}' isn't an LC call number"}
  shelf = find_shelf(shelves, parts)
  if not shelf:
    return {"error": f"'{query}' is outside your shelf list "
                     f"({shelves[0]['start']} to {shelves[-1]['end']})"}

  info = layout[str(shelf["shelf"])]
  journals, total = info["journals"], info["total_items"]
  result = {"query": query, "shelf": shelf, "total_items": total,
            "journals_on_shelf": len(journals)}

  match = next((j for j in journals if same_journal(query, j)), None)
  if match:
    result.update(match=match, start_frac=match["start_frac"],
                  end_frac=match["end_frac"],
                  frac=(match["start_frac"] + match["end_frac"]) / 2)
    return result

  # Not one of the journals: it goes where its call number falls between them
  key = u.lc_key(parts)
  before = [j for j in journals if j["key"] < key]
  frac = before[-1]["end_frac"] if before else 0.0
  after = next((j for j in journals if j["key"] > key), None)
  result.update(match=None, frac=frac, start_frac=frac, end_frac=frac,
                neighbour_before=before[-1] if before else None,
                neighbour_after=after)
  return result


def describe(result):
  if "error" in result:
    return result["error"]
  s = result["shelf"]
  length = s.get("length")
  lines = [f"\n{result['query']}",
           f"  Shelf {s['shelf']}  ({s['start']} – {s['end']}),  "
           f"{result['total_items']} items, {result['journals_on_shelf']} journals"]
  if result["total_items"] == 0:
    lines.append("  No items recorded on this shelf, so no position estimate.")
    return "\n".join(lines)

  def where(frac):
    pct = f"{100 * frac:.0f}% along"
    return f"{pct} ({frac * length:.1f} of {length:g} from the start)" if length else pct

  m = result.get("match")
  if m:
    lines.append(f"  Journal: {m['title']}  ({m['call_number']}, {m['items']} items)")
    lines.append(f"  Its run: from {where(result['start_frac'])}")
    lines.append(f"           to   {where(result['end_frac'])}")
  else:
    lines.append(f"  Not one of the journals in the data; estimated by its call number:")
    lines.append(f"  About {where(result['frac'])}")
    b, a = result.get("neighbour_before"), result.get("neighbour_after")
    if b:
      lines.append(f"    after  {b['call_number']}  ({b['title']})")
    if a:
      lines.append(f"    before {a['call_number']}  ({a['title']})")

  # A picture of the shelf, with the spot or the run marked
  bar = ["·"] * BAR_WIDTH
  lo = min(BAR_WIDTH - 1, int(result["start_frac"] * BAR_WIDTH))
  hi = min(BAR_WIDTH - 1, max(lo, int(result["end_frac"] * BAR_WIDTH) - (1 if m else 0)))
  for i in range(lo, hi + 1):
    bar[i] = "█"
  lines.append(f"  start |{''.join(bar)}| end")
  return "\n".join(lines)


def write_table(layout):
  """Every journal's position on its shelf: the full distribution map."""
  with open(TABLE_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["shelf", "call_number", "title", "items",
                     "start_fraction", "end_fraction", "start_position",
                     "end_position"])
    for name, info in layout.items():
      length = info["shelf"].get("length")
      for j in info["journals"]:
        writer.writerow([name, j["call_number"], j["title"], j["items"],
                         round(j["start_frac"], 4), round(j["end_frac"], 4),
                         round(j["start_frac"] * length, 2) if length else "",
                         round(j["end_frac"] * length, 2) if length else ""])
  print(f"Each journal's position saved to {TABLE_FILE}")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("call_numbers", nargs="*", help="call number(s) to locate")
  parser.add_argument("--shelves", default=SHELVES_FILE)
  parser.add_argument("--details", default=DETAIL_FILE)
  parser.add_argument("--table", action="store_true",
                      help=f"write every journal's position to {TABLE_FILE}")
  args = parser.parse_args()

  shelves = sd.read_shelves(args.shelves)
  holdings, unknown = load_holdings(args.details)
  layout = build_layout(shelves, holdings)
  if unknown:
    print(f"Note: {unknown} journal(s) have no item count yet and are treated "
          f"as 1 item. Rerun shelf_density.py to fill them in.")

  if args.table:
    write_table(layout)
  for q in args.call_numbers:
    print(describe(locate(q, shelves, layout)))
  if not args.call_numbers and not args.table:
    print("Type a call number (or press Enter to quit).")
    while True:
      try:
        q = input("> ").strip()
      except (EOFError, KeyboardInterrupt):
        break
      if not q:
        break
      print(describe(locate(q, shelves, layout)))


if __name__ == "__main__":
  main()
