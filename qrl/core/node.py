# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
import copy
import time
from collections import defaultdict
from typing import Optional

from pyqrllib.pyqrllib import bin2hstr, XmssPool
from qrl.core.AddressState import AddressState
from twisted.internet import reactor

from qrl.core import logger, config, BufferedChain, ntp
from qrl.core.Block import Block
from qrl.core.ESyncState import ESyncState
from qrl.core.StakeValidatorsTracker import StakeValidatorsTracker
from qrl.core.Transaction import StakeTransaction, DestakeTransaction, Vote, Transaction
from qrl.crypto.hashchain import hashchain
from qrl.crypto.xmss import XMSS
from qrl.generated import qrl_pb2


class SyncState:
    def __init__(self):
        self.state = ESyncState.unsynced
        self.epoch_diff = -1


class ConsensusMechanism(object):
    def __init__(self,
                 buffered_chain: BufferedChain):

        self.buffered_chain = buffered_chain

        #########
        private_seed = self.wallet.address_bundle[0].xmss.get_seed_private()
        self._wallet_private_seeds = {self.epoch: private_seed}
        self.hash_chain = dict()
        self.hash_chain[self.epoch] = hashchain(private_seed).hashchain
        #########

        self.slave_xmss_dict = dict()
        self.slave_xmsspool = None
        self._init_slave_xmsspool(0)

    def _init_slave_xmsspool(self, starting_epoch):
        baseseed = self.wallet.address_bundle[0].xmss.get_seed()
        pool_size = 2
        self.slave_xmsspool = XmssPool(baseseed,
                                       config.dev.slave_xmss_height,
                                       starting_epoch,
                                       pool_size)

    def get_slave_xmss(self, blocknumber):
        epoch = self._get_mining_epoch(blocknumber)
        if epoch not in self.slave_xmss:
            if self.slave_xmsspool.getCurrentIndex() - epoch != 0:
                self._init_slave_xmsspool(epoch)
                return None
            if not self.slave_xmsspool.isAvailable():
                return None

            # Generate slave xmss
            assert (epoch == self.slave_xmsspool.getCurrentIndex())  # Verify we are not skipping trees
            tmp_xmss = self.slave_xmsspool.getNextTree()
            self.slave_xmss[epoch] = XMSS(tmp_xmss.getHeight(), _xmssfast=tmp_xmss)

        return self.slave_xmss[epoch]


