#QRL main blockchain, state, stake, transaction functions.

# after basic working pos
# add stake_seed into block classes..
# move hashchain generation from chain to wallet..derive from private SEED gen..store in f_wallet..update based upon epoch...
# api stakers - from stake_list..
# establish tx.nonce at the point of block creation - important as we sort the tx pool by txhash, thus nonce could be wrong then..
# add stake list check to the state check - addresses which are staking cannot make transactions..
# perhaps we should add all the stakers with hash terminators and last hash + nonce into block.stake as a list?

__author__ = 'pete'

from merkle import sha256, numlist
from time import time
from operator import itemgetter
from math import log, ceil

import os, copy, ast, sys, json, jsonpickle, decimal
import merkle, wallet, db

import cPickle as pickle

global transaction_pool, stake_pool, txhash_timestamp, m_blockchain, my, node_list, ping_list, last_ping

global mining_address, stake_list, stake_commit, stake_reveal, hash_chain, epoch_prf


hash_chain = []
ping_list =[]
node_list = ['104.251.219.145']
#node_list = []
m_blockchain = []
transaction_pool = []
txhash_timestamp = []
stake_commit = []
stake_reveal = []
stake_pool = []
stake_list = []

print 'loading db'
db = db.DB()

print 'loading wallet'
my = wallet.f_read_wallet()
wallet.f_load_winfo()
mining_address = my[0][1].address
print 'mining/staking address', mining_address

# pos

class Hashchain():
	def __init__(self, n=10000):
		start_chain = merkle.random_key()
		iter_chain = start_chain
		hash_chain = []
		hash_chain.append(start_chain)
		for x in range(n):
			iter_chain = sha256(iter_chain)
			hash_chain.append(iter_chain)
		self.hash_chain = hash_chain
		self.n = n

	def hash(self, x):
		if x > self.n or x < 0:
			return False
		return self.hash_chain[self.n-x]

	def seed(self):
		return self.hash_chain[0]


# return a sorted list of txhashes from transaction_pool, sorted by timestamp from block n (actually from start of transaction_pool) to time, then ordered by txhash.

def sorted_tx_pool(timestamp=None):
	if timestamp == None:
		timestamp=time()
	pool = copy.deepcopy(transaction_pool)
	trimmed_pool = []
	#start_time = m_blockchain[-1].blockheader.timestamp
	end_time = timestamp
	for tx in pool:
		#if txhash_timestamp[txhash_timestamp.index(tx.txhash)+1] >= start_time and txhash_timestamp[txhash_timestamp.index(tx.txhash)+1] <= end_time:
		if txhash_timestamp[txhash_timestamp.index(tx.txhash)+1] <= end_time:
					trimmed_pool.append(tx.txhash)

	trimmed_pool.sort()

	if trimmed_pool == []:
		return False

	return trimmed_pool

# merkle tree root hash of tx from pool for next POS block

def merkle_tx_hash(hashes):
	if len(hashes)==64:					# if len = 64 then it is a single hash string rather than a list..
		return hashes
	j=int(ceil(log(len(hashes),2)))
	l_array = []
	l_array.append(hashes)
	for x in range(j):
		next_layer = []
		i = len(l_array[x])%2 + len(l_array[x])/2
		z=0
		for y in range(i):
			if len(l_array[x])==z+1:
				next_layer.append(l_array[x][z])
			else:
				next_layer.append(sha256(l_array[x][z]+l_array[x][z+1]))
			z+=2
		l_array.append(next_layer)
	#print l_array
	return ''.join(l_array[-1])

# create a snapshot of the transaction pool to account for network traversal time (probably less than 300ms, but let's give a window of 1.5 seconds). 
# returns: list of merkle root hashes of the tx pool over last 1.5 seconds

def pos_block_pool():
	timestamp = time()
	start_time = timestamp-1.5

	x = sorted_tx_pool(start_time)
	y = sorted_tx_pool(timestamp)
	if y == False:				# if pool is empty -> return sha256 null
		return [sha256('')]
	elif x == y:					# if the pool isnt empty but there is no difference then return the only merkle hash possible..			
		return [merkle_tx_hash(y)]
	else:						# there is a difference in contents of pool over last 1.5 seconds..
		merkle_hashes = []
		if x == False:				
			merkle_hashes.append(sha256(''))
			x = []
		else:
			merkle_hashes.append(merkle_tx_hash(x))

		tmp_txhashes = x

		for tx in reversed(transaction_pool):
			if tx.txhash in y and tx.txhash not in x:
				tmp_txhashes.append(tx.txhash)
				tmp_txhashes.sort()
				merkle_hashes.append(merkle_tx_hash(tmp_txhashes))

		return merkle_hashes		

# create the PRF selector sequence based upon a seed and number of stakers in list (temporary..there are better ways to do this with bigger seed value, but it works)

def pos_block_selector(seed, n):
	n_bits = int(ceil(log(n,2)))
	prf = merkle.GEN_range_bin(seed, 1, 20000,1)
	prf_range = []
	for z in prf:
		x = ord(z) >> 8-n_bits
		if x < n:
			prf_range.append(x)
	return prf_range

# return the POS staker list position for given seed at index, i

def pos_block_selector_n(seed, n, i):
	l = pos_block_selector(seed, n)
	return l[i]

#classes

