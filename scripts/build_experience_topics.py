"""Build the five-library experience index. Use --apply to publish verified results."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import community_experience_service as service

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cache", default="var/experience-topic-cache")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    args = parser.parse_args()
    with service.store.connect() as conn, conn.cursor() as cur:
        source = service.records(cur)
    extracted = service.prepare(source, args.cache, args.workers)
    if args.apply:
        with service.store.connect() as conn:
            report = service.publish(conn, source, extracted)
    else:
        topics, links = service.assemble(source, extracted)
        report = {
            "knowledge_rows": len(source),
            "topics": len(topics),
            "source_links": len(links),
            "terms": len(topics) * 5,
        }
    print(json.dumps(report))
