#QRL main blockchain, state, transaction functions.
# todo: add a .state to .blockheader, some form of concat hash or a merkle tree of changes for client to proof, though it works without..
# tx optimisations for wots: 
# a) the pub is not needed, using the sig and message it can be generated and proven valid through the merkle base hash and -> auths + root
# b) merkle_root is not needed, the merkle_auth contains it
# c) the merkle_auth does not need both pairs of values at each tree layer, once can be concat hashed from the level below..
# d) the text in the CreateSimpleTransaction object can be shortened to a single letter..


__author__ = 'pete'

from merkle import sha256
from random import randint
from time import time

import os, sys, json, jsonpickle
import merkle, wallet, db

import cPickle as pickle

global transaction_pool, m_blockchain, my, node_list

node_list = ['86.164.190.159']
m_blockchain = []
transaction_pool = []

print 'loading db'
db = db.DB()

#classes

class CreateSimpleTransaction(): 			#creates a transaction python class object which can be pickled and sent into the p2p network..
	def __init__(self, txfrom, txto, nonce, amount, data, fee=0, ots_key=0, hrs=''):
		self.txfrom = txfrom
		self.nonce = nonce 
		self.txto = txto
		self.amount = amount
		self.fee = fee
		self.ots_key = ots_key
		self.pub = data[ots_key].pub
		self.type = data[ots_key].type
		self.txhash = sha256(''.join(self.txfrom+str(self.nonce)+self.txto+str(self.amount)+str(self.fee)))			
		self.signature = merkle.sign_mss(data, self.txhash, self.ots_key)
		self.verify = merkle.verify_mss(self.signature, data, self.txhash, self.ots_key)
		
		self.hrs = hrs
		#self.merkle_root = data[0].merkle_root #temporarily use ''.join although this is fixed..old addresses in wallet..
		self.merkle_root = ''.join(data[0].merkle_root)
		self.merkle_path = data[ots_key].merkle_path


def creategenesisblock():
	return CreateGenesisBlock()


class BlockHeader():
	def __init__(self,  blocknumber, difficulty, nonce, prev_blockheaderhash, number_transactions, hashedtransactions ):
		self.blocknumber = blocknumber
		self.difficulty = 232
		self.nonce = nonce
		if self.blocknumber == 0:
			self.timestamp = 0
		else:
			self.timestamp = time()
		self.prev_blockheaderhash = prev_blockheaderhash
		self.number_transactions = number_transactions
		self.hashedtransactions = hashedtransactions
		self.headerhash = sha256(str(self.timestamp)+str(self.difficulty)+self.nonce+str(self.blocknumber)+self.prev_blockheaderhash+str(self.number_transactions)+self.hashedtransactions)


class CreateBlock():
	def __init__(self, nonce):
		difficulty = 232
		data = m_get_last_block()
		lastblocknumber = data.blockheader.blocknumber
		prev_blockheaderhash = data.blockheader.headerhash
		if not transaction_pool:
			hashedtransactions = sha256('')
		else:
			txhashes = []
			for transaction in transaction_pool:
				txhashes.append(transaction.txhash)
			hashedtransactions = sha256(''.join(txhashes))
		self.transactions = []
		for tx in transaction_pool:
			self.transactions.append(tx)						#copy memory rather than sym link
		self.blockheader = BlockHeader(blocknumber=lastblocknumber+1, difficulty=difficulty, nonce=nonce, prev_blockheaderhash=prev_blockheaderhash, number_transactions=len(transaction_pool), hashedtransactions=hashedtransactions)


class CreateGenesisBlock():			#first block has no previous header to reference..
	def __init__(self):
		self.blockheader = BlockHeader(blocknumber=0, difficulty=232, nonce='genesis', prev_blockheaderhash=sha256('quantum resistant ledger'),number_transactions=0,hashedtransactions=sha256('0'))
		self.transactions = []
		self.state = [['Qcea29b1402248d53469e352de662923986f3a94cf0f51522bedd08fb5e64948af479', [0, 10000, []]] , ['Qd17b7c86e782546fee27b8004d686e2dbcd3800792831de7486304e3019c1f938f5b',[0, 10000,[]]]]

# JSON -> python class obj ; we can improve this with looping type check and encode if str and nest deeper if list > 1 (=1 ''join then encode)

