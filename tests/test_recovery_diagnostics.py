import unittest
from unittest.mock import Mock,patch
from src.recovery_diagnostics import ReadOnlyPaperSession,probe
from src.safety import SafetyError


class DiagnosticTests(unittest.TestCase):
    def test_transport_rejects_mutations_live_and_unlisted_paths(self):
        with ReadOnlyPaperSession() as session, patch('requests.Session.request') as request:
            for method,url in [('POST','https://paper-api.alpaca.markets/v2/orders'),
                               ('DELETE','https://paper-api.alpaca.markets/v2/orders'),
                               ('GET','https://api.alpaca.markets/v2/account'),
                               ('GET','https://paper-api.alpaca.markets/v2/account/configurations')]:
                with self.subTest(url=url,method=method),self.assertRaises(SafetyError):
                    session.request(method,url)
            request.assert_not_called()

    def test_wrong_paper_account_cannot_read_history_or_state(self):
        broker,store=Mock(),Mock()
        broker.account.return_value.id='other-account'
        with self.assertRaisesRegex(SafetyError,'scope mismatch'):
            probe(broker,store)
        store.assert_not_called()
        broker.orders.assert_not_called()
