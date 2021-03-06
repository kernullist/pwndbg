#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Functionality for disassmebling code at an address, or at an
address +/- a few instructions.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections

import capstone
import gdb
from capstone import *

import pwndbg.arch
import pwndbg.disasm.arch
import pwndbg.ida
import pwndbg.jump
import pwndbg.memoize
import pwndbg.memory
import pwndbg.symbol

try:
    import pwndbg.emu.emulator
except:
    pwndbg.emu = None

disassembler = None
last_arch    = None

CapstoneArch = {
    'arm': CS_ARCH_ARM,
    'aarch64': CS_ARCH_ARM64,
    'i386': CS_ARCH_X86,
    'x86-64': CS_ARCH_X86,
    'powerpc': CS_ARCH_PPC,
    'mips': CS_ARCH_MIPS,
    'sparc': CS_ARCH_SPARC,
}

CapstoneEndian = {
    'little': CS_MODE_LITTLE_ENDIAN,
    'big': CS_MODE_BIG_ENDIAN,
}

CapstoneMode = {
    4: CS_MODE_32,
    8: CS_MODE_64
}

# For variable-instruction-width architectures
# (x86 and amd64), we keep a cache of instruction
# sizes, and where the end of the instruction falls.
#
# This allows us to consistently disassemble backward.
VariableInstructionSizeMax = {
    'i386':   16,
    'x86-64': 16,
    'mips':   8,
}

backward_cache = collections.defaultdict(lambda: None)

@pwndbg.memoize.reset_on_objfile
def get_disassembler_cached(arch, ptrsize, endian, extra=None):
    arch = CapstoneArch[arch]

    if extra is None:
        mode = CapstoneMode[ptrsize]
    else:
        mode = extra

    mode |= CapstoneEndian[endian]

    cs = Cs(arch, mode)
    cs.detail = True
    return cs

def get_disassembler(pc):
    extra = None

    if pwndbg.arch.current in ('arm', 'aarch64'):
        extra = {0:CS_MODE_ARM,
                 0x20:CS_MODE_THUMB}[pwndbg.regs.cpsr & 0x20]

    return get_disassembler_cached(pwndbg.arch.current,
                                   pwndbg.arch.ptrsize,
                                   pwndbg.arch.endian,
                                   extra)

@pwndbg.memoize.reset_on_cont
def get_one_instruction(address):
    md   = get_disassembler(address)
    size = VariableInstructionSizeMax.get(pwndbg.arch.current, 4)
    data = pwndbg.memory.read(address, size, partial=True)
    for ins in md.disasm(bytes(data), address, 1):
        pwndbg.disasm.arch.DisassemblyAssistant.enhance(ins)
        return ins

def one(address=None):
    if address is None:
        address = pwndbg.regs.pc
    if not pwndbg.memory.peek(address):
        return None
    for insn in get(address, 1):
        backward_cache[insn.next] = insn.address
        return insn

def fix(i):
    for op in i.operands:
        if op.type == CS_OP_IMM and op.va:
            i.op_str = i.op_str.replace()

    return i

def get(address, instructions=1):
    address = int(address)

    # Dont disassemble if there's no memory
    if not pwndbg.memory.peek(address):
        return []

    retval = []
    for _ in range(instructions):
        i = get_one_instruction(address)
        if i is None:
            break
        address = i.next
        retval.append(i)

    return retval


# These instruction types should not be emulated through, either
# because they cannot be emulated without interfering (syscall, etc.)
# or because they may take a long time (call, etc.), or because they
# change privilege levels.
DO_NOT_EMULATE = {
    capstone.CS_GRP_CALL,
    capstone.CS_GRP_INT,
    capstone.CS_GRP_INVALID,
    capstone.CS_GRP_IRET,

# Note that we explicitly do not include the PRIVILEGE category, since
# we may be in kernel code, and privileged instructions are just fine
# in that case.
#    capstone.CS_GRP_PRIVILEGE,
}

def near(address, instructions=1, emulate=False):

    current = one(address)

    pc = pwndbg.regs.pc

    if current is None or not pwndbg.memory.peek(address):
        return []

    # Try to go backward by seeing which instructions we've returned
    # before, which were followed by this one.
    needle = address
    insns  = []
    cached = backward_cache[current.address]
    insn   = one(cached) if cached else None
    while insn is not None and len(insns) < instructions:
        insns.append(insn)
        cached = backward_cache[insn.address]
        insn = one(cached) if cached else None
    insns.reverse()

    insns.append(current)

    # Some architecture aren't emulated yet
    if not pwndbg.emu or pwndbg.arch.current not in pwndbg.emu.emulator.arch_to_UC:
        emulate = False

    # Emulate forward if we are at the current instruction.
    emu = None

    # If we hit the current instruction, we can do emulation going forward from there.
    if address == pc and emulate:
        emu = pwndbg.emu.emulator.Emulator()

        # For whatever reason, the first instruction is emulated twice.
        # Skip the first one here.
        emu.single_step()

    # Now find all of the instructions moving forward.
    #
    # At this point, we've already added everything *BEFORE* the requested address,
    # and the instruction at 'address'.
    insn  = current
    total_instructions = 1+(2*instructions)

    while insn and len(insns) < total_instructions:
        target = insn.target

        # Disable emulation if necessary
        if emulate and set(insn.groups) & DO_NOT_EMULATE:
            emulate = False
            emu     = None

        # Continue disassembling after a RET or JUMP, but don't follow through CALL.
        if capstone.CS_GRP_CALL in insn.groups:
            target = insn.next

        # If we initialized the emulator and emulation is still enabled, we can use it
        # to figure out the next instruction.
        elif emu:
            target_candidate, size_candidate = emu.single_step()

            if None not in (target_candidate, size_candidate):
                target = target_candidate
                size   = size_candidate

        # Continue disassembling at the *next* instruction unless we have emulated
        # the path of execution.
        elif target != pc:
            target = insn.next


        insn = one(target)
        if insn:
            insns.append(insn)

    return insns
