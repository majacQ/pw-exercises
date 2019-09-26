from io import BytesIO
from unittest import TestCase

from helper import (
    decode_base58,
    encode_bech32_checksum,
    encode_varint,
    encode_varstr,
    h160_to_p2pkh_address,
    h160_to_p2sh_address,
    hash160,
    int_to_little_endian,
    little_endian_to_int,
    read_varint,
    sha256,
)
from op import (
    op_equal,
    op_hash160,
    op_verify,
    OP_CODE_FUNCTIONS,
    OP_CODE_NAMES,
)


def p2pkh_script(h160):
    '''Takes a hash160 and returns the p2pkh scriptPubKey'''
    return Script([0x76, 0xa9, h160, 0x88, 0xac])


def p2sh_script(h160):
    '''Takes a hash160 and returns the p2sh scriptPubKey'''
    return Script([0xa9, h160, 0x87])


def p2wpkh_script(h160):
    '''Takes a hash160 and returns the p2wpkh scriptPubKey'''
    return Script([0x00, h160])


def p2wsh_script(s256):
    '''Takes a hash160 and returns the p2wsh scriptPubKey'''
    return Script([0x00, s256])


class Script:

    def __init__(self, commands=None):
        if commands is None:
            self.commands = []
        else:
            self.commands = commands

    def __repr__(self):
        result = ''
        for command in self.commands:
            if type(command) == int:
                if OP_CODE_NAMES.get(command):
                    name = OP_CODE_NAMES.get(command)
                else:
                    name = 'OP_[{}]'.format(command)
                result += '{} '.format(name)
            else:
                result += '{} '.format(command.hex())
        return result

    def __add__(self, other):
        return Script(self.commands + other.commands)

    @classmethod
    def parse(cls, s):
        # get the length of the entire field
        length = read_varint(s)
        # initialize the commands array
        commands = []
        # initialize the number of bytes we've read to 0
        count = 0
        # loop until we've read length bytes
        while count < length:
            # get the current byte
            current = s.read(1)
            # increment the bytes we've read
            count += 1
            # convert the current byte to an integer
            current_byte = current[0]
            # if the current byte is between 1 and 75 inclusive
            if current_byte >= 1 and current_byte <= 75:
                # we have a command set n to be the current byte
                n = current_byte
                # add the next n bytes as a command
                commands.append(s.read(n))
                # increase the count by n
                count += n
            elif current_byte == 76:
                # op_pushdata1
                data_length = little_endian_to_int(s.read(1))
                commands.append(s.read(data_length))
                count += data_length + 1
            elif current_byte == 77:
                # op_pushdata2
                data_length = little_endian_to_int(s.read(2))
                commands.append(s.read(data_length))
                count += data_length + 2
            else:
                # we have an op code. set the current byte to op_code
                op_code = current_byte
                # add the op_code to the list of commands
                commands.append(op_code)
        if count != length:
            raise SyntaxError('parsing script failed')
        return cls(commands)

    def raw_serialize(self):
        # initialize what we'll send back
        result = b''
        # go through each command
        for command in self.commands:
            # if the command is an integer, it's an op code
            if type(command) == int:
                # turn the command into a single byte integer using int_to_little_endian
                result += int_to_little_endian(command, 1)
            else:
                # otherwise, this is an element
                # get the length in bytes
                length = len(command)
                # for large lengths, we have to use a pushdata op code
                if length < 75:
                    # turn the length into a single byte integer
                    result += int_to_little_endian(length, 1)
                elif length > 75 and length < 0x100:
                    # 76 is pushdata1
                    result += int_to_little_endian(76, 1)
                    result += int_to_little_endian(length, 1)
                elif length >= 0x100 and length <= 520:
                    # 77 is pushdata2
                    result += int_to_little_endian(77, 1)
                    result += int_to_little_endian(length, 2)
                else:
                    raise ValueError('too long a command')
                result += command
        return result

    def serialize(self):
        # get the raw serialization (no prepended length)
        result = self.raw_serialize()
        # encode_varstr the result
        return encode_varstr(result)

    def evaluate(self, z, witness):
        # create a copy as we may need to add to this list if we have a
        # RedeemScript
        commands = self.commands[:]
        stack = []
        altstack = []
        while len(commands) > 0:
            command = commands.pop(0)
            if type(command) == int:
                # do what the op code says
                operation = OP_CODE_FUNCTIONS[command]
                if command in (99, 100):
                    # op_if/op_notif require the commands array
                    if not operation(stack, commands):
                        print('bad op: {}'.format(OP_CODE_NAMES[command]))
                        return False
                elif command in (107, 108):
                    # op_toaltstack/op_fromaltstack require the altstack
                    if not operation(stack, altstack):
                        print('bad op: {}'.format(OP_CODE_NAMES[command]))
                        return False
                elif command in (172, 173, 174, 175):
                    # these are signing operations, they need a sig_hash
                    # to check against
                    if not operation(stack, z):
                        print('bad op: {}'.format(OP_CODE_NAMES[command]))
                        return False
                else:
                    if not operation(stack):
                        print('bad op: {}'.format(OP_CODE_NAMES[command]))
                        return False
            else:
                # add the command to the stack
                stack.append(command)
                # p2sh rule. if the next three commands are:
                # OP_HASH160 <20 byte hash> OP_EQUAL this is the RedeemScript
                # OP_HASH160 == 0xa9 and OP_EQUAL == 0x87
                if len(commands) == 3 and commands[0] == 0xa9 \
                    and type(commands[1]) == bytes and len(commands[1]) == 20 \
                    and commands[2] == 0x87:
                    redeem_script = encode_varstr(command)
                    # we execute the next three op codes
                    commands.pop()
                    h160 = commands.pop()
                    commands.pop()
                    if not op_hash160(stack):
                        return False
                    stack.append(h160)
                    if not op_equal(stack):
                        return False
                    # final result should be a 1
                    if not op_verify(stack):
                        print('bad p2sh h160 {} {} vs {}'.format(redeem_script.hex(), h160.hex(), hash160(command).hex()))
                        return False
                    # hashes match! now add the RedeemScript
                    stream = BytesIO(redeem_script)
                    commands.extend(Script.parse(stream).commands)
                # witness program version 0 rule. if stack commands are:
                # 0 <20 byte hash> this is p2wpkh
                if len(stack) == 2 and stack[0] == b'' and len(stack[1]) == 20:
                    h160 = stack.pop()
                    stack.pop()
                    commands.extend(witness)
                    commands.extend(p2pkh_script(h160).commands)
                # witness program version 0 rule. if stack commands are:
                # 0 <32 byte hash> this is p2wsh
                if len(stack) == 2 and stack[0] == b'' and len(stack[1]) == 32:
                    s256 = stack.pop()
                    stack.pop()
                    commands.extend(witness[:-1])
                    witness_script = witness[-1]
                    if s256 != sha256(witness_script):
                        print('bad sha256 {} vs {}'.format(s256.hex(), sha256(witness_script).hex()))
                        return False
                    # hashes match! now add the Witness Script
                    stream = BytesIO(encode_varint(len(witness_script)) + witness_script)
                    witness_script_commands = Script.parse(stream).commands
                    commands.extend(witness_script_commands)
        if len(stack) == 0:
            return False
        if stack.pop() == b'':
            return False
        return True

    def is_p2pkh_script_pubkey(self):
        '''Returns whether this follows the
        OP_DUP OP_HASH160 <20 byte hash> OP_EQUALVERIFY OP_CHECKSIG pattern.'''
        # there should be exactly 5 commands
        # OP_DUP (0x76), OP_HASH160 (0xa9), 20-byte hash, OP_EQUALVERIFY (0x88),
        # OP_CHECKSIG (0xac)
        return len(self.commands) == 5 and self.commands[0] == 0x76 \
            and self.commands[1] == 0xa9 \
            and type(self.commands[2]) == bytes and len(self.commands[2]) == 20 \
            and self.commands[3] == 0x88 and self.commands[4] == 0xac

    def is_p2sh_script_pubkey(self):
        '''Returns whether this follows the
        OP_HASH160 <20 byte hash> OP_EQUAL pattern.'''
        # there should be exactly 3 commands
        # OP_HASH160 (0xa9), 20-byte hash, OP_EQUAL (0x87)
        return len(self.commands) == 3 and self.commands[0] == 0xa9 \
            and type(self.commands[1]) == bytes and len(self.commands[1]) == 20 \
            and self.commands[2] == 0x87

    def is_p2wpkh_script_pubkey(self):
        '''Returns whether this follows the
        OP_0 <20 byte hash> pattern.'''
        return len(self.commands) == 2 and self.commands[0] == 0x00 \
            and type(self.commands[1]) == bytes and len(self.commands[1]) == 20

    def is_p2wsh_script_pubkey(self):
        '''Returns whether this follows the
        OP_0 <20 byte hash> pattern.'''
        return len(self.commands) == 2 and self.commands[0] == 0x00 \
            and type(self.commands[1]) == bytes and len(self.commands[1]) == 32

    def hash160(self):
        # if p2pkh
        if self.is_p2pkh_script_pubkey():  # p2pkh
            # hash160 is the 3rd command
            return self.commands[2]
        elif self.is_p2sh_script_pubkey():  # p2sh
            # hash160 is the 2nd command
            return self.commands[1]
        return None

    def address(self, testnet=False):
        '''Returns the address corresponding to the script'''
        # if p2pkh
        if self.is_p2pkh_script_pubkey():  # p2pkh
            # convert to p2pkh address using h160_to_p2pkh_address (remember testnet)
            return h160_to_p2pkh_address(self.hash160(), testnet)
        # if p2sh
        elif self.is_p2sh_script_pubkey():  # p2sh
            # convert to p2sh address using h160_to_p2sh_address (remember testnet)
            return h160_to_p2sh_address(self.hash160(), testnet)
        # raise a ValueError
        raise ValueError('Unknown ScriptPubKey')

    def p2sh_address(self, testnet=False):
        '''Assumes this is a RedeemScript. Returns the p2sh address.'''
        # get the hash160 of the current script's raw serialization
        h160 = hash160(self.raw_serialize())
        # convert this to a p2sh address
        return h160_to_p2sh_address(h160, testnet)

    def p2wsh_script_pubkey(self):
        '''Assumes the script is a WitnessScript, generates the ScriptPubKey'''
        # get the sha256 of the current script's raw serialization
        s256 = sha256(self.raw_serialize())
        # return new p2wsh script using p2wsh_script
        return p2wsh_script(s256)
    
    def p2wsh_address(self, testnet=False):
        '''Assumes the script is a WitnessScript, generates a p2wsh address'''
        # get the ScriptPubKey of the WitnessScript
        script_pubkey = self.p2wsh_script_pubkey()
        # calculate the raw serialization of the ScriptPubKey
        raw = script_pubkey.raw_serialize()
        # return the encoded bech32 address
        return encode_bech32_checksum(raw, testnet=testnet)

    def p2sh_p2wsh_address(self, testnet=False):
        '''Assumes the script is a WitnessScript, generates a p2sh-p2wsh address'''
        # the RedeemScript is the p2wsh ScriptPubKey
        redeem_script = self.p2wsh_script_pubkey()
        # return the p2sh address of the RedeemScript (remember testnet)
        return redeem_script.p2sh_address(testnet)


