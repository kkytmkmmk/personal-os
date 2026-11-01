import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from personal_os.cloudflare_access import AccessFailure, AccessGate, from_environment

OWNER = 'owner' + '@' + 'example.com'
OTHER = 'other' + '@' + 'example.com'


class CloudflareAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.gate = AccessGate('https://os.example.com', 'https://sample.cloudflareaccess.com',
                               'sample-audience', OWNER)
        self.gate.client = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.key.public_key()))

    def headers(self, **changes):
        claims = dict(iss=self.gate.issuer, aud=self.gate.audience, sub='synthetic-owner',
                      email=OWNER, iat=int(time.time()), exp=int(time.time()) + 300)
        claims.update(changes)
        return {'Host': 'os.example.com', 'Origin': 'https://os.example.com',
                'Cf-Access-Jwt-Assertion': jwt.encode(claims, self.key, algorithm='RS256')}

    def test_valid_owner_and_mutation(self):
        self.assertEqual(self.gate.authorize(self.headers(), True), 'synthetic-owner')

    def test_invalid_or_incomplete_claims(self):
        for changes in ({'exp': 1}, {'iss': 'https://evil.example'}, {'aud': 'wrong'},
                        {'iat': int(time.time()) + 1000}, {'sub': None}):
            with self.subTest(changes=changes), self.assertRaises(AccessFailure):
                self.gate.authorize(self.headers(**changes))
        with self.assertRaises(AccessFailure):
            self.gate.authorize({'Host': 'os.example.com'})

    def test_wrong_owner(self):
        with self.assertRaises(AccessFailure) as raised:
            self.gate.authorize(self.headers(email=OTHER))
        self.assertEqual(raised.exception.status, 403)

    def test_forged_and_wrong_algorithm(self):
        headers = self.headers()
        headers['Cf-Access-Jwt-Assertion'] = jwt.encode({'email': OWNER}, 'synthetic-key' * 4, algorithm='HS256')
        with self.assertRaises(AccessFailure):
            self.gate.authorize(headers)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        headers['Cf-Access-Jwt-Assertion'] = jwt.encode(jwt.decode(self.headers()['Cf-Access-Jwt-Assertion'], options={'verify_signature': False}), other, algorithm='RS256')
        with self.assertRaises(AccessFailure):
            self.gate.authorize(headers)

    def test_cross_origin_or_absent_origin_write_rejected(self):
        for origin in ('https://other.example', ''):
            headers = self.headers()
            headers['Origin'] = origin
            with self.assertRaises(AccessFailure) as raised:
                self.gate.authorize(headers, True)
            self.assertEqual(raised.exception.status, 403)

    def test_forwarded_localhost_cannot_bypass_gate(self):
        headers = self.headers()
        headers['Host'] = '127.0.0.1:8788'
        self.assertTrue(self.gate.requires(headers))
        with self.assertRaises(AccessFailure):
            self.gate.authorize(headers)
        self.assertFalse(self.gate.requires({'Host': 'localhost:8788'}))

    def test_handler_checks_before_loopback_bypass(self):
        import app
        handler = app.Handler.__new__(app.Handler)
        handler.headers = {'Host': 'os.example.com'}
        handler.client_address = ('127.0.0.1', 1234)
        handler.send_json = lambda payload, status: self.assertEqual(status, 401)
        with patch.object(app, 'CLOUDFLARE_ACCESS', self.gate):
            self.assertFalse(handler._authorize())
        handler.headers = {'Host': 'localhost:8788'}
        with patch.object(app, 'CLOUDFLARE_ACCESS', self.gate):
            self.assertTrue(handler._authorize())

    def test_configuration_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(from_environment())
            os.environ['PERSONAL_OS_PUBLIC_ORIGIN'] = 'https://os.example.com'
            with self.assertRaises(ValueError):
                from_environment()
        for origin in ('http://os.example.com', 'https://os.example.com/path', 'https://os.example.com:443'):
            with self.assertRaises(ValueError):
                AccessGate(origin, self.gate.issuer, 'aud', OWNER)


if __name__ == '__main__':
    unittest.main()