class CreateStakeTransaction():
	def __init__(self, hashchain_terminator):
		data = my[0][1]
		self.txfrom = mining_address
		self.hash = hashchain_terminator
		self.type = 'XMSS/STAKE'
		S = data.SIGN(self.hash)				# Sig = {i, s, auth_route, i_bms, self.pk(i), self.PK_short}
		self.i = S[0]
		self.signature = S[1]
		self.merkle_path = S[2]
		self.i_bms = S[3]
		self.pub = S[4]
		self.PK = S[5]

class ReCreateStakeTransaction():
	def __init__(self, json_obj):
		self.type = json_obj['type'].encode('latin1')
		self.hash = json_obj['hash'].encode('latin1')
		self.i_bms = []
		for layer in json_obj['i_bms']:
			if len(layer) ==2:
					self.i_bms.append([layer[0],layer[1]])
			elif len(layer) ==3:
					self.i_bms.append([layer[0].encode('latin1'),layer[1],layer[2]])
			else:
				if isinstance(layer, dict):
					y = layer['py/tuple']
					if len(y)==2:
						self.i_bms.append([y[0],y[1]])
					elif len(y)==3:
						self.i_bms.append([y[0].encode('latin1'),y[1],y[2]])
					else:
						print 'something going wrong..'
						pass
		self.pub = []
		pub = json_obj['pub']
		for p in pub:
			if isinstance(p, dict):
				y = p['py/tuple']
				r = []
				for x in y[0]:
					r.append(x.encode('latin1'))
				self.pub.append([r, y[1].encode('latin1')])
			elif isinstance(p, unicode):
				self.pub.append(p.encode('latin1'))
			else:
				self.pub.append(p)
		self.txfrom = json_obj['txfrom'].encode('latin1')
		signature = json_obj['signature']
		self.signature = []
		for sig in signature:								
			self.signature.append(sig.encode('latin1'))		#encode('latin1') converts unicode back to UTF-8..
		self.i = json_obj['i']
		path = json_obj['merkle_path']
		self.merkle_path = []
		for auth in path:
			self.merkle_path.append(auth.encode('latin1'))
		self.PK = []										#required as jsonpickle is buggy..
		PK = json_obj['PK']
		if len(PK) == 2:
			for p in PK:
				self.PK.append(p.encode('latin1'))
		elif len(PK) == 168:
				self.PK = ast.literal_eval(PK)

class CreateSimpleTransaction(): 			#creates a transaction python class object which can be jsonpickled and sent into the p2p network..
	def __init__(self, txfrom, txto, nonce, amount, data, fee=0, ots_key=0, hrs=''):
		
		self.txfrom = txfrom
		self.nonce = nonce 
		self.txto = txto
		self.amount = int(amount)
		self.fee = int(fee)
		self.ots_key = ots_key
		self.txhash = sha256(''.join(self.txfrom+str(self.nonce)+self.txto+str(self.amount)+str(self.fee)))	

		if type(data) == list:
			self.type = data[ots_key].type
			self.pub = data[ots_key].pub
			self.signature = merkle.sign_mss(data, self.txhash, self.ots_key)
			self.verify = merkle.verify_mss(self.signature, data, self.txhash, self.ots_key)
			self.merkle_root = ''.join(data[0].merkle_root)
			self.merkle_path = data[ots_key].merkle_path

		else:		#xmss
			self.type = data.type
			S = data.SIGN(self.txhash)				# Sig = {i, s, auth_route, i_bms, self.pk(i), self.PK_short}
			self.i = S[0]
			self.signature = S[1]
			self.merkle_path = S[2]
			self.i_bms = S[3]
			self.pub = S[4]
			self.PK = S[5]
			#print self.PK
			self.merkle_root = data.root
			self.verify = data.VERIFY(self.txhash, S)
	

def creategenesisblock():
	return CreateGenesisBlock()


class BlockHeader():
	def __init__(self,  blocknumber, hashchain_link, prev_blockheaderhash, number_transactions, hashedtransactions, number_stake, hashedstake):
		self.blocknumber = blocknumber
		self.hash = hashchain_link
		if self.blocknumber == 0:
			self.timestamp = 0
		else:
			self.timestamp = time()
		self.prev_blockheaderhash = prev_blockheaderhash
		self.number_transactions = number_transactions
		self.hashedtransactions = hashedtransactions
		self.number_stake = number_stake
		self.hashedstake = hashedstake
		if self.blocknumber == 0:
			self.stake_selector = ''
			self.stake_nonce = 0
			self.block_reward = 0
			self.epoch = 0
		else:
			self.epoch = m_blockchain[-1].blockheader.blocknumber+1/10000
			self.stake_nonce = 10000-hash_chain.index(hashchain_link)				#****UPDATE WHEN HASH_CHAIN is moved to wallet..at block1 better to take from stake_list_get ****
			self.stake_selector = mining_address
			self.block_reward = block_reward(self.blocknumber)
		self.headerhash = sha256(self.stake_selector+str(self.epoch)+str(self.stake_nonce)+str(self.block_reward)+str(self.timestamp)+self.hash+str(self.blocknumber)+self.prev_blockheaderhash+str(self.number_transactions)+self.hashedtransactions+str(self.number_stake)+self.hashedstake)




class CreateBlock():
	def __init__(self, hashchain_link):
		#difficulty = 232
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
		
		if not stake_pool:
			hashedstake = sha256('')
		else:
			sthashes = []
			for st in stake_pool:
				sthashes.append(st.hash)
			hashedstake = sha256(''.join(sthashes))
		self.stake = []
		for st in stake_pool:
			self.stake.append(st)

		self.blockheader = BlockHeader(blocknumber=lastblocknumber+1, hashchain_link=hashchain_link, prev_blockheaderhash=prev_blockheaderhash, number_transactions=len(transaction_pool), hashedtransactions=hashedtransactions, number_stake=len(stake_pool), hashedstake=hashedstake)


