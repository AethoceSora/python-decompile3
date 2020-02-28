#  Copyright (c) 2020 Rocky Bernstein
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

def iflaststmt(
    self, lhs: str, n: int, rule, ast, tokens: list, first: int, last: int
) -> bool:
    testexpr = ast[0]

    # FIXME: should this be done in the caller?
    if tokens[last] == "RETURN_LAST":
        last -= 1

    if rule == ("iflaststmt", ("testexpr", "stmts")):
        # If there is a trailing if-jump (forward) at the end of "testexp", it should
        # to the end of "stmts".

        # If there was backward jump, the LHS would be "iflaststmtc".
        # Note that there might not be a COME_FROM before "stmts" because there can be a fall
        # through to it.
        stmt_offset = ast[1].first_child().off2int(prefer_last=False)
        inst_offset = self.offset2inst_index[stmt_offset]
        testexpr_last_inst = self.insts[inst_offset-1]
        if testexpr_last_inst.optype == "jabs":
            target_offset = testexpr_last_inst.argval
            if  target_offset != tokens[last].off2int(prefer_last=False):
                # There is still this weird case:
                # if a:
                #   if b:
                #     x += 3
                #     # jumps to same place as "if a then.." end jump.
                # else:
                #    ...
                # we are going to hack this my looking for another jump to the same target. Sigh.
                i = inst_offset
                inst = self.insts[i]
                while inst.offset < target_offset:
                    if inst.optype in ("jabs", "jrel") and inst.argval == target_offset:
                        return False
                    i += 1
                    inst = self.insts[i]
                return True
            pass
        pass

    if testexpr[0] in ("testtrue", "testtruec", "testfalse", "testfalsec"):

        test = testexpr[0]
        test_len = len(test)
        if test_len == 1 and test[0] in ("nand", "and") and rule[1] == ('testexpr', 'stmts'):
            # (n)and rules have precedence
            return True

        if test_len > 1 and test[1].kind.startswith("POP_JUMP_IF_"):
            if last == n:
                last -= 1
            jump_target = test[1].attr
            first_offset = tokens[first].off2int()
            if  first_offset <= jump_target < tokens[last].off2int():
                return True
            # jump_target less than tokens[first] is okay - is to a loop
            # jump_target equal tokens[last] is also okay: normal non-optimized non-loop jump

            if (last + 1) < n:
                if tokens[last - 1] == "JUMP_BACK":
                    if jump_target > first_offset:
                        # The end of the iflaststmt if test jumps backward to a loop
                        # but the false branch of the "if" doesn't also jump back.
                        # No good. This is probably an if/else instead.
                        return True
                    pass
                elif tokens[last + 1] == "COME_FROM_LOOP" and tokens[last] != "BREAK_LOOP":
                    # iflastsmtc is not at the end of a loop, but jumped outside of loop. No good.
                    # FIXME: check that tokens[last] == "POP_BLOCK"? Or allow for it not to appear?
                    return True

            # If the instruction before "first" is a "POP_JUMP_IF_FALSE" which goes
            # to the same target as jump_target, then this not nested "if .. if .."
            # but rather "if ... and ..."
            if first > 0 and tokens[first - 1] == "POP_JUMP_IF_FALSE":
                return tokens[first - 1].attr == jump_target

            if jump_target > tokens[last].off2int():
                if jump_target == tokens[last - 1].attr:
                    # if c1 [jump] jumps exactly the end of the iflaststmt...
                    return False
                pass
            pass
        pass
    return False