class ReCreateSimpleTransaction():			#recreate from JSON avoiding pickle reinstantiation of the class..
	def __init__(self, json_obj):
		self.nonce = json_obj['nonce']
		self.fee = json_obj['fee']
		self.type = json_obj['type'].encode('latin1')
		self.verify = json_obj['verify']
		self.merkle_root = json_obj['merkle_root'].encode('latin1')
		self.amount = json_obj['amount']
		pub = json_obj['pub']
		self.pub = []
		for key in pub:
			if self.type == 'LDOTS':
				x = key['py/tuple']
				self.pub.append((x[0].encode('latin1'), x[1].encode('latin1')))
			else:
				self.pub.append(key.encode('latin1'))
		self.ots_key = json_obj['ots_key']
		self.txhash = json_obj['txhash'].encode('latin1')
		self.txto = json_obj['txto'].encode('latin1')
		signature = json_obj['signature']
		self.signature = []
		for sig in signature:								
			self.signature.append(sig.encode('latin1'))		#encode('latin1') converts unicode back..
		self.merkle_path = []
		for pair in json_obj['merkle_path']:
			if isinstance(pair, dict):
				y = pair['py/tuple']
				self.merkle_path.append((y[0].encode('latin1'),y[1].encode('latin1')))
			elif isinstance(pair, list):
				self.merkle_path.append([''.join(pair).encode('latin1')])
		self.txfrom = json_obj['txfrom'].encode('latin1')
		#if json_obj['hrs']:
		self.hrs = json_obj['hrs'].encode('latin1')


class ReCreateBlock():						#recreate block class from JSON variables for processing
	def __init__(self, json_block):
		self.blockheader = ReCreateBlockHeader(json_block['blockheader'])
	
		transactions = json_block['transactions']
		self.transactions = []
		for tx in transactions:
			self.transactions.append(json_decode_tx(json.dumps(tx)))

class ReCreateBlockHeader():
	def __init__(self, json_blockheader):
		self.headerhash = json_blockheader['headerhash'].encode('latin1')
		self.number_transactions = json_blockheader['number_transactions']
		self.nonce = json_blockheader['nonce'].encode('latin1')
		self.timestamp = json_blockheader['timestamp']
		self.difficulty = json_blockheader['difficulty']
		self.hashedtransactions = json_blockheader['hashedtransactions'].encode('latin1')
		self.blocknumber = json_blockheader['blocknumber']
		self.prev_blockheaderhash = json_blockheader['prev_blockheaderhash'].encode('latin1')

# address functions

def roottoaddr(merkle_root):
	return 'Q'+sha256(merkle_root)+sha256(sha256(merkle_root))[:4]

def checkaddress(merkle_root, address):
	if 'Q'+sha256(merkle_root)+sha256(sha256(merkle_root))[:4] == address:
		return True
	else:
		return False

# network serialising functions

def json_decode_tx(json_tx):										#recreate transaction class object safely 
	return ReCreateSimpleTransaction(json.loads(json_tx))

def json_decode_block(json_block):
	return ReCreateBlock(json.loads(json_block))

def json_encode(obj):
	return json.dumps(obj)

def json_decode(js_obj):
	return json.loads(js_obj)

def json_bytestream(obj):
	return jsonpickle.encode(obj)

def json_bytestream_tx(tx_obj):											#JSON serialise tx object
	return 'TX'+json_bytestream(tx_obj)

def json_bytestream_bk(block_obj):										# "" block object
	return 'BK'+json_bytestream(block_obj)

def json_print(obj):													#prettify output from JSON for export purposes
	print json.dumps(json.loads(jsonpickle.encode(obj)), indent=4)

def json_print_telnet(obj):
	return json.dumps(json.loads(jsonpickle.encode(obj)), indent=4)

# tx, address chain search functions

