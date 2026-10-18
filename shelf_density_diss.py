"""
Count the dissertations on each shelf of Marston's dissertation area, the
same way shelf_density.py does for periodicals, but searching one year at a
time: LD1780 1913, LD1780 1914, ... LD1780 2026 (and LD1780.1 for each year).

Input:  a CSV with one row per shelf, in shelf order:
          shelf,start,end,length
          1,LD1780 1913,LD1780 1973 .S689,100
          2,LD1780 1973 .S714,LD1780 1987 .D695,100
        length is optional (any unit).

Output: shelf_counts.csv        titles (and items) per shelf
        holdings_detail.csv     one row per dissertation: call number, shelf
        shelf_distribution.png  bar chart per shelf
        These have the same names and columns as shelf_density.py's, so
        locate_on_shelf.py and shelf_distribution.py work on them; run this
        in its own folder so periodicals results aren't overwritten.

Run:    python shelf_density_diss.py shelves.csv                (titles only)
        python shelf_density_diss.py shelves.csv --items        (also count items)
        python shelf_density_diss.py shelves.csv --fresh        (start over)

Needs uf_callnumber_search.py in the same folder.

How it works
  1. For every year from FIRST_YEAR to LAST_YEAR it searches each call number
     prefix in PREFIXES followed by that year ("LD1780 1973"). A year with
     more than 500 dissertations (the most the catalog returns for one
     search) is split by the first letter of the cutter ("LD1780 1973 S*"),
     and further by its digits if needed, so nothing is cut off.
  2. Each dissertation goes on the last shelf whose start label comes at or
     before its call number, years included: LD1780 1950 sorts after
     LD1780 1913 and before LD1780 1973 .S689.
  3. Every finished search is saved, so if the run stops, running it again
     picks up where it left off.
"""
import argparse
import csv
import json
import os
import re
import string
import sys
import time

import requests

import uf_callnumber_search as u

LOCATIONS = ["diss"]            # Marston's dissertation location
PREFIXES = ["LD1780", "LD1780.1"]
FIRST_YEAR = 1913
LAST_YEAR = 2026

MAX_READABLE = 500              # the catalog won't page past 500 results
PAUSE_BETWEEN_SEARCHES = 1.0    # seconds, to go easy on the shared catalog

OUTPUT_FILE = "shelf_counts_diss.csv"
DETAIL_FILE = "holdings_detail_diss.csv"
CHART_FILE = "shelf_distribution_diss.png"
SEARCH_CACHE_FILE = "diss_search_cache_diss.json"
ITEM_CACHE_FILE = "item_counts_cache_diss.json"


# --------------------------------------------------------------------------
# Call numbers, including years
# --------------------------------------------------------------------------

LC_PATTERN = re.compile(r"^\s*([A-Z]{1,3})\s*(\d+)(?:\.(\d+))?(.*)$")
CUTTER_PATTERN = re.compile(r"^\s*\.?\s*([A-Z])(\d+)")
YEAR_PATTERN = re.compile(r"^\s*(\d{4})(?!\d)[A-Z]?")
YEAR = "#"


def parse_lc(call_number):
  """
  "LD1780 1973 .S714"   -> ("LD", "1780", "", [("#", "1973"), ("S", "714")])
  "LD1780.1 2014 .S7888" -> ("LD", "1780", "1", [("#", "2014"), ("S", "7888")])
  Returns None if it isn't an LC call number.
  """
  m = LC_PATTERN.match((call_number or "").upper())
  if not m:
    return None
  letters, whole, decimal, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4)
  parts = []
  while True:
    c = CUTTER_PATTERN.match(rest)
    if c:
      parts.append((c.group(1), c.group(2)))
      rest = rest[c.end():]
      continue
    y = YEAR_PATTERN.match(rest)
    if y:
      parts.append((YEAR, y.group(1)))
      rest = rest[y.end():]
      continue
    break
  return letters, whole, decimal, parts


