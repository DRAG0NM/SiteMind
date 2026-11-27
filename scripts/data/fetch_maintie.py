"""Fetch the pinned MaintIE gold data outside the repository, with provenance."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REVISION = '825e7710e4129598da997276a38b3cb40b200bc2'
BASE = f'https://raw.githubusercontent.com/nlp-tlp/maintie/{REVISION}/'
FILES = ('data/gold_release.json', 'data/scheme.json', 'LICENSE.md')


def fetch(destination):
    destination = Path(destination).resolve()
    repo = Path(__file__).resolve().parents[2]
    if destination == repo or repo in destination.parents:
        raise ValueError('Full dataset must be stored outside the repository')
    destination.mkdir(parents=True, exist_ok=True)
    metadata = {'source': 'https://github.com/nlp-tlp/maintie', 'revision': REVISION,
                'license': 'MIT', 'files': {}}
    for name in FILES:
        data = urlopen(BASE + name, timeout=60).read()
        target = destination / Path(name).name
        digest = hashlib.sha256(data).hexdigest()
        if target.exists() and target.read_bytes() != data:
            raise FileExistsError(f'Refusing to overwrite {target}')
        if not target.exists():
            target.write_bytes(data)
        metadata['files'][target.name] = {'url': BASE + name, 'sha256': digest, 'bytes': len(data)}
    path = destination / 'provenance.json'
    encoded = (json.dumps(metadata, indent=2, sort_keys=True) + '\n').encode()
    if path.exists() and path.read_bytes() != encoded:
        raise FileExistsError(f'Refusing to overwrite {path}')
    if not path.exists():
        path.write_bytes(encoded)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='Directory outside this repository')
    print(json.dumps(fetch(parser.parse_args().destination), indent=2))