def search_telnet(txcontains, long=1):
	tx_list = []
	hrs_list = []

	#because we allow hrs substitution in txto for transactions, we need to identify where this occurs for searching..

	if txcontains[0] == 'Q':
		for block in m_blockchain:
			for tx in block.transactions:
				if tx.txfrom == txcontains:
					if len(tx.hrs) > 0:
						if state_hrs(tx.hrs) == txcontains:
							hrs_list.append(tx.hrs)

	for tx in transaction_pool:
		if tx.txhash == txcontains or tx.txfrom == txcontains or tx.txto == txcontains or tx.txto in hrs_list:
			#print txcontains, 'found in transaction pool..'
			if long==0: tx_list.append('<tx:txhash> '+tx.txhash+' <transaction_pool>')
			if long==1: tx_list.append(json_print_telnet(tx))

	for block in m_blockchain:
		for tx in block.transactions:
			if tx.txhash == txcontains or tx.txfrom == txcontains or tx.txto == txcontains or tx.txto in hrs_list:
				#print txcontains, 'found in block',str(block.blockheader.blocknumber),'..'
				if long==0: tx_list.append('<tx:txhash> '+tx.txhash+' <block> '+str(block.blockheader.blocknumber))
				if long==1: tx_list.append(json_print_telnet(tx))
	return tx_list

# used for port 80 api - produces JSON output of a specific tx hash, including status of tx, in a block or unconfirmed + timestampe of parent block

def search_txhash(txhash):				#txhash is unique due to nonce.
	for tx in transaction_pool:
		if tx.txhash == txhash:
			print txhash, 'found in transaction pool..'
			tx_new = tx
			tx_new.block = 'unconfirmed'
			return json_print_telnet(tx_new)
	for block in m_blockchain:
		for tx in block.transactions:
			if tx.txhash== txhash:
				tx_new = tx
				tx_new.block = str(block.blockheader.blocknumber)
				tx_new.timestamp = block.blockheader.timestamp
				tx_new.confirmations = str(m_blockheight()-block.blockheader.blocknumber)
				print txhash, 'found in block',str(block.blockheader.blocknumber),'..'
				return json_print_telnet(tx_new)
	print txhash, 'does not exist in memory pool or local blockchain..'
	err = {'status' : 'Error', 'txhash' : txhash}
	return json_print_telnet(err)
	#return False

# used for port 80 api - produces JSON output reporting every transaction for an address, plus final balance..

def search_address(address):
	
	addr = {}
	addr['transactions'] = {}


	if state_address_used(address) != False:
		nonce, balance, pubhash_list = state_get_address(address)
		addr['state'] = {}
		addr['state']['address'] = address
		addr['state']['balance'] = str(balance)
		addr['state']['nonce'] = str(nonce)
		#pubhashes used could be put here..

	for tx in transaction_pool:
		if tx.txto == address or tx.txfrom == address:
			print address, 'found in transaction pool'
			addr['transactions'][tx.txhash] = {}
			addr['transactions'][tx.txhash]['txhash'] = tx.txhash
			addr['transactions'][tx.txhash]['block'] = 'unconfirmed'
			addr['transactions'][tx.txhash]['amount'] = tx.amount
			addr['transactions'][tx.txhash]['fee'] = tx.fee
			addr['transactions'][tx.txhash]['nonce'] = tx.nonce
			addr['transactions'][tx.txhash]['ots key'] = tx.ots_key
			addr['transactions'][tx.txhash]['txto'] = tx.txto
			addr['transactions'][tx.txhash]['txfrom'] = tx.txfrom

	for block in m_blockchain:
		for tx in block.transactions:
		 if tx.txto == address or tx.txfrom == address:
			print address, 'found in block ', str(block.blockheader.blocknumber), '..' 
			addr['transactions'][tx.txhash]= {}
			addr['transactions'][tx.txhash]['txhash'] = tx.txhash
			addr['transactions'][tx.txhash]['block'] = str(block.blockheader.blocknumber)
			addr['transactions'][tx.txhash]['timestamp'] = str(block.blockheader.timestamp)
			addr['transactions'][tx.txhash]['amount'] = tx.amount
			addr['transactions'][tx.txhash]['fee'] = tx.fee
			addr['transactions'][tx.txhash]['nonce'] = tx.nonce
			addr['transactions'][tx.txhash]['ots key'] = tx.ots_key
			addr['transactions'][tx.txhash]['txto'] = tx.txto
			addr['transactions'][tx.txhash]['txfrom'] = tx.txfrom	

	if len(addr['transactions']) > 0:
		addr['state']['transactions'] = str(len(addr['transactions']))
	
	if addr == {'transactions': {}}:
		addr = {'status': 'error', 'error' : 'address not found'}

	return json_print_telnet(addr)

