import unittest
from tools.plan_cloudflare_tunnel import configuration


class TunnelPlanTests(unittest.TestCase):
    def test_only_fixed_local_origin_with_access_and_catchall(self):
        ingress = configuration('os.example.com', 'https://sample.cloudflareaccess.com', 'sample-aud')['config']['ingress']
        self.assertEqual(ingress[0]['service'], 'http://127.0.0.1:8788')
        self.assertEqual(ingress[0]['originRequest']['httpHostHeader'], 'os.example.com')
        self.assertEqual(ingress[0]['originRequest']['access'], {
            'required': True, 'teamName': 'sample', 'audTag': ['sample-aud']})
        self.assertEqual(ingress[-1], {'service': 'http_status:404'})

    def test_invalid_public_configuration_refused(self):
        for hostname in ('localhost:8788', 'os.example.com/path', 'https://os.example.com'):
            with self.assertRaises(ValueError):
                configuration(hostname, 'https://sample.cloudflareaccess.com', 'aud')
        with self.assertRaises(ValueError):
            configuration('os.example.com', 'http://untrusted.example.com', 'aud')


if __name__ == '__main__':
    unittest.main()
