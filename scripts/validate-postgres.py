#!/usr/bin/env python3
"""Run explicit PostgreSQL acceptance checks without exposing connection secrets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    environment = dict(os.environ, RA_TEST_POSTGRES_URL=config['database_url'])
    command = args.arguments
    if command[:1] == ['--']:
        command = command[1:]
    command = command or ['-m', 'pytest', '-q', 'tests/integration/test_postgres.py']
    return subprocess.run([sys.executable, *command], env=environment).returncode

if __name__ == '__main__':
    sys.exit(main())