def search(txcontains, long=1):
	for tx in transaction_pool:
		if tx.txhash == txcontains or tx.txfrom == txcontains or tx.txto == txcontains:
			print txcontains, 'found in transaction pool..'
			if long==1: json_print(tx)
	for block in m_blockchain:
		for tx in block.transactions:
			if tx.txhash== txcontains or tx.txfrom == txcontains or tx.txto == txcontains:
				print txcontains, 'found in block',str(block.blockheader.blocknumber),'..'
				if long==0: print '<tx:txhash> '+tx.txhash
				if long==1: json_print(tx)
	return

# chain functions

def f_chain_exist():
	if os.path.isfile('./chain.dat') is True:
		return True
	return False

def f_read_chain():
	block_list = []
	if os.path.isfile('./chain.dat') is False:
		print 'Creating new chain file'
		block_list.append(creategenesisblock())
		with open("./chain.dat", "a") as myfile:				#add in a new call to create random_otsmss
        		pickle.dump(block_list, myfile)
	try:
			with open('./chain.dat', 'r') as myfile:
				return pickle.load(myfile)
	except:
			print 'IO error'
			return False

def f_get_last_block():
	return f_read_chain()[-1]

def f_write_chain(block_data):											
		data = f_read_chain()
		for block in block_data:
				data.append(block)
		if block_data is not False:
			print 'Appending data to chain'
			with open("./chain.dat", "w+") as myfile:				#overwrites wallet..must use w+ as cannot append pickle item
        			pickle.dump(data, myfile)
		return

def m_load_chain():
	del m_blockchain[:]
	for block in f_read_chain():
		m_blockchain.append(block)
	return m_blockchain

def m_read_chain():
	if not m_blockchain:
		m_load_chain()
	return m_blockchain

def m_get_block(n):
	if n > len(m_blockchain)-1 or n < 0:
		return False
	return m_read_chain()[n]

def m_get_last_block():
	return m_read_chain()[-1]

def m_create_block(nonce):
	return CreateBlock(nonce)

def m_add_block(block_obj):
	if not m_blockchain:
		m_read_chain()

	for block in m_blockchain[-5:-1]:
		if block.blockheader.headerhash == block_obj.blockheader.headerhash:
			print block_obj.blockheader.headerhash, str(block_obj.blockheader.blocknumber), ' already validated and appended to chain.'
			return False

	if validate_block(block_obj, new=1) is True:
		m_blockchain.append(block_obj)
		if state_add_block(m_get_last_block()) is True:
				remove_tx_in_block_from_pool(block_obj)
		else: 	
				m_remove_last_block()
				print 'last block failed state checks, removed from chain'
				state_validate_tx_pool()
				return False
	else:
		print 'm_add_block failed - block failed validation.'
		return False
	m_f_sync_chain()
	return True

def m_remove_last_block():
	if not m_blockchain:
		m_read_chain()
	m_blockchain.pop()

def m_blockheight():
	return len(m_read_chain())-1

def m_info_block(n):
	if n > m_blockheight():
		print 'No such block exists yet..'
		return False
	b = m_get_block(n)
	print 'Block: ', b, str(b.blockheader.blocknumber)
	print 'Blocksize, ', str(len(json_bytestream(b)))
	print 'Number of transactions: ', str(len(b.transactions))
	print 'Validates: ', validate_block(b, last_block = n-1)

def m_f_sync_chain():
	f_write_chain(m_read_chain()[f_get_last_block().blockheader.blocknumber+1:])
	
def m_verify_chain(verbose=0):
	n = 0
	for block in m_read_chain()[1:]:
		if validate_block(block,last_block=n, verbose=verbose) is False:
				return False
		n+=1
		if verbose is 1:
			sys.stdout.write('.')
			sys.stdout.flush()
	return True

#state functions
#first iteration - state data stored in leveldb file
#state holds address balances, the transaction nonce and a list of pubhash keys used for each tx - to prevent key reuse.

def state_load_peers():
	if os.path.isfile('./peers.dat') is True:
		print 'Opening peers.dat'
		with open('./peers.dat', 'r') as myfile:
			state_put_peers(pickle.load(myfile))
	else:
		print 'Creating peers.dat'
	 	with open('./peers.dat', 'w+') as myfile:
			pickle.dump(node_list, myfile)
			state_put_peers(node_list)

