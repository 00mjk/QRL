# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
import bz2
from typing import Optional

import os
from pyqrllib.pyqrllib import str2bin, bin2hstr

from qrl.core import config, logger
from qrl.core.GenesisBlock import GenesisBlock
from qrl.core.StateBuffer import StateBuffer
from qrl.core.BlockBuffer import BlockBuffer
from qrl.core.block import Block
from qrl.core.helper import get_blocks_left
from qrl.crypto.hashchain import hashchain
from qrl.crypto.misc import sha256
from copy import deepcopy


class BufferedChain:
    def __init__(self, chain=None):
        self.chain = chain

        self.blocks = dict()                    # FIXME: Using a dict is very inefficient when index are a sequence
        self._pending_blocks = dict()

        self.epoch = max(0, self.chain.height()) // config.dev.blocks_per_epoch  # Main chain epoch
        self.epoch_seed = None

        private_seed = self.chain.wallet.address_bundle[0].xmss.get_seed_private()
        self._wallet_private_seeds = {self.epoch: private_seed}
        self.hash_chain = dict()
        self.hash_chain[self.epoch] = hashchain(private_seed).hashchain

        if self.chain.height() > 0:
            self.epoch = self.chain.m_blockchain[-1].blockheader.blocknumber // config.dev.blocks_per_epoch

        # OBSOLETE ????
        self._block_framedata = dict()          # FIXME: this is used to access file chunks. Delete once we move to DB


    @property
    def staking_address(self):
        return self.chain.staking_address

    @property
    def wallet(self):
        return self.chain.wallet

    @property
    def state(self):
        return self.chain.state

    @property
    def transaction_pool(self):
        return self.chain.transaction_pool

    @property
    def length(self):
        return len(self.chain.blockchain)

    def add_pending_block(self, block)->bool:
        # FIXME: only used by POS (pre_block_logic) . Make obsolete
        # TODO : minimum block validation in unsynced state
        block_idx = block.blockheader.blocknumber
        self._pending_blocks[block_idx] = block
        return True

    def process_pending_blocks(self, block_expected_min: int):
        # FIXME: Pending blocks are not really used my. Why this?

        min_blocknum = min(self._pending_blocks.keys())
        max_blocknum = max(self._pending_blocks.keys())

        if len(self._pending_blocks) > 0 and min_blocknum == block_expected_min:
            # Below code is to stop downloading, once we see that we reached to blocknumber that are in pending_blocks
            # This could be exploited by sybil node, to send blocks in pending_blocks in order to disrupt downloading

            # FIXME: both min/max. Two passes
            logger.info('Processing pending blocks %s %s', min_blocknum, max_blocknum)

            # FIXME: avoid iterating by using keys
            for blocknum in range(min_blocknum, max_blocknum + 1):
                self.add_block(self._pending_blocks[blocknum])

            self._pending_blocks = dict()

            return True

        return False

    def get_last_block(self)->Optional[Block]:
        if len(self.blocks) == 0:
            return self.chain.get_last_block()

        # FIXME: Should this run max on the keys only? Or better keep track of the value..
        last_blocknum = max(self.blocks.keys())
        return self.blocks[last_blocknum][0].block      # FIXME: What does [0] refers to?

    def get_block(self, block_idx: int)->Optional[Block]:
        if block_idx in self.blocks:
            return self.blocks[block_idx][0].block      # FIXME: What does [0] refers to?
        return self.chain.get_block(block_idx)

    def add_block_mainchain(self, block, validate=True):
        if block.blocknumber <= self.chain.height():
            return

        if block.blocknumber - 1 == self.chain.height():
            if block.prev_headerhash != self.chain.m_blockchain[-1].headerhash:
                logger.info('prev_headerhash of block doesnt match with headerhash of m_blockchain')
                return
        elif block.blocknumber - 1 > 0:
            if block.blocknumber - 1 not in self.blocks or block.prev_headerhash != self.blocks[block.blocknumber - 1][0].block.headerhash:
                logger.info('No block found in buffer that matches with the prev_headerhash of received block')
                return

        if validate:
            if not self.chain.add_block(block):
                logger.info("Failed to add block by add_block, re-requesting the block #%s", block.blocknumber)
                return
        else:
            if self.chain.state.add_block(self.chain, block, ignore_save_wallet=True) is True:
                self.chain.m_blockchain.append(block)

        block_left = config.dev.blocks_per_epoch - (
            block.blockheader.blocknumber - (block.blockheader.epoch * config.dev.blocks_per_epoch))

        if block_left == 1:
            private_seed = self.chain.wallet.address_bundle[0].xmss.get_seed_private()
            self._wallet_private_seeds[block.epoch + 1] = private_seed
            self.hash_chain[block.epoch + 1] = hashchain(private_seed, epoch=block.epoch + 1).hashchain

        self._clean_if_required(block.blocknumber)

        self.epoch_seed = bin2hstr(sha256(tuple(block.blockheader.reveal_hash) + str2bin(self.epoch_seed)))

        self.chain.state.update_last_tx(block)
        self.chain.state.update_tx_metadata(block)
        self.epoch = block.epoch
        return True

    def add_block(self, block: Block)->bool:
        if not block.validate_block(self):                        # This is here because of validators, etc
            logger.info('Block validation failed')
            logger.info('Block #%s', block.blockheader.blocknumber)
            logger.info('Stake_selector %s', block.blockheader.stake_selector)
            return False

        block_idx = block.blockheader.blocknumber
        block_headerhash = block.blockheader.headerhash
        block_prev_headerhash = block.blockheader.prev_blockheaderhash

        if block_idx <= self.chain.height():
            return False

        if block_idx - 1 == self.chain.height():
            if block_prev_headerhash != self.chain.m_blockchain[-1].blockheader.headerhash:
                logger.warning('Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
                return False
        elif block_idx - 1 not in self.blocks or block_prev_headerhash != self.blocks[block_idx - 1][0].block.blockheader.headerhash:
            logger.warning('Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
            return False

        if block_idx in self.blocks and block_headerhash == self.blocks[block_idx][0].block.blockheader.headerhash:
            return False

        if (block_idx - config.dev.reorg_limit) in self.blocks:
            self._move_to_mainchain(block_idx - config.dev.reorg_limit)

        stake_reward = {}

        state_buffer = StateBuffer()

        if block_idx - 1 == self.chain.height():
            stake_validators_list = deepcopy(self.chain.state.stake_validators_list)
            stxn_state = dict()
            # TODO: Optimization required
            if not self._state_add_block_buffer(block, stake_validators_list, stxn_state):
                logger.warning('State_validate_block failed inside chainbuffer #%d', block.blockheader.blocknumber)
                return False

            block_buffer = BlockBuffer(block, stake_reward, self.chain, self.epoch_seed,
                                       self._get_st_balance(block.transactions[0].addr_from,
                                                            block.blockheader.blocknumber))

            state_buffer.set_next_seed(block.blockheader.reveal_hash, self.epoch_seed)
            state_buffer.stake_validators_list = stake_validators_list
            state_buffer.stxn_state = stxn_state
            state_buffer.update_stxn_state(self.chain.state)
        else:
            block_state_buffer = self.blocks[block_idx - 1]
            parent_state_buffer = block_state_buffer[1]
            parent_seed = block_state_buffer[1].next_seed

            stake_validators_list = deepcopy(parent_state_buffer.stake_validators_list)
            stxn_state = deepcopy(parent_state_buffer.stxn_state)
            if not self._state_add_block_buffer(block, stake_validators_list, stxn_state):
                logger.warning('State_validate_block failed inside chainbuffer #%s', block.blockheader.blocknumber)
                return False
            block_buffer = BlockBuffer(block, stake_reward, self.chain, parent_seed,
                                       self._get_st_balance(block.transactions[0].addr_from,
                                                            block.blockheader.blocknumber))
            state_buffer.stake_validators_list = stake_validators_list
            state_buffer.stxn_state = stxn_state
            state_buffer.update(self.chain.state, parent_state_buffer, block)

        if block_idx not in self.blocks:
            self.blocks[block_idx] = [block_buffer, state_buffer]
        else:
            old_block_buffer = self.blocks[block_idx][0]

            if block_buffer.score < old_block_buffer.score:
                self.blocks[block_idx] = [block_buffer, state_buffer]
                if block_idx + 1 in self.blocks:
                    self._remove_block(block_idx + 1)
            elif block_buffer.score == old_block_buffer.score:  # When two blocks having equal score
                oldheaderhash = old_block_buffer.block.blockheader.headerhash
                newheaderhash = block_buffer.block.blockheader.headerhash
                if int(bin2hstr(newheaderhash), 16) < int(bin2hstr(oldheaderhash), 16):
                    self.blocks[block_idx] = [block_buffer, state_buffer]
                    if block_idx + 1 in self.blocks:
                        self._remove_block(block_idx + 1)

        return True

    def _remove_block(self, blocknumber):
        if blocknumber not in self.blocks:
            return

        while blocknumber in self.blocks:
            del self.blocks[blocknumber]
            blocknumber += 1

    def _state_add_block_buffer(self, block, stake_validators_list, address_txn):
        self.chain.state.load_address_state(self.chain, block, address_txn)
        is_success = self.chain.state.update(block, stake_validators_list, address_txn)
        if is_success:
            self._commit(block, stake_validators_list)
            logger.info('[ChainBuffer] Block #%s added  stake: %s', block.blockheader.blocknumber,
                        block.blockheader.stake_selector)
        return is_success

    #############################################3
    #############################################3
    #############################################3
    #############################################3

    def _update_hash_chain(self, blocknumber):
        epoch = int((blocknumber + 1) // config.dev.blocks_per_epoch)
        logger.info('Created new hash chain')

        prev_private_seed = self._wallet_private_seeds[epoch - 1]
        self._wallet_private_seeds[epoch] = prev_private_seed
        self.hash_chain[epoch] = hashchain(prev_private_seed, epoch=epoch).hashchain

    def _commit(self, block, stake_validators_list):
        blocknumber = block.blockheader.blocknumber
        blocks_left = get_blocks_left(blocknumber)
        stake_validators_list.sv_list[block.blockheader.stake_selector].nonce += 1
        for dup_tx in block.duplicate_transactions:
            if dup_tx.coinbase1.txto in stake_validators_list.sv_list:
                stake_validators_list.sv_list[dup_tx.coinbase1.txto].is_banned = True

        if blocks_left == 1:
            self._update_hash_chain(blocknumber)

        stake_validators_list.update_sv(blocknumber)

    def _get_epoch_seed(self, blocknumber):
        try:
            if blocknumber - 1 == self.chain.height():
                return int(str(self.epoch_seed), 16)

            return int(str(self.blocks[blocknumber - 1][1].next_seed), 16)
        except KeyError:
            self.error_msg('get_epoch_seed', blocknumber)
        except Exception as e:
            self.error_msg('get_epoch_seed', blocknumber, e)

        return None

    def _move_to_mainchain(self, blocknum):
        block = self.blocks[blocknum][0].block
        if not self.chain.state.add_block(self.chain, block):
            logger.info('last block failed state/stake checks, removed from chain')
            return False

        self.chain.m_blockchain.append(block)
        self.chain.remove_tx_in_block_from_pool(block)  # modify fn to keep transaction in memory till reorg
        self.chain.m_f_sync_chain()

        self.epoch_seed = self.blocks[blocknum][1].next_seed

        self.epoch = int(blocknum // config.dev.blocks_per_epoch)

        self._clean_if_required(blocknum)

        del (self.blocks[blocknum])

        self.chain.state.update_last_tx(block)
        self.chain.state.update_tx_metadata(block)
        return True

    def height(self):
        if len(self.blocks) == 0:
            return self.chain.height()
        return max(self.blocks.keys())             # FIXME: max over a dictionary?

    def pubhashExists(self, addr, pubhash, blocknumber):
        # FIXME: Move to chain
        state_addr = self.get_stxn_state(blocknumber, addr)

        if state_addr is None:
            logger.info('-->> state_addr None not possible')
            return False

        if pubhash in state_addr[2]:
            return True

        return False

    ###########################
    ###########################
    ###########################
    ###########################

    def get_block_n_score(self, blocknumber):
        try:
            return self.blocks[blocknumber][0].score
        except KeyError:
            logger.error('get_block_n_score, blocknumber not in self.blocks #%s', blocknumber)
        except Exception as e:
            logger.error('Unexpected Exception')
            logger.error('%s', e)

    def _get_last_blocknumber_timestamp(self):
        last_block = self.get_last_block()
        return last_block.blockheader.blocknumber, last_block.blockheader.timestamp

    def bkmr_tracking_blocknumber(self, ntp):
        blocknumber, timestamp = self._get_last_blocknumber_timestamp()
        if ntp.getTime() - timestamp >= config.dev.minimum_minting_delay - config.dev.timestamp_error:
            return blocknumber + 1
        return blocknumber

    def verify_BK_hash(self, data, conn_identity):
        blocknum = data.block_number
        stake_selector = data.stake_selector

        prev_headerhash = data.prev_headerhash

        if blocknum <= self.chain.height():
            return False

        sv_list = self.stake_list_get(blocknum)

        if not sv_list:
            return

        if stake_selector not in sv_list:
            return

        if sv_list[stake_selector].is_banned:
            logger.warning('Rejecting block created by banned stake selector %s', stake_selector)
            return

        if not sv_list[stake_selector].is_active:
            logger.warning('Rejecting block created by inactive stake selector %s', stake_selector)
            return

        if blocknum - 1 == self.chain.height():
            if prev_headerhash != self.chain.m_blockchain[-1].blockheader.headerhash:
                logger.warning('verify_BK_hash Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
                return
            return True
        elif blocknum - 1 not in self.blocks or prev_headerhash != self.blocks[blocknum - 1][
            0].block.blockheader.headerhash:
            logger.warning('verify_BK_hash Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
            return

        reveal_hash = data.reveal_hash

        stake_validators_list = self.get_stake_validators_list(blocknum)

        if not stake_validators_list.validate_hash(reveal_hash,
                                                   blocknum,
                                                   stake_address=stake_selector):
            logger.info('%s reveal doesnt hash to stake terminator reveal %s',
                        conn_identity, reveal_hash)
            return

        score = self.score_BK_hash(data)
        return self._is_better_block(blocknum, score)

    def score_BK_hash(self, data):
        blocknum = data.block_number
        stake_selector = data.stake_selector

        reveal_hash = tuple(data.reveal_hash)

        seed = self.chain.block_chain_buffer._get_epoch_seed(blocknum)
        score = self.chain.score(stake_address=stake_selector,
                                 reveal_one=reveal_hash,
                                 balance=self._get_st_balance(stake_selector, blocknum),
                                 seed=seed)

        return score

    def _is_better_block(self, blocknum, score):
        if blocknum not in self.blocks:
            return True

        oldscore = self.blocks[blocknum][0].score

        if score < oldscore:
            return True

        return False

    def is_duplicate_block(self, blocknum, prev_blockheaderhash, stake_selector):
        """
        A block is considered as a dirty block, if same stake validator created two different blocks
        for the same blocknumber having same prev_blockheaderhash.
        """
        if blocknum > self.height():
            return

        best_block = self.get_block(blocknum)
        best_blockheader = best_block.blockheader

        if best_blockheader.prev_blockheaderhash != prev_blockheaderhash:
            return

        if best_blockheader.stake_selector != stake_selector:
            return

        return True

    def _get_st_balance(self, stake_address, blocknumber):
        if stake_address is None:
            logger.error('stake address should not be none, returning None')
            return None

        try:
            if blocknumber - 1 == self.chain.height():
                if stake_address in self.chain.state.stake_validators_list.sv_list:
                    return self.chain.state.stake_validators_list.sv_list[stake_address].balance
                logger.info('Blocknumber not found')
                return None

            return self.blocks[blocknumber - 1][1].stake_validators_list.sv_list[stake_address].balance
        except KeyError:
            self.error_msg('get_st_balance', blocknumber)
        except Exception as e:
            self.error_msg('get_st_balance', blocknumber, e)

        return None

    def get_stxn_state(self, blocknumber, addr):
        try:
            if blocknumber - 1 == self.chain.height() or addr not in self.blocks[blocknumber - 1][1].stxn_state:
                tmp_state = self.chain.state.get_address(addr)
                return tmp_state

            stateBuffer = self.blocks[blocknumber - 1][1]

            if addr in stateBuffer.stxn_state:
                return deepcopy(stateBuffer.stxn_state[addr])  # FIXME: Why deepcopy?

            return self.chain.state.get_address(addr)
        except KeyError:
            self.error_msg('get_stxn_state', blocknumber)
        except Exception as e:
            self.error_msg('get_stxn_state', blocknumber, e)

        return None

    def stake_list_get(self, blocknumber):
        try:
            if blocknumber - 1 > self.height():
                return None

            if blocknumber - 1 == self.chain.height():
                return self.chain.state.stake_validators_list.sv_list

            stateBuffer = self.blocks[blocknumber - 1][1]

            return stateBuffer.stake_validators_list.sv_list
        except KeyError:
            self.error_msg('stake_list_get', blocknumber)
        except Exception as e:
            self.error_msg('stake_list_get', blocknumber, e)

        return None

    def future_stake_addresses(self, blocknumber):
        try:
            if blocknumber - 1 == self.chain.height():
                return self.chain.state.stake_validators_list.future_stake_addresses

            stateBuffer = self.blocks[blocknumber - 1][1]

            return stateBuffer.stake_validators_list.future_stake_addresses
        except KeyError:
            self.error_msg('stake_list_get', blocknumber)
        except Exception as e:
            self.error_msg('stake_list_get', blocknumber, e)

        return None

    def get_stake_validators_list(self, blocknumber):
        try:
            if blocknumber - 1 == self.chain.height():
                return self.chain.state.stake_validators_list

            return self.blocks[blocknumber - 1][1].stake_validators_list
        except KeyError:
            self.error_msg('get_stake_validators_list', blocknumber)
        except Exception as e:
            self.error_msg('get_stake_validators_list', blocknumber, e)

        return None

    def _clean_mining_data(self, blocknumber):
        """
        Removes the mining data from the memory.
        :param blocknumber:
        :return:
        """

        prev_epoch = blocknumber // config.dev.blocks_per_epoch

        if prev_epoch in self._wallet_private_seeds:
            del self._wallet_private_seeds[prev_epoch]

        if prev_epoch in self.hash_chain:
            del self.hash_chain[prev_epoch]

        # FIXME: This should not be here
        if prev_epoch in self.chain.slave_xmss:
            del self.chain.slave_xmss[prev_epoch]

    def _clean_if_required(self, blocknumber):
        """
        Checks if the mining data such as private_seeds, hash_chain, slave_xmss
        are no more required.
        :param blocknumber:
        :return:
        """
        prev_epoch = int((blocknumber - 1) // config.dev.blocks_per_epoch)

        sv_list = self.chain.state.stake_validators_list.sv_list
        if self.chain.state in sv_list:
            activation_blocknumber = sv_list[self.chain.state].activation_blocknumber
            if activation_blocknumber + config.dev.blocks_per_epoch == blocknumber:
                self._clean_mining_data(blocknumber - 1)
        elif prev_epoch != self.epoch:
            self._clean_mining_data(blocknumber - 1)


    ###########################
    ###########################
    ###########################
    ###########################

    def error_msg(self, func_name, blocknum, exception=None):
        if exception:
            logger.error(func_name + ' Unknown exception at blocknum: %s', blocknum)
            logger.exception(exception)
            return

        logger.error('%s blocknum not found in blocks %s', func_name, blocknum)
        if self.blocks:
            logger.error('Min block num %s', min(self.blocks))
            logger.error('Max block num %s', max(self.blocks))

    def load(self):
        self.blockchain = []

        # FIXME: Adds an empty block, later ignores and overwrites.. A complete genesis block should be here
        genesis_block = GenesisBlock().set_staking_address(self.staking_address)
        self.chain.pstate.zero_all_addresses()
        self.chain.pstate.read_genesis()

        # FIXME: Direct access - Breaks encapsulation
        self.chain.blockchain.append(genesis_block)                       # FIXME: Adds without checking???

        # FIXME: it is not nice how genesis block is ignored
        tmp_chain = self._f_read_chain(0)
        if len(tmp_chain) > 0:
            for block in tmp_chain[1:]:
                self.add_block_mainchain(block, validate=False)

        epoch = 1
        # FIXME: Avoid checking files here..
        while os.path.isfile(self._get_chain_datafile(epoch)):
            del self.blockchain[:-1]                                # FIXME: This optimization could be encapsulated
            for block in self._f_read_chain(epoch):
                self.add_block_mainchain(block, validate=False)

            epoch += 1

        self.wallet.save_wallet()
        return self.blockchain

    @staticmethod
    def _get_chain_datafile(epoch):
        base_dir = os.path.join(config.user.data_path, config.dev.chain_file_directory)
        config.create_path(base_dir)
        return os.path.join(base_dir, 'chain.da' + str(epoch))

    def _f_read_chain(self, epoch):
        delimiter = config.dev.binary_file_delimiter
        chunk_filename = self._get_chain_datafile(epoch)
        block_list = []

        if os.path.isfile(chunk_filename):
            try:
                with open(chunk_filename, 'rb') as myfile:
                    json_block = bytearray()
                    tmp = bytearray()
                    count = 0
                    offset = 0
                    while True:
                        chars = myfile.read(config.dev.chain_read_buffer_size)
                        for char in chars:
                            offset += 1
                            if count > 0 and char != delimiter[count]:
                                count = 0
                                json_block += tmp
                                tmp = bytearray()
                            if char == delimiter[count]:
                                tmp.append(delimiter[count])
                                count += 1
                                if count < len(delimiter):
                                    continue
                                tmp = bytearray()
                                count = 0
                                pos = offset - len(delimiter) - len(json_block)
                                json_block = bz2.decompress(json_block)

                                block = Block.from_json(json_block)

                                self._update_block_metadata(block.blocknumber, pos, len(json_block))

                                block_list.append(block)

                                json_block = bytearray()
                                continue
                            json_block.append(char)
                        if len(chars) < config.dev.chain_read_buffer_size:
                            break
            except Exception as e:
                logger.error('IO error %s', e)
                block_list = []

        return block_list

    def _update_block_metadata(self, block_number, block_position, block_size):
        # FIXME: This is not scalable but it will fine fine for Oct2017 while we replace this with protobuf
        self._block_framedata[block_number] = [block_position, block_size]

    def _get_block_metadata(self, block_number: int):
        # FIXME: This is not scalable but it will fine fine for Oct2017 while we replace this with protobuf
        return self._block_framedata[block_number]
