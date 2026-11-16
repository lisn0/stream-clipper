"""Save a bounded sample of live source segments while the benchmark runs."""

import argparse
import json
import os
import shutil
import time
from pathlib import Path


MAX_REVIEW_BYTES = 384 * 1024**2


def sample_type(row):
    decision = row["decision"]
    if decision in {"exported", "boundaries need review", "footage rejected"}:
        return decision
    ranking = row["ranking"]
    if ranking["chat_qualified"] or ranking["viewer_creators"] >= 3:
        return "strong single signal"
    return None


def save_source(staging, output, row, start, end, remaining_bytes):
    channel = row["channel"]
    sources = []
    for recording in staging.glob(f"{channel}-*"):
        matches = []
        for path in recording.glob("*.ts"):
            pdt = int(path.stem.rsplit("-", 1)[1]) / 1_000_000
            if start - 4 <= pdt <= end:
                matches.append((path, pdt))
        if len(matches) > len(sources):
            sources = matches
    if not sources:
        return 0
    size = sum(path.stat().st_size for path, _ in sources)
    if size > remaining_bytes:
        return 0
    sources.sort(key=lambda item: item[1])
    output.mkdir(parents=True, exist_ok=True)
    segment_times = []
    for path, pdt in sources:
        target = output / path.name
        if not target.exists():
            os.link(path, target)
        segment_times.append(pdt)
    review = {**row, "review_source": {"requested": [start, end], "segment_starts": segment_times}}
    (output / "decision.json").write_text(json.dumps(review, indent=2) + "\n")
    return size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staging", type=Path)
    parser.add_argument("monitor_pid", type=int)
    args = parser.parse_args()
    audit = args.staging / "quality.jsonl"
    offset = 0
    saved = set()
    retained_bytes = 0
    origin = time.time()
    while True:
        running = True
        try:
            os.kill(args.monitor_pid, 0)
        except ProcessLookupError:
            running = False
        if audit.exists():
            with audit.open() as decisions:
                decisions.seek(offset)
                while line := decisions.readline():
                    if not line.endswith("\n"):
                        break
                    offset = decisions.tell()
                    row = json.loads(line)
                    kind = sample_type(row)
                    if kind is None or shutil.disk_usage(args.staging).free <= 7 * 1024**3:
                        continue
                    ranking = row["ranking"]
                    anchor = ranking["chat_anchor"] or row["timestamp"]
                    bucket = (int((anchor - origin) // 1800), kind)
                    if bucket in saved:
                        continue
                    start = min(row.get("source_start", anchor), anchor - 35)
                    end = max(row.get("source_end", anchor), anchor + 20)
                    output = args.staging / "review-source" / f"{row['channel']}-{row['timestamp']:.6f}"
                    try:
                        size = save_source(args.staging, output, row, start, end,
                                           MAX_REVIEW_BYTES - retained_bytes)
                        if size:
                            retained_bytes += size
                            saved.add(bucket)
                    except OSError as exc:
                        print(f"Could not retain review context for {output.name}: {exc}", flush=True)
        if not running:
            break
        time.sleep(10)
    print(f"Retained {len(saved)} live source samples, {retained_bytes} bytes", flush=True)


if __name__ == "__main__":
    main()