class CreateGenesisBlock():			#first block has no previous header to reference..
	def __init__(self):
		self.blockheader = BlockHeader(blocknumber=0, hashchain_link='genesis', prev_blockheaderhash=sha256('quantum resistant ledger'),number_transactions=0, hashedtransactions=sha256('0'), number_stake=0, hashedstake=sha256('0'))
		self.transactions = []
		self.stake = []
		self.state = [['Q60e2ade04e249adcf85e95743f2c3b3d46cfce9121fec3f81ed4696789cb28ce235e', [0, 100000*100000000, []]] , ['Q6e7ea4ac974303517b5bbf689e7331001313a15c6547414adca1d61d60aa9c8b078b',[0, 10000*100000000,[]]]]
		self.stake_list = ['Q60e2ade04e249adcf85e95743f2c3b3d46cfce9121fec3f81ed4696789cb28ce235e', 'Q6e7ea4ac974303517b5bbf689e7331001313a15c6547414adca1d61d60aa9c8b078b']
		self.stake_seed = '1a02aa2cbe25c60f491aeb03131976be2f9b5e9d0bc6b6d9e0e7c7fd19c8a076c29e028f5f3924b4'


# JSON -> python class obj ; we can improve this with looping type check and encode if str and nest deeper if list > 1 (=1 ''join then encode)

class ReCreateSimpleTransaction():			#recreate from JSON avoiding pickle reinstantiation of the class..
	def __init__(self, json_obj):
		
		self.type = json_obj['type'].encode('latin1')

		if self.type != 'XMSS':

			self.nonce = json_obj['nonce']
			self.fee = int(json_obj['fee'])
			self.verify = json_obj['verify']
			self.merkle_root = json_obj['merkle_root'].encode('latin1')
			self.amount = int(json_obj['amount'])
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
			#self.hrs = json_obj['hrs'].encode('latin1')
		
		else:	#xmss

			self.nonce = json_obj['nonce']
			self.fee = int(json_obj['fee'])
			self.i_bms = []
			for layer in json_obj['i_bms']:
				if len(layer) ==2:
					self.i_bms.append([layer[0],layer[1]])
				elif len(layer) ==3:
					self.i_bms.append([layer[0].encode('latin1'),layer[1],layer[2]])
				else:
					if isinstance(layer, dict):
						y = layer['py/tuple']
						if len(y)==2:
							self.i_bms.append([y[0],y[1]])
						elif len(y)==3:
							self.i_bms.append([y[0].encode('latin1'),y[1],y[2]])
						else:
							print 'something going wrong..'
							pass

			self.verify = json_obj['verify']
			self.merkle_root = json_obj['merkle_root'].encode('latin1')
			self.amount = int(json_obj['amount'])
			
			self.pub = []
			pub = json_obj['pub']
			for p in pub:
				if isinstance(p, dict):
					y = p['py/tuple']
					r = []
					for x in y[0]:
						r.append(x.encode('latin1'))
					self.pub.append([r, y[1].encode('latin1')])
				elif isinstance(p, unicode):
					self.pub.append(p.encode('latin1'))
				else:
					self.pub.append(p)
			
			self.ots_key = json_obj['ots_key']
			self.txhash = json_obj['txhash'].encode('latin1')
			self.txto = json_obj['txto'].encode('latin1')
			self.txfrom = json_obj['txfrom'].encode('latin1')
			signature = json_obj['signature']
			self.signature = []
			for sig in signature:								
				self.signature.append(sig.encode('latin1'))		#encode('latin1') converts unicode back to UTF-8..
			self.i = json_obj['i']
			path = json_obj['merkle_path']
			self.merkle_path = []
			for auth in path:
				self.merkle_path.append(auth.encode('latin1'))
			self.PK = []										#required as jsonpickle is buggy..
			PK = json_obj['PK']
			if len(PK) == 2:
				for p in PK:
					self.PK.append(p.encode('latin1'))
			elif len(PK) == 168:
					self.PK = ast.literal_eval(PK)
			#strip out later
			#self.hrs = json_obj['hrs'].encode('latin1')


class ReCreateBlock():						#recreate block class from JSON variables for processing
	def __init__(self, json_block):
		self.blockheader = ReCreateBlockHeader(json_block['blockheader'])
	
		transactions = json_block['transactions']
		self.transactions = []
		for tx in transactions:
			self.transactions.append(ReCreateSimpleTransaction(tx))
#			self.transactions.append(json_decode_tx(json.dumps(tx)))

		stake = json_block['stake']
		self.stake = []
		for st in stake:
			self.stake.append(ReCreateStakeTransaction(st))

class ReCreateBlockHeader():
	def __init__(self, json_blockheader):
		self.stake_nonce = json_blockheader['stake_nonce']
		self.epoch = json_blockheader['epoch']
		self.headerhash = json_blockheader['headerhash'].encode('latin1')
		self.number_transactions = json_blockheader['number_transactions']
		self.number_stake = json_blockheader['number_stake']
		self.hash = json_blockheader['hash'].encode('latin1')
		self.timestamp = json_blockheader['timestamp']
		self.hashedtransactions = json_blockheader['hashedtransactions'].encode('latin1')
		self.hashedstake = json_blockheader['hashedstake'].encode('latin1')
		self.blocknumber = json_blockheader['blocknumber']
		self.prev_blockheaderhash = json_blockheader['prev_blockheaderhash'].encode('latin1')
		self.stake_selector = json_blockheader['stake_selector'].encode('latin1')
		self.block_reward = json_blockheader['block_reward']