def state_save_peers():
	with open("./peers.dat", "w+") as myfile:			
        			pickle.dump(state_get_peers(), myfile)

def state_get_peers():
	try: return db.get('node_list')
	except: return False
	
def state_put_peers(peer_list):
	try: db.put('node_list', peer_list)
	except: return False

def state_uptodate():									#check state db marker to current blockheight.
	if m_blockheight() == db.get('blockheight'):
		return True
	return False

def state_blockheight():
	return db.get('blockheight')

def state_get_address(addr):
	try: return db.get(addr)
	except:	return [0,0,[]]

def state_address_used(addr):							#if excepts then address does not exist..
	try: return db.get(addr)
	except: return False 

def state_balance(addr):
	try: return db.get(addr)[1]
	#except:	return False
	except:	return 0 

def state_nonce(addr):
	try: return db.get(addr)[0]
	except: return 0
	#except:	return False

def state_pubhash(addr):
	try: return db.get(addr)[2]
	except: return []
	#except:	return False

def state_hrs(hrs):
	try: return db.get('hrs'+hrs)
	except: return False


def state_validate_tx(tx):		#checks new tx validity based upon node statedb and node mempool. different to state_add_block validation which is an ordered update of state based upon statedb alone.

	if state_uptodate() is False:
			print 'Warning state not updated to allow safe tx validation, tx validity could be unreliable..'
			#return False

	if state_balance(tx.txfrom) is 0:
			print 'State validation failed for', tx.txhash, 'because: Empty address'
			return False 

	if state_balance(tx.txfrom) < tx.amount: 
			print 'State validation failed for', tx.txhash,  'because: Insufficient'
			return False

	if state_hrs(tx.hrs) is not False:				#if false - not used before..so pass..
		if state_hrs(tx.hrs) != tx.txfrom:			#if another address found in state_db than txfrom then invalid..
			print 'Human readable string already seen in ledger associated with another address, tx therefore invalid: ', tx.txhash
			return False
		if tx.hrs[0] == 'Q':
			print 'Human readable string invalid for ', tx.txhash
			return False
	# nonce and public key can be in the mempool (transaction_pool) and so these must be checked also..
	# if the tx is new to node then simple check would work. but if we are checking a tx in the transaction_pool, then order has to be correct..

	z = 0
	x = 0
	for t in transaction_pool:
			if t.txfrom == tx.txfrom:
					x+=1
					if t.txhash == tx.txhash:		#this is our unique tx..
						z = x

	if x == 0:
		z+=1

	if state_nonce(tx.txfrom)+z != tx.nonce:
			print 'State validation failed for', tx.txhash, 'because: Invalid nonce'
			return False

	pub = tx.pub
	if tx.type == 'LDOTS':
		pub = [i for sub in pub for i in sub]
	elif tx.type == 'WOTS':
				pass
	pubhash = sha256(''.join(pub))

	for txn in transaction_pool:
	  if txn.txhash == tx.txhash:
	  	pass
	  else:
		pub = txn.pub
		if txn.type == 'LDOTS':
			pub = [i for sub in pub for i in sub]
		elif txn.type == 'WOTS':
				pass
		pubhashn = sha256(''.join(pub))

		if pubhashn == pubhash:
			print 'State validation failed for', tx.txhash, 'because: Public key re-use detected'
			return False


	if pubhash in state_pubhash(tx.txfrom):
			print 'State validation failed for', tx.txhash, 'because: Public key re-use detected'
			return False

	return True

def state_validate_tx_pool():
	x=0
	for tx in transaction_pool:
		if state_validate_tx(tx) is False:
			x+=1
			print 'tx', tx.txhash, 'failed..'
			remove_tx_from_pool(tx)
	if x > 0:
		return False
	return True

# add some form of header hash check to confirm block correct..

