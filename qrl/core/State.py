# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
from typing import Optional, List

from google.protobuf.json_format import MessageToJson, Parse
from pyqrllib.pyqrllib import bin2hstr, hstr2bin

from qrl.core import config
from qrl.core.BlockMetadata import BlockMetadata
from qrl.core.GenesisBlock import GenesisBlock
from qrl.core.Block import Block
from qrl.core.misc import logger, db
from qrl.core.Transaction import Transaction, TokenTransaction, TransferTokenTransaction
from qrl.core.TokenMetadata import TokenMetadata
from qrl.core.TokenList import TokenList
from qrl.core.EphemeralMetadata import EphemeralMetadata, EncryptedEphemeralMessage
from qrl.core.AddressState import AddressState
from qrl.generated import qrl_pb2


class State:
    # FIXME: Rename to PersistentState
    # FIXME: Move blockchain caching/storage over here
    # FIXME: Improve key generation

    def __init__(self):
        self._db = db.DB()  # generate db object here

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._db is not None:
            del self._db
            self._db = None

    def put_block(self, block, batch):
        self._db.put_raw(bin2hstr(block.headerhash).encode(), block.to_json().encode(), batch)

    def get_block(self, header_hash: bytes) -> Optional[Block]:
        try:
            json_data = self._db.get_raw(bin2hstr(header_hash).encode())
            return Block.from_json(json_data)
        except KeyError:
            logger.debug('[get_block] Block header_hash %s not found', bin2hstr(header_hash).encode())
        except Exception as e:
            logger.error('[get_block] %s', e)

        return None

    def put_block_metadata(self, headerhash, block_metadata, batch):
        self._db.put_raw(b'metadata_'+bin2hstr(headerhash).encode(), block_metadata.to_json(), batch)

    def get_block_metadata(self, header_hash: bytes) -> Optional[BlockMetadata]:
        try:
            json_data = self._db.get_raw(b'metadata_'+bin2hstr(header_hash).encode())
            return BlockMetadata.from_json(json_data)
        except KeyError:
            logger.debug('[get_block_metadata] Block header_hash %s not found',
                         b'metadata_'+bin2hstr(header_hash).encode())
        except Exception as e:
            logger.error('[get_block_metadata] %s', e)

        return None

    def put_block_number_mapping(self, block_number, block_number_mapping, batch):
        self._db.put_raw(str(block_number).encode(), MessageToJson(block_number_mapping).encode(), batch)

    def get_block_number_mapping(self, block_number: bytes) -> Optional[qrl_pb2.BlockNumberMapping]:
        try:
            json_data = self._db.get_raw(str(block_number).encode())
            block_number_mapping = qrl_pb2.BlockNumberMapping()
            return Parse(json_data, block_number_mapping)
        except KeyError:
            logger.debug('[get_block_number_mapping] Block #%s not found', block_number)
        except Exception as e:
            logger.error('[get_block_number_mapping] %s', e)

        return None

    def get_block_by_number(self, block_number) -> Optional[Block]:
        block_number_mapping = self.get_block_number_mapping(block_number)
        if not block_number_mapping:
            return None
        return self.get_block(block_number_mapping.headerhash)

    def get_state(self, header_hash, addresses_state, state_block_number=-1):
        while True:
            block = self.get_block(header_hash)

            if not block:
                return addresses_state

            if block.block_number == state_block_number:
                return addresses_state

            if block.block_number == 0:
                for genesis_balance in GenesisBlock().genesis_balance:
                    bytes_addr = genesis_balance.address.encode()
                    addresses_state[bytes_addr] = AddressState.get_default(bytes_addr)
                    addresses_state[bytes_addr]._data.balance = genesis_balance.balance
                return addresses_state

            for tx_pbdata in block.transactions:
                tx = Transaction.from_pbdata(tx_pbdata)
                if tx.txfrom not in addresses_state:
                    addresses_state[tx.txfrom] = AddressState.get_default(tx.txfrom)

                if tx.subtype in (qrl_pb2.Transaction.TRANSFER,
                                  qrl_pb2.Transaction.TRANSFERTOKEN,
                                  qrl_pb2.Transaction.COINBASE):
                    if tx.txto not in addresses_state:
                        addresses_state[tx.txto] = AddressState.get_default(tx.txto)

                if tx.subtype == qrl_pb2.Transaction.TOKEN:
                    addresses_state[tx.owner] = AddressState.get_default(tx.owner)
                    for initial_balance in tx.initial_balances:
                        if initial_balance not in addresses_state:
                            addresses_state[initial_balance.address] = AddressState.get_default(initial_balance.address)

                tx.apply_on_state(addresses_state)

            header_hash = block.prev_headerhash

    def update_state(self, addresses_state):
        for address in addresses_state:
            self._save_address_state(addresses_state[address])

    def get_ephemeral_metadata(self, msg_id: bytes):
        try:
            json_ephemeral_metadata = self._db.get_raw(b'ephemeral_' + msg_id)

            return EphemeralMetadata.from_json(json_ephemeral_metadata)
        except KeyError:
            pass
        except Exception as e:
            logger.exception(e)

        return EphemeralMetadata()

    def update_ephemeral(self, encrypted_ephemeral: EncryptedEphemeralMessage):
        ephemeral_metadata = self.get_ephemeral_metadata(encrypted_ephemeral.msg_id)
        ephemeral_metadata.add(encrypted_ephemeral)

        self._db.put_raw(b'ephemeral_' + encrypted_ephemeral.msg_id, ephemeral_metadata.to_json().encode())

    def _blockheight(self):
        # FIXME: Remove
        return self._db.get('blockheight')

    def _set_blockheight(self, height):
        # FIXME: Remove
        return self._db.put('blockheight', height)

    def update_last_tx(self, block, batch):
        if len(block.transactions) == 0:
            return
        last_txn = []

        try:
            last_txn = self._db.get('last_txn')
        except:  # noqa
            pass

        for protobuf_txn in block.transactions[-20:]:
            txn = Transaction.from_pbdata(protobuf_txn)
            if txn.subtype == qrl_pb2.Transaction.TRANSFER:
                last_txn.insert(0, [txn.to_json(),
                                    block.block_number,
                                    block.timestamp])

        del last_txn[20:]
        self._db.put('last_txn', last_txn, batch)

    def get_last_txs(self):
        try:
            last_txn = self._db.get('last_txn')
        except:  # noqa
            return []

        txs = []
        for tx_metadata in last_txn:
            tx_json, block_num, block_ts = tx_metadata
            tx = Transaction.from_json(tx_json)
            txs.append(tx)

        return txs

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################

    def update_address_tx_hashes(self, addr: bytes, new_txhash: bytes):
        txhash = self.get_address_tx_hashes(addr)
        txhash.append(new_txhash)

        # FIXME:  Json does not support bytes directly | Temporary workaround
        tmp_hashes = [bin2hstr(item) for item in txhash]

        self._db.put(b'txn_' + addr, tmp_hashes)

    def get_token_list(self):
        try:
            return self._db.get_raw(b'token_list')
        except KeyError:
            return TokenList().to_json()

    def update_token_list(self, token_txhashes: list, batch):
        pbdata = self.get_token_list()
        token_list = TokenList.from_json(pbdata)
        token_list.update(token_txhashes)
        self._db.put_raw(b'token_list', token_list.to_json().encode(), batch)

    def get_token_metadata(self, token_txhash: bytes):
        json_data = self._db.get_raw(b'token_' + token_txhash)
        return TokenMetadata.from_json(json_data)

    def update_token_metadata(self, transfer_token: TransferTokenTransaction):
        token_metadata = self.get_token_metadata(transfer_token.token_txhash)
        token_metadata.update([transfer_token.txhash])
        self._db.put_raw(b'token_' + transfer_token.token_txhash,
                         token_metadata.to_json().encode())

    def create_token_metadata(self, token: TokenTransaction):
        token_metadata = TokenMetadata.create(token_txhash=token.txhash, transfer_token_txhashes=[token.txhash])
        self._db.put_raw(b'token_' + token.txhash,
                         token_metadata.to_json().encode())

    def get_state_version(self):
        return self._db.get(b'state_version')

    def update_state_version(self, block_number, batch):
        self._db.put(b'state_version', block_number, batch)

    def get_address_tx_hashes(self, addr: bytes) -> List[bytes]:
        try:
            tx_hashes = self._db.get(b'txn_' + addr)
        except KeyError:
            tx_hashes = []
        except Exception as e:
            logger.exception(e)
            tx_hashes = []

        tx_hashes = [bytes(hstr2bin(item)) for item in tx_hashes]

        return tx_hashes

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################

    def get_txn_count(self, addr):
        try:
            return self._db.get((b'txn_count_' + addr))
        except KeyError:
            pass
        except Exception as e:
            # FIXME: Review
            logger.error('Exception in get_txn_count')
            logger.exception(e)

        return 0

    def increase_txn_count(self, addr: bytes):
        # FIXME: This should be transactional
        last_count = self.get_txn_count(addr)
        self._db.put(b'txn_count_' + addr, last_count + 1)

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################

    def update_tx_metadata(self, block, batch):
        if len(block.transactions) == 0:
            return

        token_list = []

        # FIXME: Inconsistency in the keys/types
        for protobuf_txn in block.transactions:
            txn = Transaction.from_pbdata(protobuf_txn)
            if txn.subtype in (qrl_pb2.Transaction.TRANSFER,
                               qrl_pb2.Transaction.COINBASE,
                               qrl_pb2.Transaction.MESSAGE,
                               qrl_pb2.Transaction.TOKEN,
                               qrl_pb2.Transaction.TRANSFERTOKEN,
                               qrl_pb2.Transaction.LATTICE):
                self._db.put(bin2hstr(txn.txhash),
                             [txn.to_json(), block.block_number, block.timestamp],
                             batch)

                if txn.subtype in (qrl_pb2.Transaction.TRANSFER,
                                   qrl_pb2.Transaction.MESSAGE,
                                   qrl_pb2.Transaction.TOKEN,
                                   qrl_pb2.Transaction.TRANSFERTOKEN):
                    # FIXME: Being updated without batch, need to fix,
                    # as its making get request, and batch get not possible
                    # Thus cache is required to have only 1 time get
                    self.update_address_tx_hashes(txn.txfrom, txn.txhash)

                if txn.subtype == qrl_pb2.Transaction.TOKEN:
                    self.update_address_tx_hashes(txn.owner, txn.txhash)
                    for initial_balance in txn.initial_balances:
                        if initial_balance.address == txn.owner:
                            continue
                        self.update_address_tx_hashes(initial_balance.address, txn.txhash)

                if txn.subtype in (qrl_pb2.Transaction.TRANSFER,
                                   qrl_pb2.Transaction.COINBASE,
                                   qrl_pb2.Transaction.TRANSFERTOKEN):
                    # FIXME: Being updated without batch, need to fix,
                    if txn.subtype == qrl_pb2.Transaction.TRANSFERTOKEN:
                        self.update_token_metadata(txn)
                    self.update_address_tx_hashes(txn.txto, txn.txhash)
                    self.increase_txn_count(txn.txto)

                if txn.subtype == qrl_pb2.Transaction.TOKEN:
                    self.create_token_metadata(txn)
                    token_list.append(txn.txhash)

                self.increase_txn_count(txn.txfrom)

        if token_list:
            self.update_token_list(token_list, batch)

    def get_tx_metadata(self, txhash: bytes):
        try:
            tx_metadata = self._db.get(bin2hstr(txhash))
        except Exception:
            return None
        if tx_metadata is None:
            return None
        txn_json, block_number, _ = tx_metadata
        return Transaction.from_json(txn_json), block_number

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################

    def _get_address_state(self, address: bytes) -> AddressState:
        data = self._db.get_raw(address)
        if data is None:
            raise KeyError("{} not found".format(address))
        pbdata = qrl_pb2.AddressState()
        pbdata.ParseFromString(bytes(data))
        address_state = AddressState(pbdata)
        return address_state

    def _save_address_state(self, address_state: AddressState, batch=None):
        data = address_state.pbdata.SerializeToString()
        self._db.put_raw(address_state.address, data, batch)

    def get_address(self, address: bytes) -> AddressState:
        # FIXME: Avoid two calls to know if address is not recognized (merged with is used)
        try:
            return self._get_address_state(address)
        except KeyError:
            # FIXME: Check all cases where address is not found
            return AddressState.create(address=address,
                                       nonce=config.dev.default_nonce,
                                       balance=config.dev.default_account_balance,
                                       ots_bitfield=[b'\x00'] * config.dev.ots_bitfield_size,
                                       tokens=dict())

    def nonce(self, addr: bytes) -> int:
        return self.get_address(addr).nonce

    def balance(self, addr: bytes) -> int:
        return self.get_address(addr).balance

    def address_used(self, address: bytes):
        # FIXME: Probably obsolete
        try:
            return self._get_address_state(address)
        except KeyError:
            return False
        except Exception as e:
            # FIXME: Review
            logger.error('Exception in address_used')
            logger.exception(e)
            raise

    def return_all_addresses(self):
        addresses = []
        for key, data in self._db.RangeIter(b'Q', b'Qz'):
            pbdata = qrl_pb2.AddressState()
            pbdata.ParseFromString(bytes(data))
            address_state = AddressState(pbdata)
            addresses.append(address_state)
        return addresses

    def get_batch(self):
        return self._db.get_batch()

    def write_batch(self, batch):
        self._db.write_batch(batch)

    #########################################
    #########################################
    #########################################
    #########################################
    #########################################

    def total_coin_supply(self):
        # FIXME: This is temporary code. NOT SCALABLE. It is easy to keep a global count
        all_addresses = self.return_all_addresses()
        coins = 0
        for a in all_addresses:
            coins = coins + a.balance
        return coins