# address functions

# for xmss

def xmss_rootoaddr(PK_short):
	return 'Q'+sha256(PK_short[0]+PK_short[1])+sha256(sha256(PK_short[0]+PK_short[1]))[:4]

def xmss_checkaddress(PK_short, address):
	if 'Q'+sha256(PK_short[0]+PK_short[1])+sha256(sha256(PK_short[0]+PK_short[1]))[:4] == address:
		return True
	return False

# for mss

def roottoaddr(merkle_root):
	return 'Q'+sha256(merkle_root)+sha256(sha256(merkle_root))[:4]

def checkaddress(merkle_root, address):
	if 'Q'+sha256(merkle_root)+sha256(sha256(merkle_root))[:4] == address:
		return True
	return False

# block reward calculation
# decay curve: 200 years (until 2217AD, 420480000 blocks at 15s block-times)
# N_tot is less the initial coin supply.

def calc_coeff(N_tot, block_tot):
	# lambda = Ln N_0 - Ln (N(t)) / t
	return log(N_tot)/block_tot

# calculate remaining emission at block_n: N=total initial coin supply, coeff = decay constant
# need to use decimal as floating point not precise enough on different platforms..

#def remaining_emission(N_tot,block_n):
#	coeff = calc_coeff(21000000, 420480000)
	# N_t = N_0.e^{-coeff.t} where t = block
#	return N_tot*e**(-coeff*block_n)

def remaining_emission(N_tot, block_n):
	coeff = calc_coeff(21000000, 420480000)
	return decimal.Decimal(N_tot*decimal.Decimal(-coeff*block_n).exp()).quantize(decimal.Decimal('1.00000000'), rounding=decimal.ROUND_HALF_UP)

# return block reward for the block_n 

def block_reward(block_n):
	return int((remaining_emission(21000000, block_n-1)-remaining_emission(21000000, block_n))*100000000)

# network serialising functions

def json_decode_st(json_tx):
	return ReCreateStakeTransaction(json.loads(json_tx))

def json_decode_tx(json_tx):										#recreate transaction class object safely 
	return ReCreateSimpleTransaction(json.loads(json_tx))

def json_decode_block(json_block):
	return ReCreateBlock(json.loads(json_block))

def json_encode(obj):
	return json.dumps(obj)

def json_decode(js_obj):
	return json.loads(js_obj)

def json_bytestream(obj):	
	return jsonpickle.encode(obj, make_refs=False)						#annoying bug!!!

def json_bytestream_tx(tx_obj):											#JSON serialise tx object
	return 'TX'+json_bytestream(tx_obj)

def json_bytestream_bk(block_obj):										# "" block object
	return 'BK'+json_bytestream(block_obj)

def json_print(obj):													#prettify output from JSON for export purposes
	print json.dumps(json.loads(jsonpickle.encode(obj, make_refs=False)), indent=4)

def json_print_telnet(obj):
	return json.dumps(json.loads(jsonpickle.encode(obj, make_refs=False)), indent=4)

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
			tx_new = copy.deepcopy(tx)
			tx_new.block = 'unconfirmed'
			tx_new.hexsize = len(json_bytestream(tx_new))
			tx_new.status = 'ok'
			return json_print_telnet(tx_new)
	for block in m_blockchain:
		for tx in block.transactions:
			if tx.txhash== txhash:
				tx_new = copy.deepcopy(tx)
				tx_new.block = block.blockheader.blocknumber
				tx_new.timestamp = block.blockheader.timestamp
				tx_new.confirmations = m_blockheight()-block.blockheader.blocknumber
				tx_new.hexsize = len(json_bytestream(tx_new))
				tx_new.amount = tx_new.amount/100000000.000000000
				tx_new.fee = tx_new.fee/100000000.000000000
				print txhash, 'found in block',str(block.blockheader.blocknumber),'..'
				tx_new.status = 'ok'
				return json_print_telnet(tx_new)
	print txhash, 'does not exist in memory pool or local blockchain..'
	err = {'status' : 'Error', 'error' : 'txhash not found', 'method' : 'txhash', 'parameter' : txhash}
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
		addr['state']['balance'] = balance/100000000.000000000
		addr['state']['nonce'] = nonce
		#pubhashes used could be put here..

	for tx in transaction_pool:
		if tx.txto == address or tx.txfrom == address:
			print address, 'found in transaction pool'
			addr['transactions'][tx.txhash] = {}
			addr['transactions'][tx.txhash]['txhash'] = tx.txhash
			addr['transactions'][tx.txhash]['block'] = 'unconfirmed'
			addr['transactions'][tx.txhash]['amount'] = tx.amount/100000000.000000000
			addr['transactions'][tx.txhash]['fee'] = tx.fee/100000000.000000000
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
			addr['transactions'][tx.txhash]['block'] = block.blockheader.blocknumber
			addr['transactions'][tx.txhash]['timestamp'] = block.blockheader.timestamp
			addr['transactions'][tx.txhash]['amount'] = tx.amount/100000000.000000000
			addr['transactions'][tx.txhash]['fee'] = tx.fee/100000000.000000000
			addr['transactions'][tx.txhash]['nonce'] = tx.nonce
			addr['transactions'][tx.txhash]['ots key'] = tx.ots_key
			addr['transactions'][tx.txhash]['txto'] = tx.txto
			addr['transactions'][tx.txhash]['txfrom'] = tx.txfrom	

	if len(addr['transactions']) > 0:
		addr['state']['transactions'] = len(addr['transactions'])
	

	if addr == {'transactions': {}}:
		addr = {'status': 'error', 'error' : 'address not found', 'method' : 'address', 'parameter' : address}
	else:
		addr['status'] = 'ok'


	return json_print_telnet(addr)

