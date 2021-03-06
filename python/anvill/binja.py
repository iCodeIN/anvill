# Copyright (c) 2020 Trail of Bits, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from typing import Union, List, Tuple, Optional, Set

import binaryninja as bn
from binaryninja import MediumLevelILInstruction as mlinst
from binaryninja import LowLevelILInstruction as llinst
from .arch import *
from .exc import *
from .function import *
from .loc import *
from .os import *
from .type import *
from .program import *


def is_valid_addr(bv, addr):
    return bv.get_segment_at(addr) is not None


def is_constant(bv, insn):
    return insn.operation in (
        bn.LowLevelILOperation.LLIL_CONST,
        bn.LowLevelILOperation.LLIL_CONST_PTR,
    )


def is_constant_pointer(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_CONST_PTR


def is_function_call(bv, insn):
    return insn.operation in (
        bn.LowLevelILOperation.LLIL_CALL,
        bn.LowLevelILOperation.LLIL_TAILCALL,
        bn.LowLevelILOperation.LLIL_CALL_STACK_ADJUST,
    )


def is_tailcall(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_TAILCALL


def is_return(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_RET


def is_jump(bv, insn):
    return insn.operation in (
        bn.LowLevelILOperation.LLIL_JUMP,
        bn.LowLevelILOperation.LLIL_JUMP_TO,
    )


def is_branch(bv, insn):
    return insn.operation in (
        bn.LowLevelILOperation.LLIL_JUMP,
        bn.LowLevelILOperation.LLIL_JUMP_TO,
        bn.LowLevelILOperation.LLIL_GOTO,
    )


def is_load_insn(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_LOAD


def is_store_insn(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_STORE


def is_memory_insn(bv, insn):
    return is_load_insn(bv, insn) or is_store_insn(bv, insn)


def is_unimplemented(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_UNIMPL


def is_unimplemented_mem(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_UNIMPL_MEM


def is_undef(bv, insn):
    return insn.operation == bn.LowLevelILOperation.LLIL_UNDEF


def is_code(bv, addr):
    sec_list = bv.get_sections_at(addr)
    for sec in sec_list:
        if sec.start <= addr < sec.end:
            return sec.semantics == bn.SectionSemantics.ReadOnlyCodeSectionSemantics
    return False


class XrefType:
    XREF_NONE = 0
    XREF_IMMEDIATE = 1
    XREF_DISPLACEMENT = 2
    XREF_MEMORY = 3
    XREF_CONTROL_FLOW = 4

    @staticmethod
    def is_memory(bv, reftype):
        return reftype in (XrefType.XREF_DISPLACEMENT, XrefType.XREF_MEMORY)


def _collect_code_xrefs_from_insn(bv, insn, ref_eas, reftype=XrefType.XREF_NONE):
    """Recursively collect xrefs in a IL instructions"""
    if not isinstance(insn, bn.LowLevelILInstruction):
        return

    if is_unimplemented(bv, insn) or is_undef(bv, insn):
        return

    if is_function_call(bv, insn) or is_jump(bv, insn):
        reftype = XrefType.XREF_CONTROL_FLOW

    elif is_memory_insn(bv, insn) or is_unimplemented_mem(bv, insn):
        mem_il = insn.dest if is_store_insn(bv, insn) else insn.src
        if is_constant(bv, mem_il):
            reftype = XrefType.XREF_MEMORY
        else:
            reftype = XrefType.XREF_DISPLACEMENT
        _collect_code_xrefs_from_insn(bv, mem_il, ref_eas, reftype)

        for opnd in insn.operands:
            _collect_code_xrefs_from_insn(bv, opnd, ref_eas)

    elif is_constant_pointer(bv, insn):
        const_ea = insn.constant
        if is_code(bv, const_ea) and not XrefType.is_memory(bv, reftype):
            ref_eas.add(const_ea)

    # Recursively look for the xrefs in operands
    for opnd in insn.operands:
        _collect_code_xrefs_from_insn(bv, opnd, ref_eas, reftype)


def _convert_bn_llil_type(constant_val: bn.function.RegisterValue, reg_size_bytes: int) \
        -> Type:
    """Convert LLIL register type to Anvill type
    """
    if constant_val.type == bn.function.RegisterValueType.ConstantPointerValue:
        ret = PointerType()
        return ret
    elif constant_val.type == bn.function.RegisterValueType.ConstantValue:
        ret = IntegerType(reg_size_bytes, True)
        return ret


def _convert_bn_type(tinfo: bn.types.Type, cache):
    """Convert an bn `Type` instance into a `Type` instance."""
    if str(tinfo) in cache:
        return cache[str(tinfo)]

    # Void type.
    if tinfo.type_class == bn.TypeClass.VoidTypeClass:
        return VoidType()

    # Pointer, array, or function.
    elif tinfo.type_class == bn.TypeClass.PointerTypeClass:
        ret = PointerType()
        cache[str(tinfo)] = ret
        ret.set_element_type(_convert_bn_type(tinfo.element_type, cache))
        return ret

    elif tinfo.type_class == bn.TypeClass.FunctionTypeClass:
        ret = FunctionType()
        cache[str(tinfo)] = ret
        ret.set_return_type(_convert_bn_type(tinfo.return_value, cache))

        for var in tinfo.parameters:
            ret.add_parameter_type(_convert_bn_type(var.type, cache))

        if tinfo.has_variable_arguments:
            ret.set_is_variadic()

        return ret

    elif tinfo.type_class == bn.TypeClass.ArrayTypeClass:
        ret = ArrayType()
        cache[str(tinfo)] = ret
        ret.set_element_type(_convert_bn_type(tinfo.element_type, cache))
        ret.set_num_elements(tinfo.count)
        return ret

    elif tinfo.type_class == bn.TypeClass.StructureTypeClass:
        ret = StructureType()
        cache[str(tinfo)] = ret
        return ret

    elif tinfo.type_class == bn.TypeClass.EnumerationTypeClass:
        ret = EnumType()
        cache[str(tinfo)] = ret
        return ret

    elif tinfo.type_class == bn.TypeClass.BoolTypeClass:
        return BoolType()

    # long double ty may get represented as int80_t. If the size
    # of the IntegerTypeClass is [10, 12], create a float type
    # int32_t (int32_t arg1, int80_t arg2 @ st0)
    elif tinfo.type_class == bn.TypeClass.IntegerTypeClass:
        if tinfo.width in [1, 2, 4, 8, 16]:
            ret = IntegerType(tinfo.width, True)
            return ret
        elif tinfo.width in [10, 12]:
            width = tinfo.width
            return FloatingPointType(width)

    elif tinfo.type_class == bn.TypeClass.FloatTypeClass:
        width = tinfo.width
        return FloatingPointType(width)

    elif tinfo.type_class in [
        bn.TypeClass.VarArgsTypeClass,
        bn.TypeClass.ValueTypeClass,
        bn.TypeClass.NamedTypeReferenceClass,
        bn.TypeClass.WideCharTypeClass,
    ]:
        raise UnhandledTypeException(
            "Unhandled VarArgs, Value, or WideChar type: {}".format(str(tinfo)), tinfo
        )

    else:
        raise UnhandledTypeException("Unhandled type: {}".format(str(tinfo)), tinfo)


def get_type(ty):
    """Type class that gives access to type sizes, printings, etc."""

    if isinstance(ty, Type):
        return ty

    elif isinstance(ty, Function):
        return ty.type()

    elif isinstance(ty, bn.Type):
        return _convert_bn_type(ty, {})

    if not ty:
        return VoidType()

    raise UnhandledTypeException("Unrecognized type passed to `Type`.", ty)


def get_arch(bv):
    """Arch class that gives access to architecture-specific functionality."""
    name = bv.arch.name
    if name == "x86_64":
        return AMD64Arch()
    elif name == "x86":
        return X86Arch()
    elif name == "aarch64":
        return AArch64Arch()
    else:
        raise UnhandledArchitectureType(
            "Missing architecture object type for architecture '{}'".format(name)
        )


def get_os(bv):
    """OS class that gives access to OS-specific functionality."""
    platform = str(bv.platform)
    if "linux" in platform:
        return LinuxOS()
    elif "mac" in platform:
        return MacOS()
    elif "windows" in platform:
        return WindowsOS()
    else:
        raise UnhandledOSException(
            "Missing operating system object type for OS '{}'".format(platform)
        )


class CallingConvention(object):
    def __init__(self, arch, bn_func):
        self._cc = bn_func.calling_convention
        self._arch = arch
        self._bn_func = bn_func
        self._int_arg_regs = self._cc.int_arg_regs
        self._float_arg_regs = self._cc.float_arg_regs
        if self._cc.name == "cdecl":
            self._float_arg_regs = ["st0", "st1", "st2", "st3", "st4", "st5"]

    def is_sysv(self):
        return self._cc.name == "sysv"

    def is_cdecl(self):
        return self._cc.name == "cdecl"

    @property
    def next_int_arg_reg(self):
        try:
            reg_name = self._int_arg_regs[0]
            del self._int_arg_regs[0]
            return reg_name
        except:
            return "invalid int register"

    @property
    def next_float_arg_reg(self):
        reg_name = self._float_arg_regs[0]
        del self._float_arg_regs[0]
        return reg_name

    @property
    def return_regs(self):
        for reg in self._bn_func.return_regs:
            yield reg


class BNFunction(Function):
    def __init__(self, bn_func, arch, address, param_list, ret_list, func_type):
        super(BNFunction, self).__init__(arch, address, param_list, ret_list, func_type)
        self._bn_func = bn_func

    def name(self):
        return self._bn_func.name

    def visit(self, program, is_definition, add_refs_as_defs):
        if not is_definition:
            return

        mem = program.memory()

        ref_eas: Set[int] = set()
        ea = self._bn_func.start
        max_ea = self._bn_func.highest_address
        self._fill_bytes(program._bv, mem, ea, max_ea, ref_eas)

        # Collect typed register info for this function
        for block in self._bn_func.llil:
            for inst in block:
                register_information = self._extract_types(program._bv, inst.operands, inst)
                for reg_info in register_information:
                    loc = Location()
                    loc.set_register(reg_info[0].upper())
                    loc.set_type(reg_info[1])
                    if reg_info[2] is not None:
                        # fill_bytes misses some references, catch what we can
                        ref_eas.add(reg_info[2])
                        # print(f"inst_addr: {hex(inst.address)}, reg_info[2]: {reg_info[2]}, ref_eas: {ref_eas}")
                        # assert reg_info[2] in ref_eas
                        # assert reg_info 1 is a pointer, then reg2 should be in ea
                        loc.set_value(reg_info[2])
                    loc.set_address(inst.address)
                    self._register_info.append(loc)
        # ea = effective_address
        # there is a distinction between declaration and definition
        # if the function is a declaration, then Anvill only needs to know its symbols and prototypes
        # if its a definition, then Anvill will perform analysis of the function and produce information for the func
        for ref_ea in ref_eas:
            program.try_add_referenced_entity(ref_ea, add_refs_as_defs)

    def _extract_types_mlil(self, bv, item_or_list, initial_inst: mlinst) -> List[Tuple[str, Type, Optional[int]]]:
        """
        This function decomposes a list of MLIL instructions and variables into a list of tuples
        that associate registers with pointer information if it exists.
        """
        results = []
        if isinstance(item_or_list, list):
            for item in item_or_list:
                results.extend(self._extract_types_mlil(bv, item, initial_inst))
        elif isinstance(item_or_list, mlinst):
            results.extend(self._extract_types_mlil(bv, item_or_list.operands, initial_inst))
        elif isinstance(item_or_list, bn.Variable):
            # We only care about registers that represent pointers.
            if item_or_list.type.type_class == bn.TypeClass.PointerTypeClass:
                if item_or_list.source_type == bn.VariableSourceType.RegisterVariableSourceType:
                    reg_name = bv.arch.get_reg_name(item_or_list.storage)
                    results.append((reg_name, _convert_bn_type(item_or_list.type, {}), None))
        return results

    def _extract_types(self, bv, item_or_list, initial_inst: llinst) -> List[Tuple[str, Type, Optional[int]]]:
        """
        This function decomposes a list of LLIL instructions and associates registers with pointer values
        if they exist. If an MLIL instruction exists for the current instruction, it uses the MLIL to get more
        information about otherwise implicit operands and their types if available. (ex, a call instruction has
        rdi, rsi as operands in the MLIL, we should check if they have pointer information)
        """
        results = []
        if isinstance(item_or_list, list):
            for item in item_or_list:
                results.extend(self._extract_types(bv, item, initial_inst))
        elif isinstance(item_or_list, llinst):
            results.extend(self._extract_types(bv, item_or_list.operands, initial_inst))
        elif isinstance(item_or_list, bn.lowlevelil.ILRegister):
            # For every register, is it a pointer?
            possible_pointer: bn.function.RegisterValue = initial_inst.get_reg_value(item_or_list.name)
            if possible_pointer.type == bn.function.RegisterValueType.ConstantPointerValue or \
                    possible_pointer.type == bn.function.RegisterValueType.ExternalPointerValue:  # or
                # possible_pointer.type == bn.function.RegisterValueType.ConstantValue:
                # Is there a scenario where a register has a ConstantValue type thats used as a pointer?
                val_type = _convert_bn_llil_type(possible_pointer, item_or_list.info.size)
                results.append((item_or_list.name, val_type, possible_pointer.value))

            if initial_inst.mlil is not None:
                mlil_results = self._extract_types_mlil(bv, initial_inst.mlil, initial_inst.mlil)
                results.extend(mlil_results)
        return results

    def _fill_bytes(self, bv, memory, start, end, ref_eas):
        br = bn.BinaryReader(bv)
        for bb in self._bn_func.basic_blocks:
            for ea in range(bb.start, bb.end):
                seg = bv.get_segment_at(ea)
                br.seek(ea)
                memory.map_byte(ea, br.read8(), seg.writable, seg.executable)
                insn = self._bn_func.get_lifted_il_at(ea)
                if insn:
                    _collect_code_xrefs_from_insn(bv, insn, ref_eas)


class BNVariable(Variable):
    def __init__(self, bn_var, arch, address, type_):
        super(BNVariable, self).__init__(arch, address, type_)
        self._bn_var = bn_var

    def visit(self, program, is_definition, add_refs_as_defs):
        if not is_definition:
            return

        if isinstance(self._type, VoidType):
            return

        bv = program._bv
        br = bn.BinaryReader(bv)
        mem = program.memory()
        begin = self._address
        end = begin + self._type.size(self._arch)

        for ea in range(begin, end):
            br.seek(ea)
            seg = bv.get_segment_at(ea)
            mem.map_byte(ea, br.read8(), seg.writable, seg.executable)


class BNProgram(Program):
    def __init__(self, path):
        self._path = path
        self._bv = bn.BinaryViewType.get_view_of_file(self._path)
        super(BNProgram, self).__init__(get_arch(self._bv), get_os(self._bv))

    def get_variable_impl(self, address):
        """Given an address, return a `Variable` instance, or
        raise an `InvalidVariableException` exception."""
        arch = self._arch
        bn_var = self._bv.get_data_var_at(address)
        var_type = get_type(bn_var.type)
        # fall back onto an array of bytes type for variables
        # of an unknown (void) type.
        if isinstance(var_type, VoidType):
            var_type = ArrayType()
            var_type.set_num_elements(1)

        return BNVariable(bn_var, arch, address, var_type)

    def get_function_impl(self, address):
        """Given an architecture and an address, return a `Function` instance or
        raise an `InvalidFunctionException` exception."""
        arch = self._arch

        bn_func = self._bv.get_function_at(address)
        if not bn_func:
            func_contains = self._bv.get_functions_containing(address)
            if func_contains and len(func_contains):
                bn_func = func_contains[0]

        if not bn_func:
            raise InvalidFunctionException(
                "No function defined at or containing address {:x}".format(address)
            )

        func_type = get_type(bn_func.function_type)
        calling_conv = CallingConvention(arch, bn_func)

        index = 0
        param_list = []
        for var in bn_func.parameter_vars:
            source_type = var.source_type
            var_type = var.type
            arg_type = get_type(var_type)

            if source_type == bn.VariableSourceType.RegisterVariableSourceType:
                if (
                        bn.TypeClass.IntegerTypeClass == var_type.type_class
                        or bn.TypeClass.PointerTypeClass == var_type.type_class
                ):
                    reg_name = calling_conv.next_int_arg_reg
                elif bn.TypeClass.FloatTypeClass == var_type.type_class:
                    reg_name = calling_conv.next_float_arg_reg
                elif bn.TypeClass.VoidTypeClass == var_type.type_class:
                    reg_name = "invalid void"
                else:
                    reg_name = None
                    raise AnvillException(
                        "No variable type defined for function parameters"
                    )

                loc = Location()
                loc.set_register(reg_name.upper())
                loc.set_type(arg_type)
                param_list.append(loc)

            elif source_type == bn.VariableSourceType.StackVariableSourceType:
                loc = Location()
                loc.set_memory(self._bv.arch.stack_pointer.upper(), var.storage)
                loc.set_type(arg_type)
                param_list.append(loc)

            index += 1

        ret_list = []
        retTy = get_type(bn_func.return_type)
        if not isinstance(retTy, VoidType):
            for reg in calling_conv.return_regs:
                loc = Location()
                loc.set_register(reg.upper())
                loc.set_type(retTy)
                ret_list.append(loc)

        func = BNFunction(bn_func, arch, address, param_list, ret_list, func_type)
        return func

    @property
    def functions(self):
        for func in self._bv.functions:
            yield (func.start, func.name)

    @property
    def variables(self):
        for addr, var in self._bv.data_vars.items():
            # filter out functions
            if len(self._bv.get_functions_at(addr)) == 0:
                yield (addr, var)


_PROGRAM = None


def get_program(*args, **kargs):
    global _PROGRAM
    if _PROGRAM:
        return _PROGRAM
    assert len(args) == 1

    prog = BNProgram(args[0])
    if "cache" not in kargs or kargs["cache"]:
        _PROGRAM = prog
    return prog