def state_add_block(block):

	assert state_blockheight() == m_blockheight()-1, 'state leveldb not @ m_blockheight-1'

	#snapshot of state in case we need to revert to it..

	st1 = []	
	st2 = []
	st3 = []
	for tx in block.transactions:
		st1.append(state_get_address(tx.txfrom))
		if tx.txto[0] != 'Q' and state_hrs(tx.txto) != False:			#if hrs then get balance from actual txto..
			st2.append(state_get_address(state_hrs(tx.txto)))
		else:
			st2.append(state_get_address(tx.txto))
		st3.append(state_hrs(tx.hrs))

	y = 0
	
	# cycle through every tx in the new block to check state

	for tx in block.transactions:

		pub = tx.pub
		if tx.type == 'LDOTS':
				   pub = [i for sub in pub for i in sub]
		elif tx.type == 'WOTS':
				pass
		pubhash = sha256(''.join(pub))

		s1 = state_get_address(tx.txfrom)
		
		# basic tx state checks..

		if s1[1] - tx.amount < 0:
			print tx, tx.txfrom, 'exceeds balance, invalid tx'
			#return False
			break

		if tx.nonce != s1[0]+1:
			print 'nonce incorrect, invalid tx'
			print tx, tx.txfrom, tx.nonce
			#return False
			break

		if pubhash in s1[2]:
			print 'pubkey reuse detected: invalid tx', tx.txhash
			break

		#hrs checks for tx validity, adds tx.hrs string to statedb if valid..
		if len(tx.hrs) != 0:
			if state_hrs(tx.hrs) is not False:
				print 'Invalid hrs string (already associated with another Q-address'
				break
			if tx.hrs[0] == 'Q':
				print 'Invalid hrs string (begins with Q)', tx.txhash
				break
			db.put('hrs'+tx.hrs, tx.txfrom)

		# commit new changes to statedb for txfrom, txto (which could be a hrs->actual txto)

		s1[0]+=1
		s1[1] = s1[1]-tx.amount
		s1[2].append(pubhash)
		db.put(tx.txfrom, s1)

		if tx.txto[0] != 'Q' and state_hrs(tx.txto) != False:			#update the actual balance of txto not hrs
			to = state_hrs(tx.txto)
		else:
			to = tx.txto
	
		s2 = state_get_address(to)
		s2[1] = s2[1]+tx.amount
		db.put(to, s2)

		y+=1

	if y<len(block.transactions):			# if we havent done all the tx in the block we have break, need to revert state back to before the change.
		print 'failed to state check entire block'
		print 'reverting state'

		for x in range(len(block.transactions)):
			db.put(block.transactions[x].txfrom, st1[x])
			db.put(block.transactions[x].txto, st2[x])
			if st3[x] == False:									
				pass
			else:
				db.put('hrs'+block.transactions[x].hrs, st3[x])			#only revert write hrs into state_db if there is an address entry previously..
		return False

	db.put('blockheight', m_blockheight())
	print block.blockheader.headerhash, str(len(block.transactions)),'tx ',' passed verification.'
	return True


def state_read_chain():

	db.zero_all_addresses()
	c = m_get_block(0).state
	for address in c:
		db.put(address[0], address[1])

	c = m_read_chain()[1:]

	for block in c:

		for tx in block.transactions:
			pub = tx.pub
			if tx.type == 'LDOTS':
				  	pub = [i for sub in pub for i in sub]
			elif tx.type == 'WOTS':
					pass
			pubhash = sha256(''.join(pub))

			s1 = state_get_address(tx.txfrom)

			if s1[1] - tx.amount < 0:
				print tx, tx.txfrom, 'exceeds balance, invalid tx', tx.txhash
				print block.blockheader.headerhash, 'failed state checks'
				return False

			if tx.nonce != s1[0]+1:
				print 'nonce incorrect, invalid tx', tx.txhash
				print block.blockheader.headerhash, 'failed state checks'
				return False

			if pubhash in s1[2]:
				print 'public key re-use detected, invalid tx', tx.txhash
				print block.blockheader.headerhash, 'failed state checks'
				return False

			if len(tx.hrs) != 0:
				if state_hrs(tx.hrs) is not False:
					print 'hrs invalid re-use attempt in tx ', tx.txhash
					return False
				if tx.hrs[0] == 'Q':
					print 'hrs invalid, starts with "Q"', tx.txhash
					return False
				db.put('hrs'+tx.hrs, tx.txfrom)

			s1[0]+=1
			s1[1] = s1[1]-tx.amount
			s1[2].append(pubhash)
			db.put(tx.txfrom, s1)							#must be ordered in case tx.txfrom = tx.txto


			if tx.txto[0] != 'Q' and state_hrs(tx.txto) != False:		#if hrs then need to update state of actual txto..
				to = state_hrs(tx.txto)
			else:
				to = tx.txto
			s2 = state_get_address(to)
			s2[1] = s2[1]+tx.amount
			db.put(to, s2)			

		print block, str(len(block.transactions)), 'tx ', ' passed'
	db.put('blockheight', m_blockheight())
	return True