def lc_key(parsed, limit_to=None):
  """
  Shelf-order key: class, number, decimal, then years (as numbers) and
  cutters (as decimals, so S689 < S7 < S714). With limit_to (another parsed
  call number), the key is cut to that one's detail, so an end label of
  "LD1780 1973 .S689" also covers "LD1780 1973 .S6891".
  """
  letters, whole, decimal, parts = parsed
  if limit_to is not None:
    decimal = decimal[:len(limit_to[2])]
    parts = parts[:len(limit_to[3])]
  key = [letters, int(whole), decimal.ljust(12, "0")]
  for i, (letter, digits) in enumerate(parts):
    if letter == YEAR:
      key += ["0", digits.zfill(12)]
      continue
    if limit_to is not None and i < len(limit_to[3]):
      digits = digits[:len(limit_to[3][i][1])]
    key += ["1" + letter, digits.ljust(12, "0")]
  return key


def sort_key(call_number):
  parsed = parse_lc(call_number)
  return (0, lc_key(parsed)) if parsed else (1, [call_number or ""])


# --------------------------------------------------------------------------
# Shelf list
# --------------------------------------------------------------------------

def read_shelves(path):
  shelves = []
  try:
    f = open(path, newline="", encoding="utf-8-sig")
  except FileNotFoundError:
    sys.exit(f"{path} not found")
  with f:
    reader = csv.DictReader(f)
    columns = {c.strip().lower(): c for c in (reader.fieldnames or [])}
    missing = {"shelf", "start", "end"} - set(columns)
    if missing:
      sys.exit(f"{path} needs the columns shelf,start,end "
               f"(missing: {', '.join(sorted(missing))})")
    for line_no, row in enumerate(reader, 2):
      name = row[columns["shelf"]].strip()
      start, end = row[columns["start"]].strip(), row[columns["end"]].strip()
      if not (name or start or end):
        continue
      sp, ep = parse_lc(start), parse_lc(end)
      if not sp or not ep:
        sys.exit(f"{path} line {line_no}: '{start}' or '{end}' isn't a call number")
      if lc_key(sp) > lc_key(ep):
        sys.exit(f"{path} line {line_no}: start '{start}' comes after end '{end}'")
      length = None
      if "length" in columns and row[columns["length"]].strip():
        length = float(row[columns["length"]])
      shelves.append({"shelf": name, "start": start, "end": end, "length": length,
                      "start_parts": sp, "end_parts": ep, "holdings": []})
  if not shelves:
    sys.exit(f"{path} has no shelves")
  return shelves


# --------------------------------------------------------------------------
# Searching year by year
# --------------------------------------------------------------------------

def load_json(path):
  try:
    with open(path, encoding="utf-8") as f:
      return json.load(f)
  except (OSError, ValueError):
    return {}


def save_json(path, data):
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f)
  os.replace(tmp, path)


def narrower(term):
  """
  Split a search that has too many results:
    "LD1780 1973"     -> "LD1780 1973 A*" ... "Z*"  (first letter of the cutter)
    "LD1780 1973 S*"  -> "LD1780 1973 S1*" ... "S9*"
    "LD1780 1973 S7*" -> "LD1780 1973 S70*" ... "S79*"
  The letter L is searched as L1* ... L9* straight away, because L* would
  also match the LD1780 part of every call number.
  """
  words = term.split()
  last = words[-1]
  if len(words) == 2:                       # prefix + year
    out = []
    for letter in string.ascii_uppercase:
      if letter == "L":
        out += [f"{term} L{d}*" for d in "123456789"]
      else:
        out.append(f"{term} {letter}*")
    return out
  stem = last.rstrip("*")
  digits = "123456789" if stem.isalpha() else string.digits
  return [f"{' '.join(words[:-1])} {stem}{d}*" for d in digits]


def run_search(query, facets, wanted, log, depth=0):
  """One search; split it if the catalog can't return all of its results."""
  _, total = u.fetch_page(query, facets, 0, 1, raw=True)
  if total == 0:
    return []
  if total <= MAX_READABLE or depth >= 6:
    if total > MAX_READABLE:
      log.append(f"'{query}' has {total} results; only {MAX_READABLE} could be read")
    results, _ = u.collect_results(query, facets, wanted, raw=True,
                                   max_results=MAX_READABLE)
    return results
  print(f"    '{query}' has {total} results, splitting it")
  results = []
  for child in narrower(query):
    results += run_search(child, facets, wanted, log, depth + 1)
  return results