# return json info on last n tx in the blockchain

def last_tx(n=None):

	addr = {}
	addr['transactions'] = {}

	error = {'status': 'error', 'error' : 'invalid argument', 'method' : 'last_tx', 'parameter' : n}

	if not n:
		n = 1

	try: 	n = int(n)
 	except: return json_print_telnet(error)

 	if n <= 0 or n > 20:
 		return json_print_telnet(error)

 	if len(transaction_pool) != 0:
 		if n-len(transaction_pool) >=0:		# request bigger than tx in pool
 			z = len(transaction_pool)
 			n = n-len(transaction_pool)
 		elif n-len(transaction_pool) <=0:	# request smaller than tx in pool..
 			z = n
 			n = 0
 	
 	 	for tx in reversed(transaction_pool[-z:]):
 	 		addr['transactions'][tx.txhash] = {}
 	 		addr['transactions'][tx.txhash]['txhash'] = tx.txhash
			addr['transactions'][tx.txhash]['block'] = 'unconfirmed'
			addr['transactions'][tx.txhash]['timestamp'] = 'unconfirmed'
			addr['transactions'][tx.txhash]['amount'] = tx.amount/100000000.000000000
			addr['transactions'][tx.txhash]['type'] = tx.type

		if n == 0:
			addr['status'] = 'ok'
			return json_print_telnet(addr)


	for block in reversed(m_blockchain):
			if len(block.transactions) > 0:
				for tx in reversed(block.transactions):
					addr['transactions'][tx.txhash] = {}
 	 				addr['transactions'][tx.txhash]['txhash'] = tx.txhash
					addr['transactions'][tx.txhash]['block'] = block.blockheader.blocknumber
					addr['transactions'][tx.txhash]['timestamp'] = block.blockheader.timestamp
					addr['transactions'][tx.txhash]['amount'] = tx.amount/100000000.000000000
					addr['transactions'][tx.txhash]['type'] = tx.type
					n-=1
					if n == 0:
						addr['status'] = 'ok'
						return json_print_telnet(addr)
	return json_print_telnet(error)

def richlist(n=None):			#only feasible while chain is small..
	if not n:
		n = 5

	error = {'status': 'error', 'error' : 'invalid argument', 'method' : 'richlist', 'parameter' : n}

	try: n=int(n)
	except: return json_print_telnet(error)

	if n<=0 or n > 20:
		return json_print_telnet(error)

	if state_uptodate()==False:
		return json_print_telnet({'status': 'error', 'error': 'leveldb failed', 'method': 'richlist'})

	addr = db.return_all_addresses()
	richlist = sorted(addr, key=itemgetter(1), reverse=True)

	rl = {}
	rl['richlist'] = {}

	if len(richlist) < n:
		n = len(richlist)

	for rich in richlist[:n]:
		rl['richlist'][richlist.index(rich)+1] = {}
		rl['richlist'][richlist.index(rich)+1]['address'] = rich[0]
		rl['richlist'][richlist.index(rich)+1]['balance'] = rich[1]/100000000.000000000

	rl['status'] = 'ok'

	return json_print_telnet(rl)

# return json info on last n blocks

def last_block(n=None):

	if not n:
		n = 1

	error = {'status': 'error', 'error' : 'invalid argument', 'method' : 'last_block', 'parameter' : n}

	try: 	n=int(n)
	except: return json_print_telnet(error)	

	if n <= 0 or n > 20:
		return json_print_telnet(error)

	lb = m_blockchain[-n:]

	last_blocks = {}
	last_blocks['blocks'] = {}

	for block in reversed(lb):

		last_blocks['blocks'][block.blockheader.blocknumber] = {}
		last_blocks['blocks'][block.blockheader.blocknumber]['blocknumber'] = block.blockheader.blocknumber
		last_blocks['blocks'][block.blockheader.blocknumber]['blockhash'] = block.blockheader.prev_blockheaderhash
		last_blocks['blocks'][block.blockheader.blocknumber]['number transactions'] = block.blockheader.number_transactions
		last_blocks['blocks'][block.blockheader.blocknumber]['timestamp'] = block.blockheader.timestamp
		last_blocks['blocks'][block.blockheader.blocknumber]['block interval'] = block.blockheader.timestamp - m_blockchain[block.blockheader.blocknumber-1].blockheader.timestamp

	last_blocks['status'] = 'ok'

	return json_print_telnet(last_blocks)

# return json info on stake_commit list

def stake_commits(data=None):

	sc = {}
	sc['status'] = 'ok'
	sc['commits'] = {}

	for c in stake_commit:
		#[stake_address, block_number, merkle_hash_tx, commit_hash]
		sc['commits'][str(c[1])+'-'+c[3]] = {}
		sc['commits'][str(c[1])+'-'+c[3]]['stake_address'] = c[0]
		sc['commits'][str(c[1])+'-'+c[3]]['block_number'] = c[1]
		sc['commits'][str(c[1])+'-'+c[3]]['merkle_hash_tx'] = c[2]
		sc['commits'][str(c[1])+'-'+c[3]]['commit_hash'] = c[3]


	return json_print_telnet(sc)

def stakers(data=None):

	stakers = {}

	return json_print_telnet(stakers)