class POS(ConsensusMechanism):
    def __init__(self,
                 buffered_chain: BufferedChain,
                 p2p_factory,
                 sync_state: SyncState,
                 time_provider):

        super().__init__(buffered_chain)

        self.p2p_factory = p2p_factory  # FIXME: Decouple from p2pFactory. Comms vs node logic
        self.p2p_factory.pos = self  # FIXME: Temporary hack to keep things working while refactoring

        self.sync_state = sync_state
        self.time_provider = time_provider
        self.stake = config.user.enable_auto_staking

        ########
        self.r1_time_diff = defaultdict(list)
        self.r2_time_diff = defaultdict(list)
        self.pos_blocknum = 0
        self.pos_callLater = None

        self.incoming_blocks = {}
        self.last_pos_cycle = 0
        self.last_selected_height = 0
        self.last_bk_time = 0
        self.last_pb_time = 0
        self.next_header_hash = None
        self.next_block_number = None

        self.blockheight_map = []
        self.retry_consensus = 0  # Keeps track of number of times consensus failed for the last blocknumber

        self.epoch_diff = None

    @property
    def staking_address(self):
        return self.buffered_chain.wallet.address_bundle[0].xmss.get_address()

    @property
    def staking_xmss(self):
        return self.buffered_chain.wallet.address_bundle[0].xmss

    def staking_xmss_save(self):
        self.buffered_chain.wallet.save_wallet()

    ##################################################3
    ##################################################3
    ##################################################3
    ##################################################3

    def start(self, force_sync):
        self.restart_monitor_bk(80)
        reactor.callLater(20, self.initialize_pos, force_sync)

    def _handler_state_unsynced(self):
        self.last_bk_time = time.time()
        self.restart_unsynced_logic()

    def _handler_state_syncing(self):
        self.last_pb_time = time.time()

    def _handler_state_synced(self):
        self.sync_state.epoch_diff = 0
        self.last_pos_cycle = time.time()
        self.restart_post_block_logic()

    def _handler_state_forked(self):
        self.stop_post_block_logic()

    def update_node_state(self, new_sync_state: ESyncState):
        self.sync_state.state = new_sync_state
        logger.info('Status changed to %s', self.sync_state.state)

        _mapping = {
            ESyncState.unsynced: self._handler_state_unsynced,
            ESyncState.syncing: self._handler_state_syncing,
            ESyncState.synced: self._handler_state_synced,
            ESyncState.forked: self._handler_state_forked,
        }

        _mapping[self.sync_state.state]()

    def stop_monitor_bk(self):
        try:
            reactor.monitor_bk.cancel()
        except Exception:  # No need to log this exception
            pass

    def restart_monitor_bk(self, delay: int):
        self.stop_monitor_bk()
        reactor.monitor_bk = reactor.callLater(delay, self.monitor_bk)

    def monitor_bk(self):
        # FIXME: Too many magic numbers / timing constants
        # FIXME: This is obsolete
        time_diff1 = time.time() - self.last_pos_cycle
        if 90 < time_diff1:
            if self.sync_state.state == ESyncState.synced:
                self.stop_post_block_logic()
                self.update_node_state(ESyncState.unsynced)
                self.epoch_diff = -1
                reactor.monitor_bk = reactor.callLater(60, self.monitor_bk)
                return

            if self.sync_state.state == ESyncState.unsynced:
                if time.time() - self.last_bk_time > 120:
                    self.last_pos_cycle = time.time()
                    logger.info(' POS cycle activated by monitor_bk() ')
                    self.update_node_state(ESyncState.synced)
                reactor.monitor_bk = reactor.callLater(60, self.monitor_bk)
                return

        time_diff2 = time.time() - self.last_pb_time
        if self.sync_state.state == ESyncState.syncing and time_diff2 > 60:
            self.stop_post_block_logic()
            self.update_node_state(ESyncState.unsynced)
            self.epoch_diff = -1

        reactor.monitor_bk = reactor.callLater(60, self.monitor_bk)

    def initialize_pos(self, force_sync):
        found = False
        if self.buffered_chain.height == 0:
            genesis_block = self.buffered_chain.get_block(0)
            for raw_tx in genesis_block.transactions:
                tx = Transaction.from_pbdata(raw_tx)
                if tx.txfrom == self.staking_address:
                    found = True
            while found:
                if self.create_vote_tx(0):
                    break
                time.sleep(2)

        if found:
            reactor.callLater(1, self._handler_state_synced)
        else:
            reactor.callLater(1, self._handler_state_unsynced)

    def create_new_block(self, reveal_hash, last_block_number) -> Optional[Block]:
        # FIXME: Embed into the previous code
        logger.info('create_new_block #%s', (last_block_number + 1))
        block_obj = self.create_stake_block(reveal_hash, last_block_number)
        return block_obj

    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################

    def restart_unsynced_logic(self, delay=0):
        logger.info('Restarting unsynced logic in %s seconds', delay)
        try:
            reactor.unsynced_logic.cancel()
        except Exception:  # No need to log this exception
            pass

        reactor.unsynced_logic = reactor.callLater(delay, self.unsynced_logic)

    def unsynced_logic(self):
        '''
        Unsynced Logic
        1.	Request for maximum blockheight and passes bock number X
        2.	Peers response chain height with headerhash and the headerhash of block number X
        3.	Unsynced node, selects most common chain height, matches the headerhash of block number X
        4.	If headerhash of block number X doesn't match, change state to Forked
        5.	If headerhash of block number X matches, perform Downloading of blocks from those selected peers
        '''
        if self.sync_state.state != ESyncState.synced:
            self.p2p_factory.broadcast_get_synced_state()

            reactor.unsynced_logic = reactor.callLater(20, self.start_download)

    def start_download(self):
        # FIXME: Why PoS is downloading blocks?
        # add peers and their identity to requested list
        # FMBH
        if self.sync_state.state == ESyncState.synced:
            return

        logger.info('Checking Download..')

        if not self.p2p_factory.has_synced_peers:
            logger.warning('No connected peers in synced state. Retrying...')
            self.update_node_state(ESyncState.unsynced)
            return

        self.update_node_state(ESyncState.syncing)
        logger.info('Initializing download from %s', self.buffered_chain.height + 1)
        self.p2p_factory.randomize_block_fetch()

    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################

    def pre_block_logic(self, block: Block) -> bool:
        # FIXME: Ensure that the chain is in memory

        chain_buffer_height = self.buffered_chain.height
        last_block_before = self.buffered_chain.get_last_block()

        if block.block_number < self.buffered_chain.height:
            return False

        # FIXME: Simplify logic
        if self.sync_state.state == ESyncState.synced:
            if not self.buffered_chain.add_block(block):
                return False
        elif chain_buffer_height + 1 == block.block_number:
            if block.block_number > 1:
                if not self.buffered_chain.add_block(block):
                    return False
            elif block.block_number == 1:
                if not self.buffered_chain.add_block(block):
                    return False
            self.isSynced(block.timestamp)
        else:
            self.buffered_chain.add_pending_block(block)

        if self.sync_state.state == ESyncState.synced:
            last_block_after = self.buffered_chain.get_last_block()
            self.last_pos_cycle = time.time()
            self.p2p_factory.broadcast_block(block)
            if last_block_before.headerhash != last_block_after.headerhash:
                self.schedule_pos(block.block_number + 1)

        return True

    def schedule_pos(self, blocknumber):
        if self.sync_state.state == ESyncState.synced:
            if self.pos_callLater and self.pos_callLater.active():
                if blocknumber - self.pos_blocknum == 1:
                    return

            self.restart_post_block_logic(blocknumber)

    def stop_post_block_logic(self):
        try:
            self.pos_callLater.cancel()
        except Exception:  # No need to log this exception
            pass

        try:
            self.vote_callLater.cancel()
        except Exception:
            pass

    def restart_post_block_logic(self, blocknumber=-1, delay=None):
        if blocknumber == -1:
            blocknumber = self.buffered_chain.height + 1

        if not delay:
            last_block = self.buffered_chain.get_block(blocknumber - 1)
            last_block_timestamp = last_block.timestamp
            curr_timestamp = int(ntp.getTime())

            delay = max(5, last_block_timestamp + config.dev.minimum_minting_delay - curr_timestamp)

        self.stop_post_block_logic()
        self.pos_callLater = reactor.callLater(delay,
                                               self.post_block_logic,
                                               blocknumber=blocknumber)

        self.pos_blocknum = blocknumber

        stake_list = self.stake_list_get(blocknumber - 1)
        if self.staking_address in stake_list:
            stake_validator = stake_list[self.staking_address]
            if not (stake_validator.is_active and not stake_validator.is_banned):
                return
        else:
            return

        vote_delay = max(0, delay - config.dev.vote_x_seconds_before_next_block)

        self.vote_callLater = reactor.callLater(vote_delay,
                                                self.create_vote_tx,
                                                blocknumber=blocknumber - 1)

    def create_next_block(self, blocknumber, activation_blocknumber) -> bool:
        if blocknumber - activation_blocknumber + 1 > config.dev.blocks_per_epoch:
            logger.warning('Too old activation_blocknumber')
            logger.warning('Activation Blocknumber: %s', activation_blocknumber)
            logger.warning('Current Blocknumber: %s', blocknumber)
            return False

        if self.buffered_chain.get_slave_xmss(blocknumber):
            hash_chain = self.buffered_chain.hash_chain_get(blocknumber)

            my_reveal = hash_chain[::-1][blocknumber - activation_blocknumber + 1]
            block = self.create_new_block(my_reveal, blocknumber - 1)

            return self.pre_block_logic(block)  # broadcast this block

        return False

    def check_consensus(self, blocknumber) -> bool:
        voteMetadata = self.buffered_chain.get_consensus(blocknumber - 1)
        consensus_headerhash = self.buffered_chain.get_consensus_headerhash(blocknumber - 1)

        if not consensus_headerhash:
            logger.warning('Consensus is still None, rescheduling post_block_logic after 5 sec')
            self.restart_post_block_logic(blocknumber, 5)
            return False

        prev_sv_tracker = self.get_stake_validators_tracker(blocknumber)

        consensus_ratio = voteMetadata.total_stake_amount / prev_sv_tracker.get_total_stake_amount()

        if consensus_ratio < 0.51:
            logger.warning('Consensus below 51%%, rescheduling post_block_logic after 5 sec')
            logger.warning('%s/%s', voteMetadata.total_stake_amount, prev_sv_tracker.get_total_stake_amount())
            self.retry_consensus += 1
            if self.retry_consensus >= config.dev.max_consensus_retry and self.buffered_chain.height > 1:
                self.retry_consensus = 0
                self.buffered_chain.remove_last_buffer_block()
                self.stop_post_block_logic()
                self.update_node_state(ESyncState.unsynced)
                return False
            self.restart_post_block_logic(blocknumber, 5)
            return False

        self.retry_consensus = 0
        prev_block = self.buffered_chain.get_block(blocknumber - 1)

        if consensus_headerhash != prev_block.headerhash:
            logger.warning('Fork detected...')
            logger.warning('Fork from Block #%s', blocknumber - 1)
            logger.warning('Fork Recovery Started...')
            self.buffered_chain.expected_headerhash[blocknumber - 1] = consensus_headerhash
            self.buffered_chain.remove_last_buffer_block()
            self.stop_post_block_logic()
            self.update_node_state(ESyncState.unsynced)
            return False

        return True

    def post_block_logic(self, blocknumber):
        """
        post block logic we initiate the next POS cycle
        send ST, reset POS flags and remove unnecessary
        messages in chain.stake_reveal_one and _two..

        :return:
        """

        if not self.check_consensus(blocknumber):
            return

        if self.stake:
            future_stake_addresses = self.future_stake_addresses(blocknumber)

            if self.staking_address not in future_stake_addresses:
                self._create_stake_tx(blocknumber)

            stake_list = self.stake_list_get(blocknumber)

            delay = config.dev.minimum_minting_delay
            if self.staking_address in stake_list and stake_list[
                self.staking_address].is_active:
                if stake_list[self.staking_address].is_banned:
                    logger.warning('You have been banned.')
                else:
                    activation_blocknumber = stake_list[self.staking_address].activation_blocknumber
                    self.create_next_block(blocknumber, activation_blocknumber)
                    delay = None

            last_blocknum = self.buffered_chain.height
            self.restart_post_block_logic(last_blocknum + 1, delay)

        return

    def create_vote_tx(self, blocknumber: int):
        block = self.buffered_chain.get_block(blocknumber)
        if not block:
            logger.warning('Block #%s not found, cancelled voting', blocknumber)
            return
        signing_xmss = self.buffered_chain.get_slave_xmss(blocknumber)
        if not signing_xmss:
            logger.warning('Skipped Voting: Slave XMSS none, XMSS POOL might still be generating slave_xmss')
            return

        vote = Vote.create(blocknumber=blocknumber,
                           headerhash=block.blockheader.headerhash,
                           xmss=signing_xmss)

        vote.sign(signing_xmss)

        # FIXME: Temporary fix, need to add ST txn into Genesis
        if blocknumber > 1:
            tx_state = self.get_stxn_state(blocknumber + 1, vote.addr_from)

            stake_validators_tracker = self.get_stake_validators_tracker(blocknumber)

            if not (vote.validate() and vote.validate_extended(tx_state, stake_validators_tracker)):
                logger.warning('Create Vote Txn failed due to validation failure')
                return

        self.buffered_chain.set_voted(blocknumber)

        self.buffered_chain.add_vote(vote)

        self.p2p_factory.broadcast_vote(vote)

        return True

    def _create_stake_tx(self, curr_blocknumber):
        sv_dict = self.stake_list_get(curr_blocknumber)
        if self.staking_address in sv_dict:
            activation_blocknumber = sv_dict[
                                         self.staking_address].activation_blocknumber + config.dev.blocks_per_epoch
        else:
            activation_blocknumber = curr_blocknumber + 2  # Activate as Stake Validator, 2 blocks after current block

        if activation_blocknumber < curr_blocknumber:
            activation_blocknumber = curr_blocknumber + 2

        balance = self.get_stxn_state(curr_blocknumber, self.staking_address).balance
        if balance < config.dev.minimum_staking_balance_required:
            logger.warning('Staking not allowed due to insufficient balance')
            logger.warning('Balance %s', balance)
            return

        slave_xmss = self.buffered_chain.get_slave_xmss(activation_blocknumber)
        if not slave_xmss:
            return

        st = StakeTransaction.create(
            activation_blocknumber=activation_blocknumber,
            xmss=self.staking_xmss,
            slavePK=slave_xmss.pk()
        )

        st.sign(self.staking_xmss)
        tx_state = self.get_stxn_state(curr_blocknumber, st.txfrom)
        if not (st.validate() and st.validate_extended(tx_state)):
            logger.warning('Create St Txn failed due to validation failure, will retry next block')
            return

        self.p2p_factory.broadcast_st(st)
        for num in range(len(self.buffered_chain.tx_pool.transaction_pool)):
            t = self.buffered_chain.tx_pool.transaction_pool[num]
            if t.subtype == qrl_pb2.Transaction.STAKE and st.hash == t.hash:
                if st.get_message_hash() == t.get_message_hash():
                    return
                self.buffered_chain.tx_pool.remove_tx_from_pool(t)
                break

        self.buffered_chain.tx_pool.add_tx_to_pool(st)
        self.staking_xmss_save()

    def make_destake_tx(self):
        curr_blocknumber = self.buffered_chain.height + 1
        stake_validators_tracker = self.get_stake_validators_tracker(curr_blocknumber)

        # No destake txn required if mining address is not in stake_validator_list
        if self.staking_address not in stake_validators_tracker.sv_dict and \
                self.buffered_chain.height not in stake_validators_tracker.future_stake_addresses:
            logger.warning('%s Not found in Stake Validator list, destake txn note required',
                           self.staking_address)
            return

        # Skip if mining address is not active in either stake validator list
        if not ((self.staking_address in stake_validators_tracker.sv_dict and
                 stake_validators_tracker.sv_dict[self.staking_address].is_active) or
                (self.staking_address in stake_validators_tracker.future_stake_addresses and
                 stake_validators_tracker.future_stake_addresses[self.staking_address].is_active)):
            logger.warning('%s is already inactive in Stake validator list, destake txn not required',
                           self.staking_address)
            return

        de_stake_txn = DestakeTransaction.create(xmss=self.staking_xmss)

        de_stake_txn.sign(self.staking_xmss)
        tx_state = self.get_stxn_state(curr_blocknumber, de_stake_txn.txfrom)
        if not (de_stake_txn.validate() and de_stake_txn.validate_extended(tx_state)):
            logger.warning('Make DeStake Txn failed due to validation failure')
            return

        self.p2p_factory.broadcast_destake(de_stake_txn)
        for num in range(len(self.buffered_chain.tx_pool.transaction_pool)):
            t = self.buffered_chain.tx_pool.transaction_pool[num]
            if t.subtype == qrl_pb2.Transaction.STAKE:
                if de_stake_txn.get_message_hash() == t.get_message_hash():
                    return
                self.buffered_chain.tx_pool.remove_tx_from_pool(t)
                break

        self.buffered_chain.tx_pool.add_tx_to_pool(de_stake_txn)
        self.staking_xmss_save()

        return True

    def isSynced(self, block_timestamp) -> bool:
        if block_timestamp + config.dev.minimum_minting_delay > ntp.getTime():
            self.update_node_state(ESyncState.synced)
            return True
        return False

    def create_stake_block(self, reveal_hash, last_block_number) -> Optional[Block]:
        # TODO: Persistence will move to rocksdb
        # FIXME: Difference between this and create block?????????????

        # FIXME: Break encapsulation
        t_pool2 = copy.deepcopy(self.buffered_chain.tx_pool.transaction_pool)
        del self.buffered_chain.tx_pool.transaction_pool[:]
        ######

        # recreate the transaction pool as in the tx_hash_list, ordered by txhash..
        tx_nonce = defaultdict(int)
        total_txn = len(t_pool2)
        txnum = 0
        stake_validators_tracker = self.get_stake_validators_tracker(last_block_number + 1)
        # FIX ME : Temporary fix, to include only either ST txn or Other txn for an address
        stake_txn = set()
        transfercoin_txn = set()
        message_txn = set()
        destake_txn = set()
        token_txn = set()
        transfer_token_txn = set()
        lattice_public_key_txn = set()

        address_txn = dict()

        while txnum < total_txn:
            tx = t_pool2[txnum]
            state_addr = self.get_stxn_state(last_block_number + 1, tx.addr_from)
            if tx.ots_key_reuse(state_addr, tx.ots_key):
                del t_pool2[txnum]
                total_txn -= 1
                continue
            if tx.txfrom not in address_txn:
                address_txn[tx.txfrom] = self.get_stxn_state(last_block_number + 1, tx.txfrom)
            if tx.subtype == qrl_pb2.Transaction.TRANSFER:
                if tx.txfrom in stake_txn:
                    logger.debug("Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                    tx.txfrom].is_active:
                    logger.debug("Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].balance < tx.amount:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Transfer Amount %s', address_txn[tx.txfrom].balance,
                                   tx.amount)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                transfercoin_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.STAKE:
                if tx.txfrom in stake_validators_tracker.future_stake_addresses:
                    logger.debug('P2P dropping st as staker is already in future_stake_address %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict:
                    expiry = stake_validators_tracker.sv_dict[
                                 tx.txfrom].activation_blocknumber + config.dev.blocks_per_epoch
                    if tx.activation_blocknumber < expiry:
                        logger.debug('P2P dropping st txn as it is already active for the given range %s', tx.txfrom)
                        del t_pool2[txnum]
                        total_txn -= 1
                        continue

                if tx.txfrom in (transfercoin_txn, message_txn, token_txn, transfer_token_txn):
                    logger.debug('Dropping st txn as %s txn found in pool %s', tx.subtype, tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                # This check is to ignore multiple ST txn from same address
                if tx.txfrom in stake_txn:
                    logger.debug('Dropping st txn as existing Stake txn has been added %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                if tx.txfrom in destake_txn:
                    logger.debug('Dropping st txn as Destake txn has been added %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                if tx.txfrom in stake_validators_tracker.future_stake_addresses:
                    logger.debug('Skipping st as staker is already in future_stake_address')
                    logger.debug('Staker address : %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                if tx.txfrom in stake_validators_tracker.sv_dict:
                    expiry = stake_validators_tracker.sv_dict[
                                 tx.txfrom].activation_blocknumber + config.dev.blocks_per_epoch
                    if tx.activation_blocknumber < expiry:
                        logger.debug('Skipping st txn as it is already active for the given range %s', tx.txfrom)
                        del t_pool2[txnum]
                        total_txn -= 1
                        continue
                # skip 1st st txn without tx.first_hash in case its beyond allowed epoch blocknumber
                if tx.activation_blocknumber > self.buffered_chain.height + config.dev.blocks_per_epoch + 1:
                    logger.debug('Skipping st as activation_blocknumber beyond limit')
                    logger.debug('Expected # less than : %s',
                                 (self.buffered_chain.height + config.dev.blocks_per_epoch))
                    logger.debug('Found activation_blocknumber : %s', tx.activation_blocknumber)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                stake_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.DESTAKE:
                if tx.txfrom in stake_txn:
                    logger.debug('Dropping destake txn as stake txn has been added %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                if tx.txfrom in destake_txn:
                    logger.debug('Dropping destake txn as destake txn has already been added for %s', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue
                if tx.txfrom not in stake_validators_tracker.sv_dict and tx.txfrom not in stake_validators_tracker.future_stake_addresses:
                    logger.debug('Dropping destake txn as %s not found in stake validator list', tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                destake_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.MESSAGE:
                if tx.txfrom in stake_txn:
                    logger.debug("Txn dropped: %s address is a Message TXN", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                    tx.txfrom].is_active:
                    logger.debug("Message Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Message Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid message tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Free %s', address_txn[tx.txfrom].balance, tx.fee)
                    total_txn -= 1
                    continue

                message_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.TOKEN:
                if tx.owner not in address_txn:
                    address_txn[tx.owner] = self.get_stxn_state(last_block_number + 1, tx.owner)
                for initial_balance in tx.initial_balances:
                    if initial_balance.address not in address_txn:
                        address_txn[initial_balance.address] = self.get_stxn_state(last_block_number + 1,
                                                                                   initial_balance.address)
                if tx.txfrom in stake_txn:
                    logger.debug("Token Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                    tx.txfrom].is_active:
                    logger.debug("Token Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Token Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Fee %s',
                                   address_txn[tx.txfrom].balance,
                                   tx.fee)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                token_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.TRANSFERTOKEN:
                if tx.txfrom in stake_txn:
                    logger.debug("Transfer Token Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                    tx.txfrom].is_active:
                    logger.debug("Transfer Token Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Transfer Token Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Transfer Amount %s',
                                   address_txn[tx.txfrom].balance,
                                   tx.fee)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if bin2hstr(tx.token_txhash).encode() not in address_txn[tx.txfrom].tokens:
                    logger.warning('%s doesnt own any token with token_txnhash %s', tx.txfrom,
                                   bin2hstr(tx.token_txhash).encode())
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()] < tx.amount:
                    logger.warning('Token Transfer amount exceeds available token')
                    logger.warning('Token Txhash %s', bin2hstr(tx.token_txhash).encode())
                    logger.warning('Available Token Amount %s',
                                   address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()])
                    logger.warning('Transaction Amount %s', tx.amount)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                transfer_token_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.LATTICE:
                if tx.txfrom in stake_txn:
                    logger.debug("Lattice Txn dropped: ST txn has been accepted for %s address", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                    tx.txfrom].is_active:
                    logger.debug("Lattice Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Lattice Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Transfer Amount %s',
                                   address_txn[tx.txfrom].balance,
                                   tx.fee)
                    del t_pool2[txnum]
                    total_txn -= 1
                    continue

                # TODO: Review If to add the check for new txn, but duplicate lattice keys by the same address
                lattice_public_key_txn.add(tx.txfrom)

            if tx.subtype == qrl_pb2.Transaction.TRANSFER:
                address_txn[tx.txfrom].balance -= tx.amount + tx.fee

            if tx.subtype in (qrl_pb2.Transaction.MESSAGE,
                              qrl_pb2.Transaction.TOKEN,
                              qrl_pb2.Transaction.TRANSFERTOKEN,
                              qrl_pb2.Transaction.LATTICE):
                address_txn[tx.txfrom].balance -= tx.fee

            if tx.subtype == qrl_pb2.Transaction.TOKEN:
                for initial_balance in tx.initial_balances:
                    address_txn[initial_balance.address].tokens[bin2hstr(tx.txhash).encode()] += initial_balance.amount

            if tx.subtype == qrl_pb2.Transaction.TRANSFERTOKEN:
                address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()] -= tx.amount
                if tx.txto not in address_txn:
                    address_txn[tx.txto] = self.get_stxn_state(last_block_number + 1, tx.txto)
                address_txn[tx.txto].tokens[bin2hstr(tx.token_txhash).encode()] += tx.amount

            if tx.subtype in (qrl_pb2.Transaction.TRANSFER, qrl_pb2.Transaction.COINBASE):
                if tx.txto not in address_txn:
                    address_txn[tx.txto] = self.get_stxn_state(last_block_number + 1, tx.txto)
                address_txn[tx.txto].balance += tx.amount

            tx.set_ots_key(address_txn, tx.txfrom, tx.ots_key)
            self.buffered_chain.tx_pool.add_tx_to_pool(tx)
            tx_nonce[tx.txfrom] += 1
            tx._data.nonce = self.get_stxn_state(last_block_number + 1, tx.txfrom).nonce + tx_nonce[
                tx.txfrom]
            txnum += 1

        # create the block..
        block_obj = self.buffered_chain.create_block(reveal_hash, last_block_number)

        # reset the pool back
        # FIXME: Reset pool from here?
        self.buffered_chain.tx_pool.transaction_pool = copy.deepcopy(t_pool2)

        return block_obj

    def stake_list_get(self, blocknumber):
        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if blocknumber - 1 > self.buffered_chain.height:
                return None

            # FIXME: Avoid +1/-1, assign a them to make things clear
            if blocknumber - 1 == self.buffered_chain._chain.height or blocknumber <= 1:
                return self.buffered_chain._chain.pstate.stake_validators_tracker.sv_dict

            if blocknumber - 1 not in self.buffered_chain.blocks and blocknumber == self.buffered_chain._chain.height:
                return self.buffered_chain._chain.pstate.prev_stake_validators_tracker.sv_dict

            return self.buffered_chain.blocks[blocknumber - 1].stake_validators_tracker.sv_dict
        except KeyError:
            self.buffered_chain.error_msg('stake_list_get', blocknumber)
        except Exception as e:
            self.buffered_chain.error_msg('stake_list_get', blocknumber, e)

        return None

    def future_stake_addresses(self, blocknumber):
        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if blocknumber - 1 == self.buffered_chain._chain.height:
                return self.buffered_chain._chain.pstate.stake_validators_tracker.future_stake_addresses

            return self.buffered_chain.blocks[blocknumber - 1].stake_validators_tracker.future_stake_addresses
        except KeyError:
            self.buffered_chain.error_msg('stake_list_get', blocknumber)
        except Exception as e:
            self.buffered_chain.error_msg('stake_list_get', blocknumber, e)

        return None

    def get_stake_validators_tracker(self, block_idx: int) -> Optional[StakeValidatorsTracker]:
        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if block_idx - 1 == self.buffered_chain._chain.height or block_idx == 0:
                return self.buffered_chain._chain.pstate.stake_validators_tracker

            if block_idx - 1 not in self.buffered_chain.blocks and block_idx == self.buffered_chain._chain.height:
                return self.buffered_chain._chain.pstate.prev_stake_validators_tracker

            return self.buffered_chain.blocks[block_idx - 1].stake_validators_tracker
        except KeyError:
            self.buffered_chain.error_msg('get_stake_validators_tracker', block_idx)
        except Exception as e:
            self.buffered_chain.error_msg('get_stake_validators_tracker', block_idx, e)

        return None

    def get_stxn_state(self, blocknumber, addr) -> Optional[AddressState]:
        try:
            if self.buffered_chain._chain.height == 0 and blocknumber == 0:
                address_state = self.buffered_chain._chain.pstate.get_address(addr)
                return address_state

            # FIXME: Simplify this - self.blocks[blocknumber - 1][1] is a StateBuffer
            if blocknumber - 1 == self.buffered_chain._chain.height or addr not in self.buffered_chain.blocks[
                blocknumber - 1].address_state_dict:
                address_state = self.buffered_chain._chain.pstate.get_address(addr)
                return address_state

            if addr in self.buffered_chain.blocks[blocknumber - 1].address_state_dict:
                return copy.deepcopy(
                    self.buffered_chain.blocks[blocknumber - 1].address_state_dict[addr])  # FIXME: Why deepcopy?

            return self.buffered_chain._chain.pstate.get_address(addr)

        except KeyError:
            self.buffered_chain.error_msg('get_stxn_state', blocknumber)
        except Exception as e:
            self.buffered_chain.error_msg('get_stxn_state', blocknumber, e)

        return None