#tx functions and classes

def createsimpletransaction(txfrom, txto, amount, data, fee=0, hrs=''):	

	#few state checks to ensure tx is valid, including tx already in the transaction_pool
	#need to avoid errors in nonce and public key re-use which will invalidate the tx at other nodes

	if state_uptodate() is False:
			msg = 'state not at latest block in chain'
			print msg
			return (False, msg)

	if state_balance(txfrom) is 0:
			msg = 'empty address'
			print msg
			return (False, msg) 

	if state_balance(txfrom) < amount: 
			msg = 'insufficient funds for valid tx'
			print msg
			return (False, msg)
	
	if len(hrs) != 0:
		if hrs[0] == 'Q':
			msg = 'cannot start a human readable string with "Q"'
			print msg
			return (False, msg)
		if state_hrs(hrs) is not False:
			msg = 'human readable string already associated with another address'
			print msg
			return (False, msg)
	

	# signatures remaining is important to check - once all the public keys are used then any funds left will be frozen and unspendable..

	nonce = state_nonce(txfrom)+1

	for t in transaction_pool:
		if t.txfrom == txfrom:
				nonce+=1

	s = data[0].signatures-nonce

	if s == 0: 
		if state_balance(txfrom)-amount > 0:
			msg = '***WARNING***: Only ONE remaining transaction possible from this address without leaving funds inaccessible. If you wish to proceed either create the tx manually or move ALL funds from this address with next transaction attempt. Transaction cancelled.'
			print msg
			return (False, msg)
		else:
			msg = 'Creating final transaction with address..'
			print msg

	if s < 0: 
		msg = 'No valid transactions from this address can be performed as there are no remaining valid signatures available, sorry.'	#not strictly true..
		print msg
		return (False, msg)
	if s == 1:
			msg = 'Warning: only '+str(s)+'remaining transactions possible from this address - consider moving funds to a new address immediately.'
			print msg
	elif s <= 5:
		msg = 'Warning: only '+ str(s)+'further transactions possible from this address before one-time signatures run out.'
		print msg
	else:
		msg = str(s)+' further transactions can be signed from this address.'	
	#need to determine which public key in the OTS-MSS to use..

	ots_key = nonce-1		#nonce for first tx from an address is 1, first ots signature is 0..

	for pubhash in state_pubhash(txfrom):
		if pubhash == data[ots_key].pubhash:
			msg = 'Wallet error: pubhash at ots_key has already been used. Compose a transaction manually and move funds to a new address.'
			print msg
			return (False, msg)

	return (CreateSimpleTransaction(txfrom=txfrom, txto=txto, amount=amount, nonce=nonce, data=data, fee=fee, ots_key=ots_key, hrs=hrs), msg)

def add_tx_to_pool(tx_class_obj):
	transaction_pool.append(tx_class_obj)

def remove_tx_from_pool(tx_class_obj):
	transaction_pool.remove(tx_class_obj)

def show_tx_pool():
	return transaction_pool

def remove_tx_in_block_from_pool(block_obj):
	for tx in block_obj.transactions:
		for txn in transaction_pool:
			if tx.txhash == txn.txhash:
				remove_tx_from_pool(txn)

def flush_tx_pool():
	del transaction_pool[:]

def validate_tx_in_block(block_obj, new=0):
	x = 0
	for transaction in block_obj.transactions:
		if validate_tx(transaction, new=new) is False:
			print 'invalid tx: ',transaction, 'in block'
			x+=1
	if x > 0:
		return False
	return True

def validate_tx_pool():									#invalid transactions are auto removed from pool..
	for transaction in transaction_pool:
		if validate_tx(transaction) is False:
			remove_tx_from_pool(transaction)
			print 'invalid tx: ',transaction, 'removed from pool'

	return True

