# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
import copy
from collections import defaultdict
from typing import Optional, Dict

from pyqrllib.pyqrllib import bin2hstr

from qrl.core import config, State
from qrl.core.misc import logger
from qrl.core.AddressState import AddressState
from qrl.core.Block import Block
from qrl.core.BlockMetadata import BlockMetadata
from qrl.core.Chain import Chain
from qrl.core.GenesisBlock import GenesisBlock
from qrl.core.EphemeralMessage import EncryptedEphemeralMessage
from qrl.core.StakeValidatorsTracker import StakeValidatorsTracker
from qrl.core.TokenMetadata import TokenMetadata
from qrl.core.Transaction import CoinBase, Transaction
from qrl.core.TransactionPool import TransactionPool
from qrl.crypto.hashchain import hashchain
from qrl.crypto.misc import sha256
from qrl.crypto.xmss import XMSS
from qrl.core.formulas import score, calc_seed
from qrl.generated import qrl_pb2, qrllegacy_pb2


# TODO: Rename to unstable/fluid chain or something similar?
class BufferedChain:
    def __init__(self, chain: Chain):
        self._chain = chain

        self.blocks = dict()  # FIXME: Using a dict is very inefficient when index are a sequence

        self.epoch = max(0, self._chain.height) // config.dev.blocks_per_epoch  # Main chain epoch
        self.epoch_seed = None
        if self._chain.height > 0:
            self.epoch = self._chain.blockchain[-1].block_number // config.dev.blocks_per_epoch

        self.tx_pool = TransactionPool()  # FIXME: This is not stable, it should not be in chain

        self.stake_list = []

    @property
    def transaction_pool(self):
        return self.tx_pool.transaction_pool

    @property
    def length(self):
        return len(self._chain.blockchain)

    @property
    def height(self) -> int:
        if len(self.blocks) == 0:
            return self._chain.height
        return max(self.blocks.keys())  # FIXME: max over a dictionary?

    @property
    def pstate(self) -> State:
        return self._chain.pstate

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################
    # Block handling

    def get_address_state(self, address: bytes):
        address_state = None
        for block_idx in self.blocks:
            address_state_dict = self.blocks[block_idx].address_state_dict
            if address in address_state_dict:
                address_state = address_state_dict[address]._data

        if address_state:
            return address_state

        tmp_address_state = self.pstate.get_address(address)
        transaction_hashes = self.pstate.get_address_tx_hashes(address)

        address_state = qrl_pb2.AddressState(address=tmp_address_state.address,
                                             balance=tmp_address_state.balance,
                                             nonce=tmp_address_state.nonce,
                                             ots_bitfield=tmp_address_state.ots_bitfield,
                                             transaction_hashes=transaction_hashes,
                                             tokens=tmp_address_state.tokens)

        return address_state

    def get_last_block(self) -> Optional[Block]:
        if len(self.blocks) == 0:
            return self._chain.get_last_block()

        # FIXME: Should this run max on the keys only? Or better keep track of the value..
        last_blocknum = max(self.blocks.keys())
        return self.blocks[last_blocknum].block

    def get_block(self, block_idx: int) -> Optional[Block]:
        if block_idx in self.blocks:
            return self.blocks[block_idx].block
        return self._chain.get_block(block_idx)

    def get_blockidx_from_txhash(self, transaction_hash) -> Optional[Transaction]:
        answer = self._chain.pstate.get_tx_metadata(transaction_hash)
        if answer is not None:
            _, block_index = answer
            return block_index

        for block_idx in self.blocks:
            if self.blocks[block_idx].contains_txn(transaction_hash):
                return block_idx

        return None

    def get_transaction(self, transaction_hash) -> Optional[Transaction]:
        for tx in self.tx_pool.transaction_pool:
            if tx.txhash == transaction_hash:
                return tx

        for block_idx in self.blocks:
            tx = self.blocks[block_idx].get_txn(transaction_hash)
            if tx:
                return tx

        return self._chain.get_transaction(transaction_hash)

    def _move_to_mainchain(self, stake_validators_tracker: StakeValidatorsTracker) -> bool:
        if len(self.blocks) == 0:
            return True

        # FIXME: Simplify condition
        while len(self.blocks) > 0 and \
                (len(self.blocks) > config.dev.reorg_limit or min(self.blocks.keys()) == 0):

            # FIXME: self.blocks why a dict instead of a deque?
            block_idx = min(self.blocks.keys())
            block = self.blocks[block_idx].block
            next_seed = self.blocks[block_idx].next_seed
            address_state_dict = self.blocks[block_idx].address_state_dict

            if not self._add_block_mainchain(block, address_state_dict, stake_validators_tracker, next_seed):
                logger.info('Block {0} adding to stable chain failed'.format(block.block_number))
                return False

            if block.stake_selector == self.staking_address:
                logger.info('You won Block #%s!!!!!!!!!', block.block_number)

            # modify fn to keep transaction in memory till reorg
            self.tx_pool.remove_tx_in_block_from_pool(block)

            self.epoch_seed = self.blocks[block_idx].next_seed
            self.epoch = int(block_idx // config.dev.blocks_per_epoch)
            self._clean_if_required(block_idx)
            del self.blocks[block_idx]

        return True

    def _add_block_mainchain(self, block, address_state_dict, stake_validators_tracker, next_seed) -> bool:
        slave_xmss = self.get_slave_xmss(block.block_number)

        if not self._chain.add_block(block, address_state_dict, stake_validators_tracker, next_seed, slave_xmss):
            logger.info("buff: Block {}. Add_block failed. Requesting again".format(block.block_number))
            self._validate_tx_pool()
            return False

        self.tx_pool.remove_tx_in_block_from_pool(block)

        # FIXME: clean this up
        block_left = config.dev.blocks_per_epoch
        block_left -= block.block_number - (block.epoch * config.dev.blocks_per_epoch)

        if block_left == 1:
            private_seed = self.wallet.address_bundle[0].xmss.get_seed_private()
            self._wallet_private_seeds[block.epoch + 1] = private_seed
            self.hash_chain[block.epoch + 1] = hashchain(private_seed, epoch=block.epoch + 1).hashchain

        self._clean_if_required(block.block_number)

        self.epoch_seed = sha256(block.reveal_hash + self.epoch_seed)

        self.epoch = block.epoch
        return True

    def add_lattice_public_key(self, lattice_public_key_txn):
        self._chain.pstate.put_lattice_public_key(lattice_public_key_txn)

    def add_ephemeral_message(self, encrypted_ephemeral: EncryptedEphemeralMessage):
        self._chain.pstate.update_ephemeral(encrypted_ephemeral)

    def get_token_metadata(self, token_txnhash) -> TokenMetadata:
        return self._chain.pstate.get_token_metadata(token_txnhash)

    def _validate_tx_pool(self):
        result = True

        # FIXME: Breaks encapsulation
        for tx in self.tx_pool.transaction_pool:
            if not tx.validate():
                result = False
                self.tx_pool.remove_tx_from_pool(tx)
                logger.info(('invalid tx: ', tx, 'removed from pool'))
                continue

            # FIXME: reference to a buffer
            tx_state = self.get_stxn_state(blocknumber=self.height + 1, addr=tx.txfrom)

            if not tx.validate_extended(tx_state=tx_state, transaction_pool=self.tx_pool.transaction_pool):
                result = False
                logger.warning('tx %s failed', tx.txhash)
                self.tx_pool.remove_tx_from_pool(tx)

        return result

    def initialize_chain(self, genesis_block):
        # Initializes Required variables before adding blocknumber 1
        seed_list = []

        for raw_tx in genesis_block.transactions:
            tx = Transaction.from_pbdata(raw_tx)
            if not tx.validate():
                return False
            if tx.subtype == qrl_pb2.Transaction.STAKE:
                for genesisBalance in genesis_block.genesis_balance:
                    if tx.txfrom == genesisBalance.address.encode() and tx.activation_blocknumber == 1:
                        seed_list.append(tx.hash)
                        # FIXME: This goes to stake validator list without verification, Security Risk
                        self._chain.pstate.stake_validators_tracker.add_sv(genesisBalance.balance, tx, 1)

        self.epoch_seed = calc_seed(seed_list)
        return True

    def get_genesis_total_stake(self):
        genesis_block = self.get_block(0)
        total = 0
        for genesisBalance in genesis_block.genesis_balance:
            total += genesisBalance.balance
        return total

    def add_block(self, block: Block) -> bool:

        # is there an older version available?
        old_block_metadata = None
        if block.block_number in self.blocks:
            old_block_metadata = self.blocks[block.block_number]

        if block.block_number < self._chain.height:
            return False

        if not self.validate_block(block):  # This is here because of validators, etc
            logger.info('Block validation failed')
            logger.info('Block #%s', block.block_number)
            logger.info('Stake_selector %s', block.stake_selector)
            return False

        same_block = self.get_block(block.block_number)
        if same_block is not None and block.headerhash == same_block.headerhash:
            logger.debug('Block {}. already received'.format(block.block_number))
            return False

        prev_block = self.get_block(block.block_number - 1)

        if prev_block is None:
            logger.warning('Prev_block is not available. Block {} rejected'.format(block.block_number))
            return False

        if prev_block.headerhash != block.prev_headerhash:
            logger.warning('buff: Block {} rejected. prev_block is not available.'.format(block.block_number))
            return False

        block_balance = self._get_st_balance_by_slave_pk(slave_public_key=block.transactions[0].public_key,
                                                         block_number=block.block_number)

        if block_balance is None:
            logger.warning('Rejected block #%s block_balance None for %s',
                           block.block_number,
                           block.transactions[0].public_key)
            return False

        if self.epoch_seed is None:
            # FIXME: A proper epoch seed should be already available for the genesis block
            logger.error("epoch seed is None for the genesis block!!!!!")
            self.epoch_seed = sha256(b'INVALID_EPOCH_SEED')

        # Prepare Metadata inputs

        if self._chain.height + 1 == block.block_number:
            prev_sv_tracker = copy.deepcopy(self._chain.pstate.stake_validators_tracker)
            address_state_dict = dict()
            hash_chain = None
            seed = self.epoch_seed
        else:
            prev_block_metadata = self.blocks[block.block_number - 1]
            prev_sv_tracker = copy.deepcopy(prev_block_metadata.stake_validators_tracker)
            address_state_dict = copy.deepcopy(prev_block_metadata.address_state_dict)
            hash_chain = copy.deepcopy(prev_block_metadata.hash_chain)
            seed = prev_block_metadata.next_seed

        if not self._state_add_block_buffer(block, prev_sv_tracker, address_state_dict):
            logger.warning('State_validate_block failed inside chainbuffer #%s', block.block_number)
            return False

        block_metadata = BlockMetadata(block=block,
                                       hash_chain=hash_chain,
                                       epoch_seed=seed,
                                       balance=block_balance)

        block_metadata.stake_validators_tracker = prev_sv_tracker
        block_metadata.address_state_dict = address_state_dict
        block_metadata.update_stxn_state(self._chain.pstate)

        # add/replace if new option is better
        if old_block_metadata is None or block_metadata.sorting_key < old_block_metadata.sorting_key:
            self.blocks[block.block_number] = block_metadata
            self._remove_blocks(block.block_number + 1)

        # Move to stable chain if necessary
        return self._move_to_mainchain()

    def _update(self,
                block: Block,
                stake_validators_tracker: StakeValidatorsTracker,
                address_txn: Dict[bytes, AddressState]) -> bool:

        if block.block_number > 0:
            if block.stake_selector not in stake_validators_tracker.sv_dict:
                logger.warning('stake selector not in stake_list_get')
                return False

            if stake_validators_tracker.sv_dict[block.stake_selector].is_banned:
                logger.warning('stake selector is in banned list')
                return False

            if not stake_validators_tracker.sv_dict[block.stake_selector].is_active:
                logger.warning('stake selector is in inactive')
                return False

        # FIX ME : Temporary fix, to include only either ST txn or TransferCoin txn for an address
        stake_txn = set()
        transfercoin_txn = set()
        destake_txn = set()
        message_txn = set()
        token_txn = set()
        transfer_token_txn = set()
        lattice_public_key_txn = set()

        # cycle through every tx in the new block to check state
        for protobuf_tx in block.transactions:
            # FIXME: Simplify this.. too complex. delegate to objects, etc.

            tx = Transaction.from_pbdata(protobuf_tx)
            if tx.subtype == qrl_pb2.Transaction.COINBASE:
                sv = stake_validators_tracker.sv_dict[self._get_st_address(tx.PK, block.block_number)]
                expected_nonce = sv.nonce + 1
            else:
                expected_nonce = address_txn[tx.txfrom].nonce + 1

            if tx.nonce != expected_nonce:
                logger.warning('nonce incorrect, invalid tx')
                logger.warning('subtype: %s', tx.subtype)
                logger.warning('%s actual: %s expected: %s', tx.txfrom, tx.nonce, expected_nonce)
                return False

            # TODO: To be fixed later
            state_addr = self.get_stxn_state(block.block_number, tx.addr_from)
            if tx.ots_key_reuse(state_addr, tx.ots_key):
                logger.warning('pubkey reuse detected: invalid tx %s', tx.txhash)
                logger.warning('subtype: %s', tx.subtype)
                return False

            if tx.subtype == qrl_pb2.Transaction.TRANSFER:
                if tx.txfrom in stake_txn:
                    logger.warning("Transfer coin done by %s address is a Stake Validator", tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                        tx.txfrom].is_active:
                    logger.warning("Source address is a Stake Validator, balance is locked while staking")
                    return False

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.warning("Source address is in Future Stake Validator List, balance is locked")
                    return False

                if address_txn[tx.txfrom].balance < tx.amount:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Transfer Amount %s', address_txn[tx.txfrom].balance, tx.amount)
                    return False

                transfercoin_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.STAKE:
                if tx.txfrom in (transfercoin_txn, message_txn, token_txn, transfer_token_txn):
                    logger.warning('Block cannot have both st txn & %s txn from same address %s', tx.subtype, tx.txfrom)
                    return False
                if tx.txfrom in stake_txn:
                    logger.warning('Block cannot have multiple Stake Txn from same address %s', tx.txfrom)
                    return False
                if tx.txfrom in destake_txn:
                    logger.warning('Block may not have both Stake and Destake txn of same address %s', tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict:
                    expiry = stake_validators_tracker.sv_dict[tx.txfrom].activation_blocknumber + \
                        config.dev.blocks_per_epoch

                    if block.block_number > 1 and tx.activation_blocknumber < expiry:
                        logger.warning('Failed %s is already active for the given range', tx.txfrom)
                        return False

                    activation_limit = block.block_number + config.dev.blocks_per_epoch + 1

                    if tx.activation_blocknumber > activation_limit:
                        logger.warning('Failed %s activation_blocknumber beyond limit', tx.txfrom)
                        logger.warning('Found %s', tx.activation_blocknumber)
                        logger.warning('Must be less than %s', tx.activation_limit)
                        return False

                future_stake_addresses = stake_validators_tracker.future_stake_addresses

                if tx.txfrom not in future_stake_addresses:
                    if tx.txfrom in address_txn:
                        balance = address_txn[tx.txfrom].balance
                    else:
                        balance = self._chain.pstate._get_address_state(tx.txfrom).balance

                    stake_validators_tracker.add_sv(balance, tx, block.block_number)

                stake_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.DESTAKE:
                if tx.txfrom in stake_txn:
                    logger.warning('Block may not have both Destake and Stake txn of same address %s', tx.txfrom)
                    return False

                if tx.txfrom in destake_txn:
                    logger.warning('Block cannot have multiple Destake Txn from same address %s', tx.txfrom)
                    return False

                if tx.txfrom not in stake_validators_tracker.sv_dict and tx.txfrom not in stake_validators_tracker.future_stake_addresses:
                    logger.warning('Failed due to destake %s is not a stake validator', tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict:
                    stake_validators_tracker.sv_dict[tx.txfrom]._is_active = False

                if tx.txfrom in stake_validators_tracker.future_stake_addresses:
                    stake_validators_tracker.future_stake_addresses[tx.txfrom]._is_active = False

                destake_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.MESSAGE:
                if tx.txfrom in stake_txn:
                    logger.warning("Message Txn done by %s address is a Stake Validator", tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                        tx.txfrom].is_active:
                    logger.warning("Source address is a Stake Validator, balance is locked while staking")
                    logger.warning("Message Txn dropped")
                    return False

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.warning("Source address is in Future Stake Validator List, balance is locked")
                    logger.warning("Message Txn dropped")
                    return False

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid message tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Free %s', address_txn[tx.txfrom].balance, tx.fee)
                    return False

                message_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.TOKEN:
                if tx.txfrom in stake_txn:
                    logger.warning("Token Transaction done by %s address is a Stake Validator", tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                        tx.txfrom].is_active:
                    logger.warning("Source address is a Stake Validator, balance is locked while staking")
                    logger.warning("Token Txn dropped")
                    return False

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.warning("Source address is in Future Stake Validator List, balance is locked")
                    logger.warning("Token Txn dropped")
                    return False

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid Token tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Fee %s',
                                   address_txn[tx.txfrom].balance,
                                   tx.fee)
                    return False

                token_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.TRANSFERTOKEN:
                if tx.txfrom in stake_txn:
                    logger.warning("Transfer Token Transaction done by %s address is a Stake Validator", tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                        tx.txfrom].is_active:
                    logger.warning("Source address is a Stake Validator, balance is locked while staking")
                    logger.warning("Transfer Token Txn dropped")
                    return False

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.warning("Source address is in Future Stake Validator List, balance is locked")
                    logger.warning("Transfer Token Txn dropped")
                    return False

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid Transfer Token Txn', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Free %s', address_txn[tx.txfrom].balance, tx.fee)
                    return False

                if bin2hstr(tx.token_txhash).encode() not in address_txn[tx.txfrom].tokens:
                    logger.warning('%s doesnt own any token with token_txnhash %s', tx.txfrom,
                                   bin2hstr(tx.token_txhash).encode())
                    return False

                if address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()] < tx.amount:
                    logger.warning('Token Transfer amount exceeds available token')
                    logger.warning('Token Txhash %s', tx.token_txhash)
                    logger.warning('Available Token Amount %s',
                                   address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()])
                    logger.warning('Transaction Amount %s', tx.amount)
                    return False

                transfer_token_txn.add(tx.txfrom)

            elif tx.subtype == qrl_pb2.Transaction.LATTICE:
                if tx.txfrom in stake_txn:
                    logger.debug("Lattice Txn dropped: ST txn has been accepted for %s address", tx.txfrom)
                    return False

                if tx.txfrom in stake_validators_tracker.sv_dict and stake_validators_tracker.sv_dict[
                        tx.txfrom].is_active:
                    logger.debug("Lattice Txn dropped: %s address is a Stake Validator", tx.txfrom)
                    return False

                if (tx.txfrom in stake_validators_tracker.future_stake_addresses and
                        stake_validators_tracker.future_stake_addresses[tx.txfrom].is_active):
                    logger.debug("Lattice Txn dropped: %s address is in Future Stake Validator", tx.txfrom)
                    return False

                if address_txn[tx.txfrom].balance < tx.fee:
                    logger.warning('%s %s exceeds balance, invalid tx', tx, tx.txfrom)
                    logger.warning('subtype: %s', tx.subtype)
                    logger.warning('Buffer State Balance: %s  Transfer Amount %s',
                                   address_txn[tx.txfrom].balance,
                                   tx.fee)
                    return False

                # TODO: Review If to add the check for new txn, but duplicate lattice keys by the same address
                lattice_public_key_txn.add(tx.txfrom)

            if tx.subtype != qrl_pb2.Transaction.COINBASE:
                address_txn[tx.txfrom].increase_nonce()

            if tx.subtype == qrl_pb2.Transaction.TRANSFER:
                address_txn[tx.txfrom].balance -= tx.amount + tx.fee

            if tx.subtype in (qrl_pb2.Transaction.MESSAGE,
                              qrl_pb2.Transaction.TOKEN,
                              qrl_pb2.Transaction.TRANSFERTOKEN,
                              qrl_pb2.Transaction.LATTICE):
                address_txn[tx.txfrom].balance -= tx.fee

            if tx.subtype == qrl_pb2.Transaction.LATTICE:
                address_txn[tx.txfrom].add_lattice_pk(tx)

            if tx.subtype == qrl_pb2.Transaction.TOKEN:
                for initial_balance in tx.initial_balances:
                    address_txn[initial_balance.address].tokens[bin2hstr(tx.txhash).encode()] += initial_balance.amount

            if tx.subtype == qrl_pb2.Transaction.TRANSFERTOKEN:
                address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()] -= tx.amount
                #  Remove Token from address_state when token balance is Zero
                if address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()] == 0:
                    del address_txn[tx.txfrom].tokens[bin2hstr(tx.token_txhash).encode()]
                address_txn[tx.txto].tokens[bin2hstr(tx.token_txhash).encode()] += tx.amount

            if tx.subtype in (qrl_pb2.Transaction.TRANSFER, qrl_pb2.Transaction.COINBASE):
                address_txn[tx.txto].balance += tx.amount

            tx.set_ots_key(address_txn, tx.txfrom, tx.ots_key)
            address_txn[tx.txfrom].transaction_hashes.append(tx.txhash)

        return True

    def remove_last_buffer_block(self):
        last_block_number = self.height
        self._remove_blocks(last_block_number)

    def _remove_blocks(self, starting_blocknumber: int):
        if starting_blocknumber not in self.blocks:
            return

        while starting_blocknumber in self.blocks:
            del self.blocks[starting_blocknumber]
            starting_blocknumber += 1

    def load_address_state(self, block: Block,
                           address_txn: Dict[bytes, AddressState]) -> Dict[bytes, AddressState]:

        for protobuf_tx in block.transactions:
            tx = Transaction.from_pbdata(protobuf_tx)
            if block.block_number == 0:
                # balance = 0
                for genesisBalance in block.genesis_balance:
                    if genesisBalance.address.encode() == tx.addr_from:
                        stake_balance = genesisBalance.balance
                        address_state = AddressState.create(address=tx.addr_from,
                                                            nonce=0,
                                                            balance=stake_balance,
                                                            ots_bitfield=[b'\x00'] * config.dev.ots_bitfield_size,
                                                            tokens=dict())
                        address_txn[tx.addr_from] = address_state
                continue
            if tx.txfrom not in address_txn:
                # FIXME: Access to chain buffer from here
                address_txn[tx.txfrom] = self.get_stxn_state(block.block_number, tx.txfrom)

            if tx.subtype in (qrl_pb2.Transaction.TRANSFER,
                              qrl_pb2.Transaction.COINBASE,
                              qrl_pb2.Transaction.TRANSFERTOKEN):
                if tx.txto not in address_txn:
                    # FIXME: Access to chain buffer from here
                    address_txn[tx.txto] = self.get_stxn_state(block.block_number, tx.txto)

            if tx.subtype == qrl_pb2.Transaction.TOKEN:
                if tx.owner not in address_txn:
                    address_txn[tx.owner] = self.get_stxn_state(block.block_number, tx.owner)
                for initial_balance in tx.initial_balances:
                    if initial_balance.address not in address_txn:
                        address_txn[initial_balance.address] = self.get_stxn_state(block.block_number,
                                                                                   initial_balance.address)

        # FIXME: Modifying input. Side effect, etc.
        return address_txn

    # Returns the number of blocks left before next epoch
    @staticmethod
    def get_blocks_left(blocknumber: int) -> int:
        epoch = blocknumber // config.dev.blocks_per_epoch
        blocks_left = blocknumber - (epoch * config.dev.blocks_per_epoch)
        blocks_left = config.dev.blocks_per_epoch - blocks_left
        return blocks_left

    def _state_add_block_buffer(self,
                                block: Block,
                                stake_validators_tracker: StakeValidatorsTracker,
                                address_state_dict: Dict[bytes, AddressState]):

        # FIXME: This is mixing states
        address_state_dict = self.load_address_state(block, address_state_dict)

        is_successful = self._update(block, stake_validators_tracker, address_state_dict)

        if is_successful:
            if block.block_number > 0:
                stake_validators_tracker.increase_nonce(block.stake_selector)

                for dup_tx in block.duplicate_transactions:
                    if dup_tx.coinbase1.txto in stake_validators_tracker.sv_dict:
                        # FIXME: Setting the property is invalid
                        stake_validators_tracker.sv_dict[dup_tx.coinbase1.txto]._is_banned = True

                if self.get_blocks_left(block.block_number) == 1:
                    # UPDATE HASHCHAIN
                    epoch = int((block.block_number + 1) // config.dev.blocks_per_epoch)
                    logger.info('Created new hash chain')

                    prev_private_seed = self.wallet.address_bundle[0].xmss.get_seed_private()
                    self._wallet_private_seeds[epoch] = prev_private_seed
                    self.hash_chain[epoch] = hashchain(prev_private_seed, epoch=epoch).hashchain

            stake_validators_tracker.update_sv(block.block_number)

            logger.info('[ChainBuffer] Block #%s added  stake: %s', block.block_number, block.stake_selector)

        return is_successful

    def create_block(self,
                     reveal_hash: bytes,
                     sv_dict: StakeValidatorsTracker,
                     last_block_number: int = -1) -> Optional[Block]:

        # FIXME: This can probably happen inside get_block, why two methods?
        if last_block_number == -1:
            last_block = self.get_last_block()
        else:
            last_block = self.get_block(last_block_number)

        signing_xmss = self.get_slave_xmss(last_block.block_number + 1)
        nonce = sv_dict[self.staking_address].nonce + 1

        new_block = Block.create(staking_address=self.staking_address,
                                 block_number=last_block.block_number + 1,
                                 reveal_hash=reveal_hash,
                                 prevblock_headerhash=last_block.headerhash,
                                 transactions=self.tx_pool.transaction_pool,
                                 duplicate_transactions=self.tx_pool.duplicate_tx_pool,
                                 signing_xmss=signing_xmss,
                                 nonce=nonce)

        slave_xmss = self.get_slave_xmss(last_block.block_number + 1)

        if not slave_xmss:
            return None  # FIXME: Not clear why the skip and return False

        # FIXME: Why is it necessary to access the wallet here? Unexpected side effect?
        self.wallet.save_slave(slave_xmss)

        return new_block

    def validate_block(self, block: Block, stake_validators_tracker: StakeValidatorsTracker) -> bool:
        """
        Checks validity of a new block
        """
        try:
            # FIXME: review this.. Too complicated
            last_block = self.get_block(block.block_number - 1)

            if last_block is not None:  # FIXME: Review this
                if not block.blockheader.validate(last_block.blockheader):
                    return False

            if last_block is None and block.block_number == 0:
                return block == self.get_block(0)

            if len(block.transactions) == 0:
                logger.warning('BLOCK : There must be atleast 1 txn')
                return False

            # Validate coinbase
            # FIXME: Check if it is possible to delegate validation to coinbase transaction. Why the code is in Block?
            coinbase_tx = Transaction.from_pbdata(block.transactions[0])

            if coinbase_tx.subtype != qrl_pb2.Transaction.COINBASE:
                logger.warning('BLOCK : First txn must be a COINBASE txn')
                return False

            if coinbase_tx.txto != block.blockheader.stake_selector:
                logger.info('Non matching txto and stake_selector')
                logger.info('txto: %s stake_selector %s', coinbase_tx.txfrom, block.stake_selector)
                return False

            if coinbase_tx.amount != block.blockheader.block_reward + block.blockheader.fee_reward:
                logger.info('Block_reward doesnt match')
                logger.info('Found: %s', coinbase_tx.amount)
                logger.info('Expected: %s', block.blockheader.block_reward + block.blockheader.fee_reward)
                logger.info('block_reward: %s', block.blockheader.block_reward)
                logger.info('fee_reward: %s', block.blockheader.fee_reward)
                return False

            if coinbase_tx.txto not in stake_validators_tracker.sv_dict:
                logger.warning('Stake selector not in stake_list for this epoch..')
                return False

            if not stake_validators_tracker.validate_hash(block.reveal_hash,
                                                          block.block_number,
                                                          coinbase_tx.txto):
                logger.warning('Supplied hash does not iterate to terminator: failed validation')
                return False

            if not self._validate_txs_in_block(block):
                logger.warning('Block validate_tx_in_block error: failed validation')
                return False

        except Exception as e:
            logger.exception(e)
            return False

        return True

    def _validate_txs_in_block(self, block: Block) -> bool:
        # FIXME: This is accessing buffered chain. It does not belong here
        # Validating coinbase txn

        # FIXME: Again checking coinbase here?
        coinbase_txn = CoinBase(block.transactions[0])

        sv_dict = self.stake_list_get(block.block_number)
        valid = coinbase_txn.validate_extended(sv_dict=sv_dict, blockheader=block.blockheader)

        if not valid:
            logger.warning('coinbase txn in block failed')
            return False

        for tx_pbdata in block.transactions[1:]:
            tx = Transaction.from_pbdata(tx_pbdata)
            if not tx.validate():
                logger.warning('invalid tx in block')
                return False

        for tx_pbdata in block.duplicate_transactions:
            tx = Transaction.from_pbdata(tx_pbdata)
            if not tx.validate():
                logger.warning('invalid duplicate tx in block')
                return False

        return True

    def get_block_score(self, blocknumber) -> Optional[int]:
        try:
            return self.blocks[blocknumber].score
        except KeyError:
            logger.error('get_block_n_score, blocknumber not in self.blocks #%s', blocknumber)
        except Exception as e:
            logger.error('Unexpected Exception')
            logger.error('%s', e)
        return None

    def bkmr_tracking_blocknumber(self, ntp):
        last_block = self.get_last_block()

        if ntp.getTime() - last_block.timestamp >= config.dev.minimum_minting_delay - config.dev.timestamp_error:
            return last_block.block_number + 1

        return last_block.block_number

    def verify_BK_hash(self,
                       mr_data: qrllegacy_pb2.MRData,
                       conn_identity,
                       stake_validators_tracker: StakeValidatorsTracker) -> bool:
        slave_public_key = mr_data.stake_selector
        prev_headerhash = mr_data.prev_headerhash

        if mr_data.block_number <= self._chain.height:
            return False

        if not stake_validators_tracker:
            return False

        str_slave_public_key = bin2hstr(slave_public_key)

        if str_slave_public_key not in stake_validators_tracker.slave_public_key_dict:
            return False

        stake_validator_address = stake_validators_tracker.slave_public_key_dict[str_slave_public_key]

        if stake_validators_tracker.sv_dict[stake_validator_address].is_banned:
            logger.warning('Rejecting block created by banned stake selector %s', stake_validator_address)
            return False

        if not stake_validators_tracker.sv_dict[stake_validator_address].is_active:
            logger.warning('Rejecting block created by inactive stake selector %s', stake_validator_address)
            return False

        # FIXME: Avoid +1/-1, assign a them to make things clear
        if mr_data.block_number - 1 == self._chain.height:
            if prev_headerhash != self._chain.blockchain[-1].headerhash:
                logger.warning('verify_BK_hash Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
                return False
            return True
        elif mr_data.block_number - 1 not in self.blocks or prev_headerhash != self.blocks[mr_data.block_number - 1]\
                .block.headerhash:
            logger.warning('verify_BK_hash Failed due to prevheaderhash mismatch, blockslen %d', len(self.blocks))
            return False

        if not stake_validators_tracker.validate_hash(mr_data.reveal_hash,
                                                      mr_data.block_number,
                                                      stake_address=stake_validator_address):
            logger.info('%s reveal doesnt hash to stake terminator reveal %s', conn_identity, mr_data.reveal_hash)
            return False

        score = self.score_BK_hash(mr_data)

        # FIXME: Unclear.. why verify checks ordering?
        return self._is_better_block(mr_data.block_number, score)

    def score_BK_hash(self, block: Block) -> int:
        seed = self._get_epoch_seed(block.block_number)

        balance = self._get_st_balance_by_slave_pk(slave_public_key=block.stake_selector,
                                                   block_number=block.block_number)

        # FIXME: Duplicated code
        return score(stake_address=block.stake_selector,
                     reveal_one=block.reveal_hash,
                     balance=balance,
                     seed=seed)

    def _is_better_block(self, block_idx: int, score: float)->bool:
        if block_idx not in self.blocks:
            return True

        oldscore = self.blocks[block_idx].score

        if score < oldscore:
            return True

        return False

    def is_duplicate_block(self,
                           block_idx: int,
                           prev_headerhash: bytes,
                           stake_selector)->bool:
        """
        A block is considered as a dirty block, if same stake validator created two different blocks
        for the same blocknumber having same prev_blockheaderhash.
        """
        if block_idx > self.height:
            return False

        best_block = self.get_block(block_idx)

        if best_block.prev_headerhash != prev_headerhash:
            return False

        if best_block.stake_selector != stake_selector:
            return False

        return True

    def _clean_if_required(self, block_idx):
        """
        Checks if the mining data such as private_seeds, hash_chain, slave_xmss
        are no more required.
        :param block_idx:
        :return:
        """
        prev_epoch = int((block_idx - 1) // config.dev.blocks_per_epoch)

        if prev_epoch != self.epoch:
            self._clean_mining_data(block_idx - 1)

    #############################################
    #############################################
    #############################################
    #############################################
    #############################################
    # TODO: Persistence will move to rocksdb

    def load(self):
        logger.info('Reading chain..')

        # TODO: Persistence will move to rocksdb
        self._chain.blockchain = []

        if self._chain.load_state():
            state_block_number = self._chain.pstate.get_state_version()
            self.epoch = state_block_number // config.dev.blocks_per_epoch
            self.epoch_seed = self._chain.pstate.get_next_seed()
            private_seed = self.wallet.address_bundle[0].xmss.get_seed_private()
            self._wallet_private_seeds = {self.epoch: private_seed}
            mining_address = self.wallet.address_bundle[0].address
            if mining_address in self._chain.pstate.stake_validators_tracker.sv_dict:
                activation_blocknumber = self._chain.pstate.stake_validators_tracker.sv_dict[mining_address].activation_blocknumber
            else:
                activation_blocknumber = state_block_number
            slave_epoch = activation_blocknumber // config.dev.blocks_per_epoch
            self.hash_chain[slave_epoch] = hashchain(private_seed, slave_epoch).hashchain
            data = self._chain.pstate.get_slave_xmss()
            if data:
                self.slave_xmss[slave_epoch] = XMSS(config.dev.slave_xmss_height, seed=data[1])
                self.slave_xmss[slave_epoch].set_index(data[0])
            return

        return self.genesis_loader(GenesisBlock())

    def genesis_loader(self, genesis_block):

        for genesis_balance in genesis_block.genesis_balance:
            genesis_address = genesis_balance.address.encode()
            address_state = AddressState.create(address=genesis_address,
                                                nonce=config.dev.default_nonce,
                                                balance=genesis_balance.balance,
                                                ots_bitfield=[b'\x00']*config.dev.ots_bitfield_size,
                                                tokens=dict())
            self._chain.pstate._save_address_state(address_state)

        is_success = self.initialize_chain(genesis_block)

        if not is_success:
            return None

        self._chain.blockchain.append(genesis_block)

        logger.info('{} blocks'.format(self.length))
        return self._chain.blockchain

    def error_msg(self, func_name, blocknum, exception=None):
        if exception:
            logger.error(func_name + ' Unknown exception at blocknum: %s', blocknum)
            logger.exception(exception)
            return

        logger.error('%s blocknum not found in blocks %s', func_name, blocknum)
        if self.blocks:
            logger.error('Min block num %s', min(self.blocks))
            logger.error('Max block num %s', max(self.blocks))

    #############################################
    #############################################
    #############################################
    #############################################
    #############################################
    # Hashchain handling

    def hash_chain_get(self, blocknumber):
        epoch = self._get_mining_epoch(blocknumber)
        return self.hash_chain[epoch]

    def select_hashchain(self,
                         stake_address: bytes = None,
                         hash_chain=None,
                         blocknumber=None):

        # NOTE: Users POS / Block

        if not hash_chain:
            for s in self.stake_list_get(blocknumber):
                if s[0] == stake_address:
                    hash_chain = s[1]
                    break

        if not hash_chain:
            return

        return hash_chain

    def _get_epoch_seed(self, blocknumber: int) -> Optional[bytes]:
        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if blocknumber - 1 == self._chain.height:
                return self.epoch_seed

            return self.blocks[blocknumber - 1].next_seed
        except KeyError:
            self.error_msg('get_epoch_seed', blocknumber)
        except Exception as e:
            self.error_msg('get_epoch_seed', blocknumber, e)

        return None

    #############################################
    #############################################
    #############################################
    #############################################
    #############################################
    # Related to staking

    def create_stake_block(self, reveal_hash, last_block_number, stake_validators_tracker) -> Optional[Block]:
        # TODO: Persistence will move to rocksdb
        # FIXME: Difference between this and create block?????????????

        # FIXME: Break encapsulation
        t_pool2 = copy.deepcopy(self.tx_pool.transaction_pool)
        del self.tx_pool.transaction_pool[:]
        ######

        # recreate the transaction pool as in the tx_hash_list, ordered by txhash..
        tx_nonce = defaultdict(int)
        total_txn = len(t_pool2)
        txnum = 0
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
                if tx.activation_blocknumber > self.height + config.dev.blocks_per_epoch + 1:
                    logger.debug('Skipping st as activation_blocknumber beyond limit')
                    logger.debug('Expected # less than : %s', (self.height + config.dev.blocks_per_epoch))
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
                    address_txn[tx.owner] = self.get_stxn_state(last_block_number+1, tx.owner)
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
            self.tx_pool.add_tx_to_pool(tx)
            tx_nonce[tx.txfrom] += 1
            tx._data.nonce = self.get_stxn_state(last_block_number + 1, tx.txfrom).nonce + tx_nonce[tx.txfrom]
            txnum += 1

        # create the block..
        block_obj = self.create_block(reveal_hash, last_block_number)

        # reset the pool back
        # FIXME: Reset pool from here?
        self.tx_pool.transaction_pool = copy.deepcopy(t_pool2)

        return block_obj

    def _get_mining_epoch(self, blocknumber):
        sv_dict = self.stake_list_get(blocknumber)

        epoch = blocknumber // config.dev.blocks_per_epoch

        if sv_dict and self.staking_address in sv_dict:
            activation_blocknumber = sv_dict[self.staking_address].activation_blocknumber
            if activation_blocknumber + config.dev.blocks_per_epoch > blocknumber:
                epoch = activation_blocknumber // config.dev.blocks_per_epoch

        return epoch

    def _get_st_address(self, slave_public_key, block_number) -> Optional[bytes]:
        if slave_public_key is None:
            logger.error('slave_public_key should not be none')
            return None

        try:
            str_slave_public_key = bin2hstr(slave_public_key)
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if block_number - 1 == self._chain.height:
                if str_slave_public_key in self._chain.pstate.stake_validators_tracker.slave_public_key_dict:
                    return self._chain.pstate.stake_validators_tracker.slave_public_key_dict[str_slave_public_key]
                logger.warning('Slave public key not found')
                logger.warning('Slave public key : %s', slave_public_key)
                slave_public_key_list = list(self._chain.pstate.stake_validators_tracker.slave_public_key_dict.keys())
                logger.warning('Stake Address list : %s', slave_public_key_list)
                return None
            elif block_number == 0:
                genesis_block = self.get_block(0)
                for raw_tx in genesis_block.transactions:
                    tx = Transaction.from_pbdata(raw_tx)
                    if tx.subtype == qrl_pb2.Transaction.STAKE:
                        if tx.slave_public_key == slave_public_key:
                            return tx.addr_from
            elif block_number == self._chain.height:
                if str_slave_public_key in self._chain.pstate.prev_stake_validators_tracker.slave_public_key_dict:
                    return self._chain.pstate.prev_stake_validators_tracker.slave_public_key_dict[str_slave_public_key]

            return self.blocks[block_number - 1].stake_validators_tracker.slave_public_key_dict[str_slave_public_key]
        except KeyError:
            self.error_msg('get_st_address', block_number)
        except Exception as e:
            self.error_msg('get_st_address', block_number, e)

        return None

    def _get_st_balance(self, stake_address, block_number) -> Optional[int]:
        if stake_address is None:
            logger.error('stake address should not be none, returning None')
            return None

        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if block_number - 1 == self._chain.height:
                if stake_address in self._chain.pstate.stake_validators_tracker.sv_dict:
                    return self._chain.pstate.stake_validators_tracker.sv_dict[stake_address].balance
                logger.warning('Stake address not found')
                logger.warning('Stake Address : %s', stake_address)
                stake_address_list = str(list(self._chain.pstate.stake_validators_tracker.sv_dict.keys()))
                logger.warning('Stake Address list : %s', stake_address_list)
                return None

            return self.blocks[block_number - 1].stake_validators_tracker.sv_dict[stake_address].balance
        except KeyError:
            self.error_msg('get_st_balance', block_number)
        except Exception as e:
            self.error_msg('get_st_balance', block_number, e)

        return None

    def _get_st_balance_by_slave_pk(self, slave_public_key, block_number) -> Optional[int]:
        str_slave_public_key = bin2hstr(slave_public_key)
        if str_slave_public_key is None:
            logger.error('stake address should not be none, returning None')
            return None

        try:
            # FIXME: Avoid +1/-1, assign a them to make things clear
            if block_number - 1 == self._chain.height:
                if str_slave_public_key in self._chain.pstate.stake_validators_tracker.slave_public_key_dict:
                    return self._chain.pstate.stake_validators_tracker.get_stake_balance_by_slave_pk(slave_public_key)
                logger.warning('Slave Public key not found')
                logger.warning('Slave PK : %s', str_slave_public_key)
                slave_public_keys = str(list(self._chain.pstate.stake_validators_tracker.slave_public_key_dict.keys()))
                logger.warning('Slave public keys: %s', slave_public_keys)
                return None

            return self.blocks[block_number - 1].stake_validators_tracker.get_stake_balance_by_slave_pk(
                                                                                                        slave_public_key
                                                                                                       )
        except KeyError:
            self.error_msg('get_st_balance_by_slave_pk', block_number)
        except Exception as e:
            self.error_msg('get_st_balance_by_slave_pk', block_number, e)

        return None

    def _clean_mining_data(self, blocknumber):
        """
        Removes the mining data from the memory.
        :param blocknumber:
        :return:
        """

        prev_epoch = blocknumber // config.dev.blocks_per_epoch
        prev_prev_epoch = prev_epoch - 1

        if prev_prev_epoch in self._wallet_private_seeds:
            del self._wallet_private_seeds[prev_prev_epoch]

        if prev_prev_epoch in self.hash_chain:
            del self.hash_chain[prev_prev_epoch]

        # FIXME: This should not be here
        if prev_prev_epoch in self.slave_xmss:
            del self.slave_xmss[prev_prev_epoch]

    def collect_ephemeral_message(self, msg_id):
        return self.pstate.get_ephemeral_metadata(msg_id)
