# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
from unittest import TestCase

from qrl.core import logger
from qrl.core.Chain import Chain
from qrl.core.State import State
from tests.misc.helper import set_wallet_dir

logger.initialize_default(force_console_output=True)


class TestChain(TestCase):
    def __init__(self, *args, **kwargs):
        super(TestChain, self).__init__(*args, **kwargs)
        # test_dir = os.path.dirname(os.path.abspath(__file__))
        # config.user.wallet_path = os.path.join(test_dir, 'known_data/testcase1')

    def test_create(self):
        with set_wallet_dir("test_wallet"):
            with State() as state:
                self.assertIsNotNone(state)

                chain = Chain(state)
                self.assertIsNotNone(chain)

                self.assertEqual(chain.staking_address,
                                 b'Q1d6222fe3e53fafe8ce33acd2f8385c6dc044ab55452f0ebceb4d00233935ffaa72dd826')

                self.assertEqual(chain.wallet.address_bundle[0].address,
                                 b'Q1d6222fe3e53fafe8ce33acd2f8385c6dc044ab55452f0ebceb4d00233935ffaa72dd826')
