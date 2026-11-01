"""Print a fail-closed Tunnel configuration; never create DNS or public resources."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from personal_os.cloudflare_access import AccessGate


def configuration(hostname, issuer, audience):
    gate = AccessGate('https://' + hostname, issuer, audience, 'owner' + '@' + 'example.com')
    return {'config': {'ingress': [
        {'hostname': gate.hostname, 'service': 'http://127.0.0.1:8788', 'originRequest': {
            'httpHostHeader': gate.hostname,
            'access': {'required': True, 'teamName': issuer.removeprefix('https://').split('.')[0],
                       'audTag': [audience]}}},
        {'service': 'http_status:404'}]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--issuer', required=True)
    parser.add_argument('--audience', required=True)
    args = parser.parse_args()
    print(json.dumps(configuration(args.hostname, args.issuer, args.audience), indent=2))


if __name__ == '__main__':
    main()