def validate_tx(tx, new=0):


		#cryptographic checks

	if not tx:
		raise Exception('No transaction to validate.')

	if tx.txhash != sha256(''.join(tx.txfrom+str(tx.nonce))+tx.txto+str(tx.amount)+str(tx.fee)):
		return False

	if tx.type == 'WOTS':
		if merkle.verify_wkey(tx.signature, tx.txhash, tx.pub) is False:
				return False
	elif tx.type == 'LDOTS':
		if merkle.verify_lkey(tx.signature, tx.txhash, tx.pub) is False:
				return False
	else: 
		return False

	if checkaddress(tx.merkle_root, tx.txfrom) is False:
			return False

	if merkle.verify_root(tx.pub, tx.merkle_root, tx.merkle_path) is False:
			return False
			
	return True

# block validation

def validate_block(block, last_block='default', verbose=0, new=0):		#check validity of new block..

	b = block.blockheader

	if int(sha256(b.prev_blockheaderhash+b.nonce),16) >= 2**b.difficulty:
		print '0'
		return False
	
	if sha256(str(b.timestamp)+str(b.difficulty)+b.nonce+str(b.blocknumber)+b.prev_blockheaderhash+str(b.number_transactions)+b.hashedtransactions) != block.blockheader.headerhash:
		print '1'
		return False

	if last_block=='default':
		if m_get_last_block().blockheader.headerhash != block.blockheader.prev_blockheaderhash:
			return False
		if m_get_last_block().blockheader.blocknumber != block.blockheader.blocknumber-1:
			return False
	else:
		if m_get_block(last_block).blockheader.headerhash != block.blockheader.prev_blockheaderhash:
			return False
		if m_get_block(last_block).blockheader.blocknumber != block.blockheader.blocknumber-1:
			return False

	if validate_tx_in_block(block, new=new) == False:
		return False

	txhashes = []
	for transaction in block.transactions:
		txhashes.append(transaction.txhash)

	if sha256(''.join(txhashes)) != block.blockheader.hashedtransactions:
		return False

	if verbose==1:
		print block, 'True'

	return True


# simple transaction creation and wallet functions using the wallet file..

def wlt():
	return merkle.numlist(wallet.list_addresses())

def create_my_tx(txfrom, txto, n, hrs=''):
	my = wallet.f_read_wallet()
	if isinstance(txto, int):
		(tx, msg) = createsimpletransaction(txto=my[txto][0],txfrom=my[txfrom][0],amount=n, data=my[txfrom][1], hrs=hrs)
	elif isinstance(txto, str):
		(tx, msg) = createsimpletransaction(txto=txto,txfrom=my[txfrom][0],amount=n, data=my[txfrom][1], hrs=hrs)
	if tx is not False:
		transaction_pool.append(tx)
		return (tx, msg)
	else:
		return (False, msg)

def create_hrs_tx(txfrom, hrs):

	my = wallet.f_read_wallet()
	if isinstance(txfrom, int):
		(tx, msg) = createsimpletransaction(txto=my[txfrom][0],txfrom=my[txfrom][0],amount=0, data=my[txfrom][1], hrs=hrs)
	elif isinstance(txfrom, str):
		return (False, 'failed: txfrom is not an int')
	if tx is not False:
		transaction_pool.append(tx)
		return (tx, msg)
	else:
		return (False, msg)

# xmss data stored in the class object from which addresses may be derived and signed. set up to immediately allow signing from address derived from ots_key = [-1]
# from the class object e.g xmss the following data can be passed: xmss.address, xmss.PK, xmss.pk(ots_key), xmss.SEED, xmss.public_SEED, xmss.private_SEED, xmss.index
# 
# signatures can be made xmss.SIGN -> returns i, s, auth_route, i_bms, self.pk(i), self.PK_short

#--------------------------------------------

def create_xmss_tx(txfrom, txto, n):
	my=wallet.f_read_wallet()
	#if isinstance(txfrom, int):
	#	(tx, msg) = createsimpletransaction(txto=,txfrom=,amount=, data=, )

#--------------------------------------------

def test_tx(n):
	for x in range(n):
		create_my_tx(randint(0,5), randint(0,5),0.06)

# debugging functions

def create_some_tx(n):				
	for x in range(n):
		a,b = wallet.getnewaddress(), wallet.getnewaddress()
		transaction_pool.append(createsimpletransaction(a[0],b[0],10,a[1]))



