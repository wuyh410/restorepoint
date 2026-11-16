#!/usr/bin/env python3
"""JSON bridge for the Qt client. Runs only on the Linux backup machine."""
import argparse
import json
from pathlib import Path
import re
import shutil
import socket
import sys

import snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['catalog', 'doctor', 'create'])
    parser.add_argument('--repository', type=Path, default=snapshot.DEFAULT_REPOSITORY)
    parser.add_argument('--reserve-gib', type=int, default=8)
    parser.add_argument('--areas', nargs='+', choices=snapshot.AREAS)
    parser.add_argument('--software-mode', choices=('files', 'reinstall'), default='files')
    args = parser.parse_args()
    if args.action == 'create':
        sys.argv = ['snapshot.py', '--repository', str(args.repository), 'create', '--reserve-gib', str(args.reserve_gib)]
        if args.areas:
            sys.argv += ['--areas', *args.areas]
        sys.argv += ['--software-mode', args.software_mode]
        snapshot.main()
    if args.action == 'doctor':
        data = {'hostname': socket.gethostname(), 'tools': {p: bool(shutil.which(p)) for p in ['python3', 'rsync', 'tar', 'gzip', 'sudo']}}
    else:
        data = []
        for path in reversed(snapshot.completed(args.repository)):
            info = json.loads((path / 'snapshot.json').read_text())
            data.append({'id': path.name, 'date': info.get('completed', ''), 'host': info.get('host', ''),
                         'kind': '完整快照' if set(snapshot.snapshot_areas(info)) == set(snapshot.DEFAULT_AREAS) else ' + '.join(snapshot.snapshot_areas(info)), 'bytes': None, 'path': str(path), 'metadata': info})
    print('RP_RESULT=' + json.dumps(data, ensure_ascii=True))


if __name__ == '__main__':
    main()
