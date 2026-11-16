"""Bounded local projection worker; no scheduling or external provider actions."""
import argparse
import json
import os
from .service import ResearchService

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-url', default=os.environ.get('RA_DATABASE_URL'), required=not bool(os.environ.get('RA_DATABASE_URL')))
    parser.add_argument('--owner-id', default=os.environ.get('RA_OWNER_ID', 'local'))
    parser.add_argument('--scope-id', default=os.environ.get('RA_SCOPE_ID', 'private'))
    parser.add_argument('--max-jobs', type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.max_jobs <= 10000:
        parser.error('--max-jobs must be between 1 and 10000')
    service = ResearchService(args.database_url, args.owner_id, args.scope_id)
    service.initialize()
    counts = {'complete': 0, 'cancelled': 0}
    for _ in range(args.max_jobs):
        result = service.run_one_job()
        if result['status'] == 'idle':
            break
        counts[result['status']] += 1
    print(json.dumps(counts))