class ScriptTest(TestCase):

    def test_parse(self):
        script_pubkey = BytesIO(bytes.fromhex('6a47304402207899531a52d59a6de200179928ca900254a36b8dff8bb75f5f5d71b1cdc26125022008b422690b8461cb52c3cc30330b23d574351872b7c361e9aae3649071c1a7160121035d5c93d9ac96881f19ba1f686f15f009ded7c62efe85a872e6a19b43c15a2937'))
        script = Script.parse(script_pubkey)
        want = bytes.fromhex('304402207899531a52d59a6de200179928ca900254a36b8dff8bb75f5f5d71b1cdc26125022008b422690b8461cb52c3cc30330b23d574351872b7c361e9aae3649071c1a71601')
        self.assertEqual(script.commands[0].hex(), want.hex())
        want = bytes.fromhex('035d5c93d9ac96881f19ba1f686f15f009ded7c62efe85a872e6a19b43c15a2937')
        self.assertEqual(script.commands[1], want)

    def test_serialize(self):
        want = '6a47304402207899531a52d59a6de200179928ca900254a36b8dff8bb75f5f5d71b1cdc26125022008b422690b8461cb52c3cc30330b23d574351872b7c361e9aae3649071c1a7160121035d5c93d9ac96881f19ba1f686f15f009ded7c62efe85a872e6a19b43c15a2937'
        script_pubkey = BytesIO(bytes.fromhex(want))
        script = Script.parse(script_pubkey)
        self.assertEqual(script.serialize().hex(), want)

    def test_address(self):
        address_1 = '1BenRpVUFK65JFWcQSuHnJKzc4M8ZP8Eqa'
        h160 = decode_base58(address_1)
        p2pkh_script_pubkey = p2pkh_script(h160)
        self.assertEqual(p2pkh_script_pubkey.address(), address_1)
        address_2 = 'mrAjisaT4LXL5MzE81sfcDYKU3wqWSvf9q'
        self.assertEqual(p2pkh_script_pubkey.address(testnet=True), address_2)
        address_3 = '3CLoMMyuoDQTPRD3XYZtCvgvkadrAdvdXh'
        h160 = decode_base58(address_3)
        p2sh_script_pubkey = p2sh_script(h160)
        self.assertEqual(p2sh_script_pubkey.address(), address_3)
        address_4 = '2N3u1R6uwQfuobCqbCgBkpsgBxvr1tZpe7B'
        self.assertEqual(p2sh_script_pubkey.address(testnet=True), address_4)

    def test_p2sh_address(self):
        hex_raw_redeem_script = '475221022626e955ea6ea6d98850c994f9107b036b1334f18ca8830bfff1295d21cfdb702103b287eaf122eea69030a0e9feed096bed8045c8b98bec453e1ffac7fbdbd4bb7152ae'
        redeem_script = Script.parse(BytesIO(bytes.fromhex(hex_raw_redeem_script)))
        self.assertEqual(redeem_script.p2sh_address(), '3CLoMMyuoDQTPRD3XYZtCvgvkadrAdvdXh')

    def test_p2wsh_address(self):
        witness_script_hex = '52210375e00eb72e29da82b89367947f29ef34afb75e8654f6ea368e0acdfd92976b7c2103a1b26313f430c4b15bb1fdce663207659d8cac749a0e53d70eff01874496feff2103c96d495bfdd5ba4145e3e046fee45e84a8a48ad05bd8dbb395c011a32cf9f88053ae'
        witness_script = Script.parse(BytesIO(encode_varstr(bytes.fromhex(witness_script_hex))))
        want = 'bc1qwqdg6squsna38e46795at95yu9atm8azzmyvckulcc7kytlcckxswvvzej'
        self.assertEqual(witness_script.p2wsh_address(), want)

    def test_p2sh_p2wsh_address(self):
        witness_script_hex = '5221026ccfb8061f235cc110697c0bfb3afb99d82c886672f6b9b5393b25a434c0cbf32103befa190c0c22e2f53720b1be9476dcf11917da4665c44c9c71c3a2d28a933c352102be46dc245f58085743b1cc37c82f0d63a960efa43b5336534275fc469b49f4ac53ae'
        witness_script = Script.parse(BytesIO(encode_varstr(bytes.fromhex(witness_script_hex))))
        want = '2MvVx9ccWqyYVNa5Xz9pfCEVk99zVBZh9ms'
        self.assertEqual(witness_script.p2sh_p2wsh_address(testnet=True), want)