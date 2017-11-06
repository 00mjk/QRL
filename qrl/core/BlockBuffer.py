# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.


class BlockBuffer(object):
    # FIXME: This is not really a buffer. Understand concept and refactor
    def __init__(self, block, stake_reward, chain, seed, balance):
        self.block = block
        self.stake_reward = stake_reward
        self.score = self._block_score(chain, seed, balance)

    def _block_score(self, chain, seed, balance):
        seed = int(str(seed), 16)
        score_val = chain.score(stake_address=self.block.stake_selector,
                                reveal_one=self.block.reveal_hash,
                                balance=balance,
                                seed=seed,
                                verbose=False)

        return score_val