def stake_reveals(data=None):

	sr = {}
	sr['status'] = 'ok'
	sr['reveals'] = {}
	#chain.stake_reveal.append([stake_address, block_number, merkle_hash_tx, reveal])
	for c in stake_reveal:
		sr['reveals'][str(c[1])+'-'+c[3]] = {}
		sr['reveals'][str(c[1])+'-'+c[3]]['stake_address'] = c[0]
		sr['reveals'][str(c[1])+'-'+c[3]]['block_number'] = c[1]
		sr['reveals'][str(c[1])+'-'+c[3]]['merkle_hash_tx'] = c[2]
		sr['reveals'][str(c[1])+'-'+c[3]]['reveal'] = c[3]

	return json_print_telnet(sr)

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

	if validate_block(block_obj, new=1) is True:
		m_blockchain.append(block_obj)
		if state_add_block(m_blockchain[-1]) is True:
				remove_tx_in_block_from_pool(block_obj)
				remove_st_in_block_from_pool(block_obj)
		else: 	
				m_remove_last_block()
				print 'last block failed state/stake checks, removed from chain'
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

def stake_list_get():
	try: return db.get('stake_list')
	except: return []

def stake_list_put(sl):
	try: db.put('stake_list', sl)
	except: return False

def next_stake_list_get():
	try: return db.get('next_stake_list')
	except: return []

def next_stake_list_put(next_sl):
	try: db.put('next_stake_list', next_sl)
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
	elif tx.type == 'XMSS':
		 pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]
	
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
		elif txn.type == 'XMSS':
			pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]

		pubhashn = sha256(''.join(pub))

		if pubhashn == pubhash:
			print 'State validation failed for', tx.txhash, 'because: OTS Public key re-use detected'
			return False


	if pubhash in state_pubhash(tx.txfrom):
			print 'State validation failed for', tx.txhash, 'because: OTS Public key re-use detected'
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

# validate and update stake+state for newly appended block.

def state_add_block(block):

	assert state_blockheight() == m_blockheight()-1, 'state leveldb not @ m_blockheight-1'

	#snapshot of state in case we need to revert to it..

	st1 = []	
	st2 = []
	st3 = state_get_address(block.blockheader.stake_selector)	# to roll back

	st4 = stake_list_get()		# to roll back
	st5 = []

	for st in block.stake:
		st5.append(state_get_address(st.txfrom))	# to roll back

	for tx in block.transactions:
		st1.append(state_get_address(tx.txfrom))
		st2.append(state_get_address(tx.txto))

	y = 0
	
	# first the coinbase address is updated

	db.put(block.blockheader.stake_selector, [st3[0],st3[1]+block.blockheader.block_reward,st3[2]])

	# reminder contents: (state address -> nonce, balance, [pubhash]) (stake -> address, hash_term, nonce)

	# if block 1: 

	if block.blockheader.blocknumber == 1:

		stake_list = []
		sl = []
		next_sl = []

		for st in block.stake:

			if st.txfrom == block.blockheader.stake_selector:			#update txfrom, hash and stake_nonce against genesis for current or next stake_list
				if st.txfrom in m_blockchain[0].stake_list:
					sl.append([st.txfrom, st.hash, 1])
				else:
					print 'designated staker not in genesis..'
					y=-1000												#triggers block fail..
			else:
				if st.txfrom in m_blockchain[0].stake_list:
					sl.append([st.txfrom, st.hash, 0])
				else:
					next_sl.append([st.txfrom, st.hash, 0])

			z = state_get_address(st.txfrom)

			pub = st.pub
			pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]
			pubhash = sha256(''.join(pub))

			db.put(st.txfrom, [z[0]+1, z[1], z[2].append(pubhash)])	#update the statedb for txfrom's
		
		stake_list = sorted(sl, key=itemgetter(1))
		stake_list_put(stake_list)
		next_stake_list_put(sorted(next_sl, key=itemgetter(1)))
		numlist(stake_list)

	else:
		if block.blockheader.epoch == m_blockchain[-1].blockheader.epoch:	#same epoch..
			stake_list = []
			prf_epoch = []
			next_sl = next_stake_list_get()
			sl = stake_list_get()
			u=0
			
			#increase the stake_nonce of state selector..must be in stake list..

			for s in sl:													
				if block.blockheader.stake_selector == s[0]:
					u=1
					s[2]+=1
					if s[2] != block.blockheader.stake_nonce:
						print 'stake_nonce wrong..'
						y=-1000
					else:
						stake_list_put(sl)
			if u != 1:
				y=-1000
				print 'stake selector not in stake_list_get'

			#confirm that the state selector is correctly chosen by PRF from seed..

			epoch_prf = pos_block_selector(m_blockchain[block.blockheader.epoch*10000].stake_seed, len(sl))		#need to add a stake_seed option in block classes
			if sl[epoch_prf[block.blockheader.blocknumber-block.blockheader.epoch*10000]][0] != block.blockheader.stake_selector:
				print 'stake selector wrong..'
				y=-1000

			# update and re-order the next_stake_list:

			for st in block.stake:
				pub = st.pub
				pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]
				pubhash = sha256(''.join(pub))

				u=0

				for s in next_sl:
					if st.txfrom == s[0]:		#already in the next stake list, ignore for staker list but update as usual the state_for_address..
						z = state_get_address(st.txfrom)
						db.put(st.txfrom, [z[0]+1, z[1], z[2].append(pubhash)])	#update the statedb for txfrom's
						u=1

				if u==0:
					z = state_get_address(st.txfrom)
					db.put(st.txfrom, [z[0]+1, z[1], z[2].append(pubhash)])
					next_sl.append([st.txfrom, st.hash, 0])

			next_stake_list_put(sorted(next_sl, key=itemgetter(1)))

		else:
			pass
	# if epoch transition..del state_list, next_state_list = state_list, and next_state_list = [], then for each st.txfrom update next_state_list (addr, hash, 0), update
	# state (nonce, pubhash, amount unchanged..)

	# cycle through every tx in the new block to check state
		
	for tx in block.transactions:

		pub = tx.pub
		if tx.type == 'LDOTS':
				pub = [i for sub in pub for i in sub]
		elif tx.type == 'XMSS':
				pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]

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

		# add a check to prevent spend from stake address..

		s1[0]+=1
		s1[1] = s1[1]-tx.amount
		s1[2].append(pubhash)
		db.put(tx.txfrom, s1)

		s2 = state_get_address(tx.txto)
		s2[1] = s2[1]+tx.amount
		db.put(tx.txto, s2)

		y+=1

	if y<len(block.transactions):			# if we havent done all the tx in the block we have break, need to revert state back to before the change.
		print 'failed to state check entire block'
		print 'reverting state'

		for x in range(len(block.transactions)):
			db.put(block.transactions[x].txfrom, st1[x])
			db.put(block.transactions[x].txto, st2[x])
		db.put(block.blockheader.coinbase, st3)		

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

		# update coinbase address state
		stake_selector = state_get_address(block.blockheader.stake_selector)
		stake_selector[1]+=block.blockheader.block_reward
		db.put(block.blockheader.stake_selector, stake_selector)

		for tx in block.transactions:
			pub = tx.pub
			if tx.type == 'LDOTS':
				  	pub = [i for sub in pub for i in sub]
			elif tx.type == 'XMSS':
					pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]

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

			s1[0]+=1
			s1[1] = s1[1]-tx.amount
			s1[2].append(pubhash)
			db.put(tx.txfrom, s1)							#must be ordered in case tx.txfrom = tx.txto

			s2 = state_get_address(tx.txto)
			s2[1] = s2[1]+tx.amount
			
			db.put(tx.txto, s2)

		print block, str(len(block.transactions)), 'tx ', ' passed'
	db.put('blockheight', m_blockheight())
	return True