def collect_all():
  facets = u.build_facets(LOCATIONS)
  wanted = {c.upper() for c in LOCATIONS}
  queries = [f"{prefix} {year}" for year in range(FIRST_YEAR, LAST_YEAR + 1)
             for prefix in PREFIXES]
  cache = load_json(SEARCH_CACHE_FILE)
  done = sum(1 for q in queries if q in cache)
  print(f"Searching {len(queries)} prefix/year combinations "
        f"({FIRST_YEAR}-{LAST_YEAR})" + (f", {done} already done" if done else ""))

  holdings, seen, log = [], set(), []
  for i, query in enumerate(queries, 1):
    if query not in cache:
      cache[query] = run_search(query, facets, wanted, log)
      save_json(SEARCH_CACHE_FILE, cache)
      time.sleep(PAUSE_BETWEEN_SEARCHES)
    for r in cache[query]:
      key = (r["holding"].get("holdId"), r["call_number"])
      if key not in seen:
        seen.add(key)
        holdings.append(r)
    if i % 20 == 0 or i == len(queries):
      print(f"  {i}/{len(queries)} searches done, {len(holdings)} dissertations so far")
  for line in log:
    print(f"  WARNING: {line}")
  return holdings


# --------------------------------------------------------------------------
# Shelves and item counts
# --------------------------------------------------------------------------

def assign_to_shelves(holdings, shelves):
  """A dissertation goes on the last shelf whose start label is at or before it."""
  ordered = sorted(shelves, key=lambda s: lc_key(s["start_parts"]))
  first, last = ordered[0], ordered[-1]
  outside = 0
  for h in holdings:
    parsed = parse_lc(h["call_number"])
    if not parsed or lc_key(parsed) < lc_key(first["start_parts"]):
      outside += 1
      continue
    if (lc_key(parsed) > lc_key(last["start_parts"])
        and lc_key(parsed, limit_to=last["end_parts"]) > lc_key(last["end_parts"])):
      outside += 1                          # after the last shelf's end label
      continue
    shelf = [s for s in ordered if lc_key(s["start_parts"]) <= lc_key(parsed)][-1]
    shelf["holdings"].append(h)
  return outside


def add_item_counts(shelves):
  cache = load_json(ITEM_CACHE_FILE)
  todo = [h for s in shelves for h in s["holdings"]
          if str(h["holding"].get("holdId")) not in cache]
  if todo:
    print(f"Counting items for {len(todo)} dissertations "
          f"(about {len(todo) * 1.5 / 60:.0f} min)...")
  failures = 0
  for i, h in enumerate(todo, 1):
    try:
      counts = u.fetch_item_counts(h["doc"], h["holding"])
      cache[str(h["holding"].get("holdId"))] = {"items": counts["items"],
                                                "volumes": len(counts["volumes"]),
                                                "call_number": h["call_number"]}
    except (requests.exceptions.RequestException, LookupError, ValueError):
      failures += 1
    if i % 10 == 0 or i == len(todo):
      save_json(ITEM_CACHE_FILE, cache)
    if i % 100 == 0 or i == len(todo):
      print(f"  {i}/{len(todo)} done")
    time.sleep(u.PAUSE_BETWEEN_ITEM_REQUESTS)
  if failures:
    print(f"  {failures} item count(s) failed; run again to retry them")
  for s in shelves:
    for h in s["holdings"]:
      entry = cache.get(str(h["holding"].get("holdId")))
      h["items"] = entry["items"] if entry else None
      h["volume_count"] = entry.get("volumes") if entry else None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def record_id(h):
  return (h["doc"].get("pnx", {}).get("control", {}).get("recordid") or [h["title"]])[0]