#tx functions and classes

def createsimpletransaction(txfrom, txto, amount, data, fee=0, hrs=''):				#NEED TO SORT THIS FUNCTION OUT!

	#few state checks to ensure tx is valid, including tx already in the transaction_pool
	#need to avoid errors in nonce and public key re-use which will invalidate the tx at other nodes

	if state_uptodate() is False:
			msg = 'state not at latest block in chain'
			print msg
			return (False, msg)

	if state_balance(txfrom) is 0:	#not necessary
			msg = 'empty address'
			print msg
			return (False, msg) 

	if state_balance(txfrom) < amount: 
			msg = 'insufficient funds for valid tx'
			print msg
			return (False, msg)
	
	#if len(hrs) != 0:
	#	if hrs[0] == 'Q':
	#		msg = 'cannot start a human readable string with "Q"'
	#		print msg
	#		return (False, msg)
	#	if state_hrs(hrs) is not False:
	#		msg = 'human readable string already associated with another address'
	#		print msg
	#		return (False, msg)
	
	# signatures remaining is important to check - once all the public keys are used then any funds left will be frozen and unspendable..

	nonce = state_nonce(txfrom)+1

	for t in transaction_pool:
		if t.txfrom == txfrom:
				nonce+=1

	if type(data) == list:
		s = data[0].signatures-nonce
	else:	#xmss
		s = data.remaining

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

	if type(data) == list:
		ots_key = nonce-1		#nonce for first tx from an address is 1, first ots signature is 0..
	else: #xmss
		ots_key = data.index

	if type(data) == list:
		for pubhash in state_pubhash(txfrom):
		 if pubhash == data[ots_key].pubhash:
			msg = 'Wallet error: pubhash at ots_key has already been used. Compose a transaction manually and move funds to a new address.'
			print msg
			return (False, msg)
	else:	#xmss
		for pubhash in state_pubhash(txfrom):
		 	pub = data.pk(ots_key)
		 	pub = [''.join(pub[0][0]),pub[0][1],''.join(pub[2:])]
			if pubhash == sha256(''.join(pub)):
		 		msg = 'Wallet error: pubhash at ots_key has already been used. Compose a transaction manually and move funds to a new address.'
				print msg
				return (False, msg)

	return (CreateSimpleTransaction(txfrom=txfrom, txto=txto, amount=amount, nonce=nonce, data=data, fee=fee, ots_key=ots_key, hrs=hrs), msg)

def add_tx_to_pool(tx_class_obj):
	transaction_pool.append(tx_class_obj)
	txhash_timestamp.append(tx_class_obj.txhash)
	txhash_timestamp.append(time())

def add_st_to_pool(st_class_obj):
	stake_pool.append(st_class_obj)

def remove_tx_from_pool(tx_class_obj):
	transaction_pool.remove(tx_class_obj)

def remove_st_from_pool(st_class_obj):
	stake_pool.remove(st_class_obj)
	

def show_tx_pool():
	return transaction_pool

def remove_tx_in_block_from_pool(block_obj):
	for tx in block_obj.transactions:
		for txn in transaction_pool:
			if tx.txhash == txn.txhash:
				remove_tx_from_pool(txn)

def remove_st_in_block_from_pool(block_obj):
	for st in block_obj.stake:
		for stn in stake_pool:
			if st.hash == stn.hash:
				remove_st_from_pool(stn)

def flush_tx_pool():
	del transaction_pool[:]

def flush_st_pool():
	del stake_pool[:]

def validate_tx_in_block(block_obj, new=0):
	x = 0
	for transaction in block_obj.transactions:
		if validate_tx(transaction, new=new) is False:
			print 'invalid tx: ',transaction, 'in block'
			x+=1
	if x > 0:
		return False
	return True

def validate_st_in_block(block_obj):
	x = 0
	for st in block_obj.stake:
		if validate_st(st) is False:
			print 'invalid st:', st, 'in block'
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


def validate_st(tx):

	if tx.type != 'XMSS/STAKE':
		return False

	if merkle.xmss_verify(tx.hash, [tx.i, tx.signature, tx.merkle_path, tx.i_bms, tx.pub, tx.PK]) is False:
			return False
	if xmss_checkaddress(tx.PK, tx.txfrom) is False:
			return False

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
	# SIG is a list composed of: i, s, auth_route, i_bms, pk[i], PK
	elif tx.type == 'XMSS':

		if merkle.xmss_verify(tx.txhash, [tx.i, tx.signature, tx.merkle_path, tx.i_bms, tx.pub, tx.PK]) is False:
			return False
		if xmss_checkaddress(tx.PK, tx.txfrom) is False:
			return False
	else: 
		return False

	if tx.type != 'XMSS':
		if checkaddress(tx.merkle_root, tx.txfrom) is False:
			return False
		if merkle.verify_root(tx.pub, tx.merkle_root, tx.merkle_path) is False:
			return False
			
	return True

# block validation

def validate_block(block, last_block='default', verbose=0, new=0):		#check validity of new block..

	b = block.blockheader

	if b.block_reward != block_reward(b.blocknumber):
		print 'Block reward incorrect for block: failed validation'
		return False

	if b.epoch != b.blocknumber/10000:
		print 'Epoch incorrect for block: failed validation'

	if b.blocknumber == 1:
		x=0
		for st in block.stake:
			if st.txfrom == b.stake_selector:
				x = 1
				if sha256(b.hash) != st.hash:
					print 'Hashchain_link does not hash correctly to terminator: failed validation'
					return False
		if x != 1:
			print 'Stake selector not in block.stake: failed validation'
			return False
	else:		# we look in stake_list for the hash terminator and hash to it..
		y=0
		for st in stake_list:
			if st[0] == b.stake_selector:
					y = 1
					terminator = b.hash
					for x in range(b.stake_nonce):
						terminator = sha256(terminator)
					if terminator != st[1]:
						print 'Supplied hash does not iterate to terminator: failed validation'
						return False
			if y != 1:
				print 'Stake selector not in stake_list for this epoch..'
				return False
	

	if sha256(b.stake_selector+str(b.epoch)+str(b.stake_nonce)+str(b.block_reward)+str(b.timestamp)+b.hash+str(b.blocknumber)+b.prev_blockheaderhash+str(b.number_transactions)+b.hashedtransactions+str(b.number_stake)+b.hashedstake) != b.headerhash:
		print 'Headerhash false for block: failed validation'
		return False

	if last_block=='default':
		if m_get_last_block().blockheader.headerhash != block.blockheader.prev_blockheaderhash:
			print 'Headerhash not in sequence: failed validation'
			return False
		if m_get_last_block().blockheader.blocknumber != block.blockheader.blocknumber-1:
			print 'Block numbers out of sequence: failed validation'
			return False
	else:
		if m_get_block(last_block).blockheader.headerhash != block.blockheader.prev_blockheaderhash:
			print 'Headerhash not in sequence: failed validation'
			return False
		if m_get_block(last_block).blockheader.blocknumber != block.blockheader.blocknumber-1:
			print 'Block numbers out of sequence: failed validation'
			return False

	if validate_tx_in_block(block, new=new) == False:
		print 'Block validate_tx_in_block error: failed validation'
		return False

	if validate_st_in_block(block) == False:
		print 'Block validate_st_in_block error: failed validation'
		return False

	txhashes = []
	for transaction in block.transactions:
		txhashes.append(transaction.txhash)

	if sha256(''.join(txhashes)) != block.blockheader.hashedtransactions:
		print 'Block hashedtransactions error: failed validation'
		return False

	sthashes = []
	for st in block.stake:
		sthashes.append(st.hash)

	if sha256(''.join(sthashes)) != b.hashedstake:
		print 'Block hashedstake error: failed validation'

	if verbose==1:
		print block, 'True'

	return True


# simple transaction creation and wallet functions using the wallet file..

def wlt():
	return merkle.numlist(wallet.list_addresses())

def create_my_tx(txfrom, txto, n, fee=0):
	#my = wallet.f_read_wallet()
	if isinstance(txto, int):
		(tx, msg) = createsimpletransaction(txto=my[txto][0],txfrom=my[txfrom][0],amount=n, data=my[txfrom][1], fee=0)
	elif isinstance(txto, str):
		(tx, msg) = createsimpletransaction(txto=txto,txfrom=my[txfrom][0],amount=n, data=my[txfrom][1], fee=0)
	if tx is not False:
		#transaction_pool.append(tx)
		add_tx_to_pool(tx)
		wallet.f_save_winfo()	#need to keep state after tx ..use wallet.info to store index..far faster than loading the 5mb wallet..
		return (tx, msg)
	else:
		return (False, msg)