def write_outputs(shelves, count_items):
  has_length = all(s["length"] for s in shelves)
  rows = []
  for s in shelves:
    row = {"shelf": s["shelf"], "start": s["start"], "end": s["end"],
           "titles": len({record_id(h) for h in s["holdings"]})}
    if count_items:
      counted = [h["items"] for h in s["holdings"] if h.get("items") is not None]
      row["items"] = sum(counted)
      row["items_missing"] = len(s["holdings"]) - len(counted)
    if has_length:
      measure = row["items"] if count_items else row["titles"]
      row["length"] = s["length"]
      row["per_unit_length"] = round(measure / s["length"], 2)
    rows.append(row)

  with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

  with open(DETAIL_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    header = ["shelf", "call_number", "title"]
    if count_items:
      header += ["items", "volumes"]
    writer.writerow(header + ["record_id"])
    for s in shelves:
      for h in sorted(s["holdings"], key=lambda h: sort_key(h["call_number"])):
        row = [s["shelf"], h["call_number"], h["title"]]
        if count_items:
          row += ["" if h.get("items") is None else h["items"],
                  "" if h.get("volume_count") is None else h["volume_count"]]
        writer.writerow(row + [record_id(h)])

  w = max([32] + [len(r["start"]) + len(r["end"]) + 5 for r in rows])
  print(f"\n{'Shelf':<10}{'Range':<{w}}{'Titles':>8}" +
        (f"{'Items':>8}" if count_items else ""))
  for r in rows:
    line = f"{r['shelf']:<10}{(r['start'] + ' – ' + r['end']):<{w}}{r['titles']:>8}"
    if count_items:
      line += f"{r['items']:>8}"
    print(line)
  print(f"\nTotal: {sum(r['titles'] for r in rows)} dissertations")
  print(f"Saved to {OUTPUT_FILE} and {DETAIL_FILE}")
  return rows


def make_chart(rows, count_items):
  try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
  except ImportError:
    print("Chart skipped: install matplotlib to get it.")
    return
  bar, surface, text, text2, grid = "#2a78d6", "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
  measure = "items" if count_items else "titles"
  panels = [(measure, f"Dissertations per shelf ({measure})", measure.capitalize())]
  if "per_unit_length" in rows[0]:
    panels.append(("per_unit_length", f"{measure.capitalize()} per unit of shelf length",
                   f"{measure.capitalize()} per unit length"))
  names = [str(r["shelf"]) for r in rows]
  fig, axes = plt.subplots(len(panels), 1, figsize=(max(7, 0.35 * len(rows) + 3),
                           3.6 * len(panels)), facecolor=surface, squeeze=False)
  for ax, (key, title, ylabel) in zip(axes[:, 0], panels):
    values = [r[key] for r in rows]
    ax.set_facecolor(surface)
    ax.bar(range(len(rows)), values, width=0.82, color=bar, zorder=2)
    ax.set_title(title, loc="left", color=text, fontsize=12, pad=10)
    ax.set_ylabel(ylabel, color=text2, fontsize=9)
    ax.set_xlabel("Shelf", color=text2, fontsize=9)
    ax.grid(axis="y", color=grid, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
      ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.tick_params(colors=text2, labelsize=8, length=0)
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(names)
    if values and max(values) > 0:
      ax.set_ylim(0, max(values) * 1.15)
  fig.tight_layout()
  fig.savefig(CHART_FILE, dpi=150, facecolor=surface)
  plt.close(fig)
  print(f"Chart saved to {CHART_FILE}")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("shelves", nargs="?", default="shelves.csv")
  parser.add_argument("--items", action="store_true",
                      help="also count items per dissertation (slow)")
  parser.add_argument("--fresh", action="store_true",
                      help="ignore saved searches and start over")
  args = parser.parse_args()
  if args.fresh and os.path.exists(SEARCH_CACHE_FILE):
    os.remove(SEARCH_CACHE_FILE)

  print(f"shelf_density_diss.py, using uf_callnumber_search.py version "
        f"{u.SCRIPT_VERSION}")
  if "raw" not in __import__("inspect").signature(u.fetch_page).parameters:
    sys.exit("uf_callnumber_search.py is too old; use version 2026-10-17e or later.")
  shelves = read_shelves(args.shelves)
  print(f"{len(shelves)} shelves, {shelves[0]['start']} to {shelves[-1]['end']}")

  try:
    holdings = collect_all()
  except requests.exceptions.RequestException as e:
    sys.exit(f"Error connecting to the catalog: {e}\n"
             f"Run the same command again to continue where it stopped.")

  outside = assign_to_shelves(holdings, shelves)
  print(f"{len(holdings) - outside} dissertations fall on your shelves "
        f"({outside} before the first shelf or after the last were left out)")
  if args.items:
    add_item_counts(shelves)
  rows = write_outputs(shelves, count_items=args.items)
  make_chart(rows, count_items=args.items)


if __name__ == "__main__":
  main()
