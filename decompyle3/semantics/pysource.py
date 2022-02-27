#  Copyright (c) 2015-2022 by Rocky Bernstein
#  Copyright (c) 2005 by Dan Pascu <dan@windowmaker.org>
#  Copyright (c) 2000-2002 by hartmut Goebel <h.goebel@crazy-compilers.com>
#  Copyright (c) 1999 John Aycock
#
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

"""Creates Python source code from an decompyle3 parse tree.

The terminal symbols are CPython bytecode instructions. (See the
python documentation under module "dis" for a list of instructions
and what they mean).

Upper levels of the grammar is a more-or-less conventional grammar for
Python.
"""

# The below is a bit long, but still it is somewhat abbreviated.
# See https://github.com/rocky/python-uncompyle6/wiki/Table-driven-semantic-actions.
# for a more complete explanation, nicely marked up and with examples.
#
#
# Semantic action rules for nonterminal symbols can be specified here by
# creating a method prefaced with "n_" for that nonterminal. For
# example, "n_exec_stmt" handles the semantic actions for the
# "exec_stmt" nonterminal symbol. Similarly if a method with the name
# of the nonterminal is suffixed with "_exit" it will be called after
# all of its children are called.
#
# After a while writing methods this way, you'll find many routines which do similar
# sorts of things, and soon you'll find you want a short notation to
# describe rules and not have to create methods at all.
#
# So another other way to specify a semantic rule for a nonterminal is via
# either tables MAP_R, or MAP_DIRECT where the key is the
# nonterminal name.
#
# These dictionaries use a printf-like syntax to direct substitution
# from attributes of the nonterminal and its children..
#
# The rest of the below describes how table-driven semantic actions work
# and gives a list of the format specifiers. The default() and
# template_engine() methods implement most of the below.
#
# We allow for a couple of ways to interact with a node in a tree.  So
# step 1 after not seeing a custom method for a nonterminal is to
# determine from what point of view tree-wise the rule is applied.

# In the diagram below, N is a nonterminal name, and K also a nonterminal
# name but the one used as a key in the table.
# we show where those are with respect to each other in the
# parse tree for N.
#
#
#          N&K               N
#         / | ... \        / | ... \
#        O  O      O      O  O      K
#
#
#      TABLE_DIRECT      TABLE_R
#
#   The default table is TABLE_DIRECT mapping By far, most rules used work this way.
#
#   The key K is then extracted from the subtree and used to find one
#   of the tables, T listed above.  The result after applying T[K] is
#   a format string and arguments (a la printf()) for the formatting
#   engine.
#
#   Escapes in the format string are:
#
#     %c  evaluate/traverse the node recursively. Its argument is a single
#         integer or tuple representing a node index.
#         If a tuple is given, the first item is the node index while
#         the second item is a string giving the node/noterminal name.
#         This name will be checked at runtime against the node type.
#
#     %p  like %c but sets the operator precedence.
#         Its argument then is a tuple indicating the node
#         index and the precedence value, an integer. If 3 items are given,
#         the second item is the nonterminal name and the precedence is given last.
#
#     %C  evaluate/travers children recursively, with sibling children separated by the
#         given string.  It needs a 3-tuple: a starting node, the maximimum
#         value of an end node, and a string to be inserted between sibling children
#
#     %,  Append ',' if last %C only printed one item. This is mostly for tuples
#         on the LHS of an assignment statement since BUILD_TUPLE_n pretty-prints
#         other tuples. The specifier takes no arguments
#
#     %P  same as %C but sets operator precedence.  Its argument is a 4-tuple:
#         the node low and high indices, the separator, a string the precidence
#         value, an integer.
#
#     %D Same as `%C` this is for left-recursive lists like kwargs where goes
#         to epsilon at the beginning. It needs a 3-tuple: a starting node, the
#         maximimum value of an end node, and a string to be inserted between
#         sibling children. If we were to use `%C` an extra separator with an
#         epsilon would appear at the beginning.
#
#     %|  Insert spaces to the current indentation level. Takes no arguments.
#
#     %+ increase current indentation level. Takes no arguments.
#
#     %- decrease current indentation level. Takes no arguments.
#
#     %{EXPR} Python eval(EXPR) in context of node. Takes no arguments
#
#     %[N]{EXPR} Python eval(EXPR) in context of node[N]. Takes no arguments
#
#     %[N]{%X} evaluate/recurse on child node[N], using specifier %X.
#     %X can be one of the above, e.g. %c, %p, etc. Takes the arguemnts
#     that the specifier uses.
#
#     %% literal '%'. Takes no arguments.
#
#
#   The '%' may optionally be followed by a number (C) in square
#   brackets, which makes the template_engine walk down to N[C] before
#   evaluating the escape code.

import sys

IS_PYPY = "__pypy__" in sys.builtin_module_names

from xdis import COMPILER_FLAG_BIT, iscode
from xdis.version_info import PYTHON_VERSION_TRIPLE

import decompyle3.parsers.parse_heads as heads
import decompyle3.parsers.main as python_parser
from decompyle3.parsers.main import get_python_parser
from decompyle3.parsers.treenode import SyntaxTree
from spark_parser import GenericASTTraversal
from decompyle3.scanner import Code, get_scanner
from decompyle3.semantics.make_function36 import make_function36
from decompyle3.semantics.parser_error import ParserError
from decompyle3.semantics.check_ast import checker
from decompyle3.semantics.customize import customize_for_version
from decompyle3.semantics.helper import (
    find_globals_and_nonlocals,
    flatten_list,
    is_lambda_mode,
)
from decompyle3.semantics.transform import TreeTransform

from decompyle3.scanners.tok import Token

from decompyle3.semantics.consts import (
    LINE_LENGTH,
    NONE,
    PASS,
    NAME_MODULE,
    TAB,
    INDENT_PER_LEVEL,
    TABLE_R,
    MAP_DIRECT,
    MAP,
    PRECEDENCE,
    escape,
    minint,
)


from decompyle3.show import maybe_show_tree
from decompyle3.util import better_repr


from io import StringIO

PARSER_DEFAULT_DEBUG = {
    "rules": False,
    "transition": False,
    "reduce": False,
    "errorstack": "full",
    "context": True,
    "dups": False,
}

TREE_DEFAULT_DEBUG = {"before": False, "after": False}

DEFAULT_DEBUG_OPTS = {
    "asm": False,
    "tree": TREE_DEFAULT_DEBUG,
    "grammar": dict(PARSER_DEFAULT_DEBUG),
}


class SourceWalkerError(Exception):
    def __init__(self, errmsg):
        self.errmsg = errmsg

    def __str__(self):
        return self.errmsg


class SourceWalker(GenericASTTraversal, object):
    stacked_params = ("f", "indent", "is_lambda", "_globals")

    def __init__(
        self,
        version,
        out,
        scanner,
        showast=TREE_DEFAULT_DEBUG,
        debug_parser=PARSER_DEFAULT_DEBUG,
        compile_mode="exec",
        is_pypy=IS_PYPY,
        linestarts={},
        tolerate_errors=False,
    ):
        """`version' is the Python version (a float) of the Python dialect
        of both the syntax tree and language we should produce.

        `out' is IO-like file pointer to where the output should go. It
        whould have a getvalue() method.

        `scanner' is a method to call when we need to scan tokens. Sometimes
        in producing output we will run across further tokens that need
        to be scaned.

        If `showast' is True, we print the syntax tree.

        `compile_mode` is is either `exec`, `single` or `lambda`.

        For `lambda`, the grammar that can be used in lambda
        expressions is used.  Otherwise, it is the compile mode that
        was used to create the Syntax Tree and specifies a gramar
        variant within a Python version to use.

        `is_pypy` should be True if the Syntax Tree was generated for PyPy.

        `linestarts` is a dictionary of line number to bytecode offset. This
        can sometimes assist in determinte which kind of source-code construct
        to use when there is ambiguity.

        """
        GenericASTTraversal.__init__(self, ast=None)

        self.scanner = scanner
        params = {"f": out, "indent": ""}
        self.version = version
        self.p = get_python_parser(
            version,
            debug_parser=dict(debug_parser),
            compile_mode=compile_mode,
            is_pypy=is_pypy,
        )

        # Initialize p_lambda on demand
        self.p_lambda = None

        self.treeTransform = TreeTransform(version=self.version, show_ast=showast)
        self.debug_parser = dict(debug_parser)
        self.showast = showast
        self.params = params
        self.param_stack = []
        self.ERROR = None
        self.prec = 100
        self.return_none = False
        self.mod_globs = set()
        self.currentclass = None
        self.classes = []
        self.pending_newlines = 0
        self.linestarts = linestarts
        self.line_number = 1
        self.ast_errors = []
        # FIXME: have p.insts update in a better way
        # modularity is broken here
        self.p.insts = scanner.insts
        self.offset2inst_index = scanner.offset2inst_index

        # This is in Python 2.6 on. It changes the way
        # strings get interpreted. See n_LOAD_CONST
        self.FUTURE_UNICODE_LITERALS = False

        # Sometimes we may want to continue decompiling when there are errors
        # and sometimes not
        self.tolerate_errors = tolerate_errors

        # If we are in a 3.6+ format string, we may need an
        # extra level of parens when seeing a lambda. We also use
        # this to understand whether or not to add the "f" prefix.
        # When not "None" it is a string of the last nonterminal
        # that started the format string
        self.in_format_string = None

        # hide_internal suppresses displaying the additional instructions that sometimes
        # exist in code but but were not written in the source code.
        # An example is:
        # __module__ = __name__
        self.hide_internal = True
        self.compile_mode = compile_mode
        self.name = None
        self.version = version
        self.is_pypy = is_pypy
        customize_for_version(self, is_pypy, version)
        return

    def maybe_show_tree(self, tree, phase):
        if self.showast.get("before", False):
            self.println(
                """
---- end before transform
"""
            )
        if self.showast.get("after", False):
            self.println(
                """
---- begin after transform
"""
                + " "
            )
        if self.showast.get(phase, False):
            maybe_show_tree(self, tree)

    def str_with_template(self, tree) -> str:
        stream = sys.stdout
        stream.write(self.str_with_template1(tree, "", None))
        stream.write("\n")

    def str_with_template1(self, tree, indent, sibNum=None) -> str:
        rv = str(tree.kind)

        if sibNum is not None:
            rv = "%2d. %s" % (sibNum, rv)
        enumerate_children = False
        if len(tree) > 1:
            rv += f" ({len(tree)})"
            enumerate_children = True

        if tree in PRECEDENCE:
            rv += f", precedence {PRECEDENCE[tree]}"

        mapping = self._get_mapping(tree)
        table = mapping[0]
        key = tree
        for i in mapping[1:]:
            key = key[i]
            pass

        if tree.transformed_by is not None:
            if tree.transformed_by is True:
                rv += " transformed"
            else:
                rv += " transformed by %s" % tree.transformed_by
                pass
            pass
        if key.kind in table:
            rv += ": %s" % str(table[key.kind])

        rv = indent + rv
        indent += "    "
        i = 0
        for node in tree:

            if hasattr(node, "__repr1__"):
                if enumerate_children:
                    child = self.str_with_template1(node, indent, i)
                else:
                    child = self.str_with_template1(node, indent, None)
            else:
                inst = node.format(line_prefix="L.")
                if inst.startswith("\n"):
                    # Nuke leading \n
                    inst = inst[1:]
                if enumerate_children:
                    child = indent + "%2d. %s" % (i, inst)
                else:
                    child = indent + inst
                pass
            rv += "\n" + child
            i += 1
        return rv

    def indent_if_source_nl(self, line_number, indent):
        if line_number != self.line_number:
            self.write("\n" + self.indent + INDENT_PER_LEVEL[:-1])
        return self.line_number

    f = property(
        lambda s: s.params["f"],
        lambda s, x: s.params.__setitem__("f", x),
        lambda s: s.params.__delitem__("f"),
        None,
    )

    indent = property(
        lambda s: s.params["indent"],
        lambda s, x: s.params.__setitem__("indent", x),
        lambda s: s.params.__delitem__("indent"),
        None,
    )

    is_lambda = property(
        lambda s: s.params["is_lambda"],
        lambda s, x: s.params.__setitem__("is_lambda", x),
        lambda s: s.params.__delitem__("is_lambda"),
        None,
    )

    _globals = property(
        lambda s: s.params["_globals"],
        lambda s, x: s.params.__setitem__("_globals", x),
        lambda s: s.params.__delitem__("_globals"),
        None,
    )

    def set_pos_info(self, node):
        if hasattr(node, "linestart") and node.linestart:
            self.line_number = node.linestart

    def preorder(self, node=None):
        super(SourceWalker, self).preorder(node)
        self.set_pos_info(node)

    def indent_more(self, indent=TAB):
        self.indent += indent

    def indent_less(self, indent=TAB):
        self.indent = self.indent[: -len(indent)]

    def traverse(self, node, indent=None, is_lambda=False):
        self.param_stack.append(self.params)
        if indent is None:
            indent = self.indent
        p = self.pending_newlines
        self.pending_newlines = 0
        self.params = {
            "_globals": {},
            "_nonlocals": {},  # Python 3 has nonlocal
            "f": StringIO(),
            "indent": indent,
            "is_lambda": is_lambda,
        }
        self.preorder(node)
        self.f.write("\n" * self.pending_newlines)
        result = self.f.getvalue()
        self.params = self.param_stack.pop()
        self.pending_newlines = p
        return result

    def write(self, *data):
        if (len(data) == 0) or (len(data) == 1 and data[0] == ""):
            return
        out = "".join((str(j) for j in data))
        n = 0
        for i in out:
            if i == "\n":
                n += 1
                if n == len(out):
                    self.pending_newlines = max(self.pending_newlines, n)
                    return
            elif n:
                self.pending_newlines = max(self.pending_newlines, n)
                out = out[n:]
                break
            else:
                break

        if self.pending_newlines > 0:
            self.f.write("\n" * self.pending_newlines)
            self.pending_newlines = 0

        for i in out[::-1]:
            if i == "\n":
                self.pending_newlines += 1
            else:
                break

        if self.pending_newlines:
            out = out[: -self.pending_newlines]
        self.f.write(out)

    def println(self, *data):
        if data and not (len(data) == 1 and data[0] == ""):
            self.write(*data)
        self.pending_newlines = max(self.pending_newlines, 1)

    def is_return_none(self, node):
        # Is there a better way?
        ret = (
            node[0] == "return_expr"
            and node[0][0] == "expr"
            and node[0][0][0] == "LOAD_CONST"
            and node[0][0][0].pattr is None
        )

        # FIXME: should the SyntaxTree expression be folded into
        # the global RETURN_NONE constant?
        return ret or node == SyntaxTree(
            "return", [SyntaxTree("return_expr", [NONE]), Token("RETURN_VALUE")]
        )

    def n_bin_op(self, node):
        """bin_op (formerly "binary_expr") is the Python AST BinOp"""
        self.preorder(node[0])
        self.write(" ")
        self.preorder(node[-1])
        self.write(" ")
        # Try to avoid a trailing parentheses by lowering the priority a little
        self.prec -= 1
        self.preorder(node[1])
        self.prec += 1
        self.prune()

    def n_delete_subscript(self, node):
        if node[-2][0] == "build_list" and node[-2][0][-1].kind.startswith(
            "BUILD_TUPLE"
        ):
            if node[-2][0][-1] != "BUILD_TUPLE_0":
                node[-2][0].kind = "build_tuple2"
        self.default(node)

    n_store_subscript = n_subscript = n_delete_subscript

    def n_expr(self, node):
        first_child = node[0]
        p = self.prec

        if first_child.kind.startswith("bin_op"):
            n = node[0][-1][0]
        else:
            n = node[0]

        # if (hasattr(n, 'linestart') and n.linestart and
        #     hasattr(self, 'current_line_number')):
        #     self.source_linemap[self.current_line_number] = n.linestart

        self.prec = PRECEDENCE.get(n.kind, -2)
        if n == "LOAD_CONST" and repr(n.pattr)[0] == "-":
            self.prec = 6

        # print("XXX", n.kind, p, "<", self.prec)
        # print(self.f.getvalue())

        if p < self.prec:
            # print(f"PREC {p}, {node[0].kind}")
            self.write("(")
            self.preorder(node[0])
            self.write(")")
        else:
            self.preorder(node[0])
        self.prec = p
        self.prune()

    def n_return_call_lambda(self, node):

        # Understand where the non-psuedo instructions lie.
        opt_start = 1 if node[0].kind in ("come_from_", "COME_FROM") else 0
        call_index = -3 if node[-1].kind == "COME_FROM" else -2

        call_fn = node[call_index]
        assert call_fn.kind.startswith("CALL_FUNCTION")
        # Just print the args
        self.template_engine(
            ("%P", (opt_start, call_fn.attr + opt_start, ", ", 100)), node
        )
        self.prune()

    # Python 3.x can have be dead code as a result of its optimization?
    # So we'll add a # at the end of the return lambda so the rest is ignored
    def n_return_expr_lambda(self, node):
        if 1 <= len(node) <= 2:
            self.preorder(node[0])
            self.prune()
        else:
            # We can't comment out like above because there may be a trailing ')'
            # that needs to be written
            assert len(node) == 3 and node[2] in (
                "RETURN_VALUE_LAMBDA",
                "LAMBDA_MARKER",
            )
            self.preorder(node[0])
            self.prune()

    def n_return(self, node):
        if self.params["is_lambda"] or node[0] in (
            "pop_return",
            "popb_return",
            "pop_ex_return",
        ):
            self.preorder(node[0])
            self.prune()
        else:
            self.write(self.indent, "return")
            # One reason we worry over whether we use "return None" or "return"
            # is that inside a generator, "return None" is illegal.
            # Thank you, Python!
            if self.return_none or not self.is_return_none(node):
                self.write(" ")
                self.preorder(node[0])
            self.println()
            self.prune()  # stop recursing

    def n_return_expr(self, node):
        if len(node) == 1 and node[0] == "expr":
            # If expr is yield we want parens.
            self.prec = PRECEDENCE["yield"] - 1
            self.n_expr(node[0])
        else:
            self.n_expr(node)

    n_return_expr_or_cond = n_expr

    def n_return_if_stmt(self, node):
        if self.params["is_lambda"]:
            self.write(" return ")
            self.preorder(node[0])
            self.prune()
        else:
            self.write(self.indent, "return")
            if self.return_none or not self.is_return_none(node):
                self.write(" ")
                self.preorder(node[0])
            self.println()
            self.prune()  # stop recursing

    # This could be a rule but we have handling to remove None
    # e.g. a[:5] rather than a[None:5]
    def n_slice2(self, node):
        p = self.prec
        self.prec = 100
        if not node[0].isNone():
            self.preorder(node[0])
        self.write(":")
        if not node[1].isNone():
            self.preorder(node[1])
        self.prec = p
        self.prune()  # stop recursing

    # This could be a rule but we have handling to remove None's
    # e.g. a[:] rather than a[None:None]
    def n_slice3(self, node):
        p = self.prec
        self.prec = 100
        if not node[0].isNone():
            self.preorder(node[0])
        self.write(":")
        if not node[1].isNone():
            self.preorder(node[1])
        self.write(":")
        if not node[2].isNone():
            self.preorder(node[2])
        self.prec = p
        self.prune()  # stop recursing

    def n_yield(self, node):
        if node != SyntaxTree("yield", [NONE, Token("YIELD_VALUE")]):
            self.template_engine(("yield %c", 0), node)
        elif self.version <= (2, 4):
            # Early versions of Python don't allow a plain "yield"
            self.write("yield None")
        else:
            self.write("yield")

        self.prune()  # stop recursing

    def n_str(self, node):
        self.write(node[0].pattr)
        self.prune()

    def pp_tuple(self, tup):
        """Pretty print a tuple"""
        last_line = self.f.getvalue().split("\n")[-1]
        l = len(last_line) + 1
        indent = " " * l
        self.write("(")
        sep = ""
        for item in tup:
            self.write(sep)
            l += len(sep)
            s = better_repr(item)
            l += len(s)
            self.write(s)
            sep = ","
            if l > LINE_LENGTH:
                l = 0
                sep += "\n" + indent
            else:
                sep += " "
                pass
            pass
        if len(tup) == 1:
            self.write(", ")
        self.write(")")

    def n_LOAD_CONST(self, node):
        attr = node.attr
        data = node.pattr
        datatype = type(data)
        if isinstance(data, float):
            self.write(better_repr(data))
        elif isinstance(data, complex):
            self.write(better_repr(data))
        elif isinstance(datatype, int) and data == minint:
            # convert to hex, since decimal representation
            # would result in 'LOAD_CONST; UNARY_NEGATIVE'
            # change:hG/2002-02-07: this was done for all negative integers
            # todo: check whether this is necessary in Python 2.1
            self.write(hex(data))
        elif datatype is type(Ellipsis):
            self.write("...")
        elif attr is None:
            # LOAD_CONST 'None' only occurs, when None is
            # implicit eg. in 'return' w/o params
            # pass
            self.write("None")
        elif isinstance(data, tuple):
            self.pp_tuple(data)
        elif isinstance(attr, bool):
            self.write(repr(attr))
        elif self.FUTURE_UNICODE_LITERALS:
            # The FUTURE_UNICODE_LITERALS compiler flag
            # in 2.6 on change the way
            # strings are interpreted:
            #    u'xxx' -> 'xxx'
            #    xxx'   -> b'xxx'
            if isinstance(data, str):
                self.write("b" + repr(data))
            else:
                self.write(repr(data))
        else:
            self.write(repr(data))
        # LOAD_CONST is a terminal, so stop processing/recursing early
        self.prune()

    def n_ifelsestmtr(self, node):
        if node[2] == "COME_FROM":
            return_stmts_node = node[3]
            node.kind = "ifelsestmtr2"
        else:
            return_stmts_node = node[2]
        if len(return_stmts_node) != 2:
            self.default(node)

        if not (
            return_stmts_node[0][0][0] == "ifstmt"
            and return_stmts_node[0][0][0][1][0] == "return_if_stmts"
        ) and not (
            return_stmts_node[0][-1][0] == "ifstmt"
            and return_stmts_node[0][-1][0][1][0] == "return_if_stmts"
        ):
            self.default(node)
            return

        self.write(self.indent, "if ")
        self.preorder(node[0])
        self.println(":")
        self.indent_more()
        self.preorder(node[1])
        self.indent_less()

        if_ret_at_end = False
        if len(return_stmts_node[0]) >= 3:
            if (
                return_stmts_node[0][-1][0] == "ifstmt"
                and return_stmts_node[0][-1][0][1][0] == "return_if_stmts"
            ):
                if_ret_at_end = True

        past_else = False
        prev_stmt_is_if_ret = True
        for n in return_stmts_node[0]:
            if n[0] == "ifstmt" and n[0][1][0] == "return_if_stmts":
                if prev_stmt_is_if_ret:
                    n[0].kind = "elifstmt"
                prev_stmt_is_if_ret = True
            else:
                prev_stmt_is_if_ret = False
                if not past_else and not if_ret_at_end:
                    self.println(self.indent, "else:")
                    self.indent_more()
                    past_else = True
            self.preorder(n)
        if not past_else or if_ret_at_end:
            self.println(self.indent, "else:")
            self.indent_more()
        self.preorder(return_stmts_node[1])
        self.indent_less()
        self.prune()

    n_ifelsestmtr2 = n_ifelsestmtr

    def n_elifelsestmtr(self, node):
        if node[2] == "COME_FROM":
            return_stmts_node = node[3]
            node.kind = "elifelsestmtr2"
        else:
            return_stmts_node = node[2]

        if len(return_stmts_node) != 2:
            self.default(node)

        for n in return_stmts_node[0]:
            if not (n[0] == "ifstmt" and n[0][1][0] == "return_if_stmts"):
                self.default(node)
                return

        self.write(self.indent, "elif ")
        self.preorder(node[0])
        self.println(":")
        self.indent_more()
        self.preorder(node[1])
        self.indent_less()

        for n in return_stmts_node[0]:
            n[0].kind = "elifstmt"
            self.preorder(n)
        self.println(self.indent, "else:")
        self.indent_more()
        self.preorder(return_stmts_node[1])
        self.indent_less()
        self.prune()

    def n_alias(self, node):
        if self.version <= (2, 1):
            if len(node) == 2:
                store = node[1]
                assert store == "store"
                if store[0].pattr == node[0].pattr:
                    self.write("import %s\n" % node[0].pattr)
                else:
                    self.write("import %s as %s\n" % (node[0].pattr, store[0].pattr))
                    pass
                pass
            self.prune()  # stop recursing

        store_node = node[-1][-1]
        assert store_node.kind.startswith("STORE_")
        iname = node[0].pattr  # import name
        sname = store_node.pattr  # store_name
        if iname and iname == sname or iname.startswith(sname + "."):
            self.write(iname)
        else:
            self.write(iname, " as ", sname)
        self.prune()  # stop recursing

    n_alias37 = n_alias

    def n_mkfunc(self, node):

        # MAKE_FUNCTION ..
        code_node = node[-3]
        if not iscode(code_node.attr):
            # docstring exists
            code_node = node[-4]

        code = code_node.attr
        assert iscode(code)

        func_name = code.co_name
        self.write(func_name)

        self.indent_more()

        make_function36(self, node, is_lambda=False, code_node=code_node)

        if len(self.param_stack) > 1:
            self.write("\n\n")
        else:
            self.write("\n\n\n")
        self.indent_less()
        self.prune()  # stop recursing

    def n_docstring(self, node):

        indent = self.indent
        doc_node = node[0]
        if doc_node.attr:
            docstring = doc_node.attr
            if not isinstance(docstring, str):
                # FIXME: we have mistakenly tagged something as a doc
                # string in transform when it isn't one.
                # The rule in n_mkfunc is pretty flaky.
                self.prune()
                return
        else:
            docstring = node[0].pattr

        quote = '"""'
        if docstring.find(quote) >= 0:
            if docstring.find("'''") == -1:
                quote = "'''"

        self.write(indent)
        docstring = repr(docstring.expandtabs())[1:-1]

        for (orig, replace) in (
            ("\\\\", "\t"),
            ("\\r\\n", "\n"),
            ("\\n", "\n"),
            ("\\r", "\n"),
            ('\\"', '"'),
            ("\\'", "'"),
        ):
            docstring = docstring.replace(orig, replace)

        # Do a raw string if there are backslashes but no other escaped characters:
        # also check some edge cases
        if (
            "\t" in docstring
            and "\\" not in docstring
            and len(docstring) >= 2
            and docstring[-1] != "\t"
            and (docstring[-1] != '"' or docstring[-2] == "\t")
        ):
            self.write("r")  # raw string
            # Restore backslashes unescaped since raw
            docstring = docstring.replace("\t", "\\")
        else:
            # Escape the last character if it is the same as the
            # triple quote character.
            quote1 = quote[-1]
            if len(docstring) and docstring[-1] == quote1:
                docstring = docstring[:-1] + "\\" + quote1

            # Escape triple quote when needed
            if quote == '"""':
                replace_str = '\\"""'
            else:
                assert quote == "'''"
                replace_str = "\\'''"

            docstring = docstring.replace(quote, replace_str)
            docstring = docstring.replace("\t", "\\\\")

        lines = docstring.split("\n")

        self.write(quote)
        if len(lines) == 0:
            self.println(quote)
        elif len(lines) == 1:
            self.println(lines[0], quote)
        else:
            self.println(lines[0])
            for line in lines[1:-1]:
                if line:
                    self.println(line)
                else:
                    self.println("\n\n")
                    pass
                pass
            self.println(lines[-1], quote)
        self.prune()

    def n_lambda_body(self, node):
        make_function36(self, node, is_lambda=True, code_node=node[-2])
        self.prune()  # stop recursing

    def comprehension_walk(self, node, iter_index, code_index=-5):
        p = self.prec
        self.prec = 27

        # FIXME: clean this up
        if node == "dict_comp":
            cn = node[1]
        elif node in ("generator_exp", "generator_exp_async"):
            if node[0] == "load_genexpr":
                load_genexpr = node[0]
            elif node[1] == "load_genexpr":
                load_genexpr = node[1]
            cn = load_genexpr[0]
        else:
            if len(node[1]) > 1 and hasattr(node[1][1], "attr"):
                # Python 3.3+ does this
                cn = node[1][1]
            else:
                assert False, "Can't find code for comprehension"

        assert iscode(cn.attr)

        code = Code(cn.attr, self.scanner, self.currentclass)

        # FIXME: is there a way we can avoid this?
        # The problem is that in filter in top-level list comprehensions we can
        # encounter comprehensions of other kinds, and lambdas
        if is_lambda_mode(self.compile_mode):
            p_save = self.p
            self.p = get_python_parser(
                self.version, compile_mode="exec", is_pypy=self.is_pypy,
            )
            tree = self.build_ast(code._tokens, code._customize, code)
            self.p = p_save
        else:
            tree = self.build_ast(code._tokens, code._customize, code)
        self.customize(code._customize)

        # Remove single reductions as in ("stmts", "sstmt"):
        while len(tree) == 1:
            tree = tree[0]

        if tree == "stmts":
            # FIXME: rest is a return None?
            # Verify this
            # rest = tree[1:]
            tree = tree[0]
        elif tree == "lambda_start":
            assert len(tree) <= 3
            tree = tree[-2]
            if tree == "return_expr_lambda":
                tree = tree[1]
            pass

        n = tree[iter_index]
        assert n == "comp_iter", n

        # Find the comprehension body. It is the inner-most
        # node that is not list_.. .
        while n == "comp_iter":  # list_iter
            n = n[0]  # recurse one step
            if n == "comp_for":
                if n[0] == "SETUP_LOOP":
                    n = n[4]
                else:
                    n = n[3]
            elif n == "comp_if":
                n = n[1]
            elif n in (
                "comp_if_not",
                "comp_if_not_and",
                "comp_if_not_or",
                "comp_if_or",
            ):
                n = n[-1]

        assert n == "comp_body", n.kind

        self.preorder(n[0])
        if node == "generator_exp_async":
            self.write(" async")
            iter_var_index = iter_index - 2
        else:
            iter_var_index = iter_index - 1
        self.write(" for ")
        self.preorder(tree[iter_var_index])
        self.write(" in ")
        if node[2] == "expr":
            iter_expr = node[2]
        else:
            iter_expr = node[-3]
        assert iter_expr == "expr"
        self.preorder(iter_expr)
        self.preorder(tree[iter_index])
        self.prec = p

    def n_generator_exp(self, node):
        self.write("(")
        if node[0].kind in ("load_closure", "load_genexpr") and self.version >= (3, 8):
            self.closure_walk(
                node, collection_index=4 if isinstance(node[4], SyntaxTree) else 3
            )
        else:
            code_index = -6
            iter_index = 4 if self.version < (3, 8) else 3
            self.comprehension_walk(node, iter_index=iter_index, code_index=code_index)
        self.write(")")
        self.prune()

    n_generator_exp_async = n_generator_exp

    def n_set_comp(self, node):
        self.write("{")
        if node[0] in ["LOAD_SETCOMP", "LOAD_DICTCOMP"]:
            self.comprehension_walk_newer(node, 1, 0)
        elif node[0].kind == "load_closure":
            self.closure_walk(node, collection_index=4)
        else:
            self.comprehension_walk(node, iter_index=4)
        self.write("}")
        self.prune()

    n_dict_comp = n_set_comp

    # In the old days this node would never get called because
    # it was embedded inside some sort of comprehension
    # Nowadays, we allow starting any code object, not just
    # a top-level module. In doing so we can
    # now encounter this outside of the embedding of
    # a comprehension.
    def n_set_comp_async(self, node):
        self.write("{")
        if node[0] in ["BUILD_SET_0", "BUILD_MAP_0"]:
            self.comprehension_walk_newer(node[1], 3, 0, collection_node=node[1])
        if node[0] in ["LOAD_SETCOMP", "LOAD_DICTCOMP"]:
            get_aiter = node[3]
            assert get_aiter == "get_aiter", node.kind
            self.comprehension_walk_newer(node, 1, 0, collection_node=get_aiter[0])
        self.write("}")
        self.prune()

    n_dict_comp_async = n_set_comp_async

    def comprehension_walk_newer(
        self, node, iter_index: int, code_index: int = -5, collection_node=None
    ):
        """Non-closure-based comprehensions the way they are done in Python3
        and some Python 2.7. Note: there are also other set comprehensions.
        Build the body of a comprehension function and then
        find the comprehension node buried in the tree which may
        be surrounded with start-like symbols or dominiators,.
        """
        # FIXME: DRY with listcomp_closure3

        # ? Is this needed
        p = self.prec

        # FIXME? Nonterminals in grammar maybe should be split out better?
        # Maybe test on self.compile_mode?
        if (
            isinstance(node[0], Token)
            and node[0].kind.startswith("LOAD")
            and iscode(node[0].attr)
        ):
            if node[3] == "get_aiter":
                compile_mode = self.compile_mode
                self.compile_mode = "genexpr"
                is_lambda = self.is_lambda
                self.is_lambda = True
                tree = self.get_comprehension_function(node, code_index)
                self.compile_mode = compile_mode
                self.is_lambda = is_lambda
            else:
                tree = self.get_comprehension_function(node, code_index)
        elif (
            len(node) > 2
            and isinstance(node[2], Token)
            and node[2].kind.startswith("LOAD")
            and iscode(node[2].attr)
        ):
            tree = self.get_comprehension_function(node, 2)
        else:
            tree = node

        # Pick out important parts of the comprehension:
        # * the variable we iterate over: "store"
        # * the results we accumulate: "n"

        store = None
        if node == "list_comp_async":
            # We have two different kinds of grammar rules:
            #   list_comp_async ::= LOAD_LISTCOMP LOAD_STR MAKE_FUNCTION_0 expr ...
            # and:
            #  list_comp_async  ::= BUILD_LIST_0 LOAD_ARG list_afor2

            if tree[0] == "expr" and tree[0][0] == "list_comp_async":
                tree = tree[0][0]
            if tree[0] == "BUILD_LIST_0":
                list_afor2 = tree[2]
                assert list_afor2 == "list_afor2"
                store = list_afor2[1]
                assert store == "store"
                n = list_afor2[2]
            else:
                # ???
                pass
        elif node.kind in ("dict_comp_async", "set_comp_async"):
            # We have two different kinds of grammar rules:
            #   dict_comp_async ::= LOAD_DICTCOMP LOAD_STR MAKE_FUNCTION_0 expr ...
            #   set_comp_async  ::= LOAD_SETCOMP LOAD_STR MAKE_FUNCTION_0 expr ...
            # and:
            #  dict_comp_async  ::= BUILD_MAP_0 genexpr_func_async
            #  set_comp_async   ::= BUILD_SET_0 genexpr_func_async
            if tree[0].kind in ("BUILD_MAP_0", "BUILD_SET_0"):
                genexpr_func_async = tree[1]
                if genexpr_func_async == "genexpr_func_async":
                    store = genexpr_func_async[2]
                    assert store == "store"
                    n = genexpr_func_async[3]
                else:
                    set_afor2 = genexpr_func_async
                    assert set_afor2 == "set_afor2"
                    n = set_afor2[1]
                    store = n[1]
                    collection_node = node[3]
            else:
                # ???
                pass

        elif node == "list_afor":
            collection_node = node[0]
            list_afor2 = node[1]
            assert list_afor2 == "list_afor2"
            store = list_afor2[1]
            assert store == "store"
            n = list_afor2[2]
        elif node == "set_afor2":
            collection_node = node[0]
            set_iter_async = node[1]
            assert set_iter_async == "set_iter_async"

            store = set_iter_async[1]
            assert store == "store"
            n = set_iter_async[2]
        else:
            n = tree[iter_index]

        if tree in (
            "dict_comp_func",
            "genexpr_func_async",
            "generator_exp",
            "list_comp",
            "set_comp",
            "set_comp_func",
            "set_comp_func_header",
        ):
            for k in tree:
                if k.kind in ("comp_iter", "list_iter", "set_iter"):
                    n = k
                elif k == "store":
                    store = k
                    pass
                pass
            pass
        elif tree.kind in ("list_comp_async", "dict_comp_async", "set_afor2"):
            # Handled this condition above.
            pass
        else:
            # FIXME: we get this when we parse lambda's explicitly.
            # And here we've already printed/handled the list comprehension
            # this iteration is duplicate in seeing the list-comprehension code
            # item again. Is this a larger duplicate parsing problem?
            # Not sure what the best this thing to do is.

            if n.kind == "return_expr_lambda":
                self.prune()
            assert n.kind in ("list_iter", "comp_iter", "set_iter_async"), n

        # FIXME: I'm not totally sure this is right.

        # Find the list comprehension body. It is the inner-most
        # node that is not list_.. .
        if_nodes = []
        if_node_parent = None
        comp_for = None
        comp_store = None
        if n == "comp_iter":
            comp_for = n
            if not store:
                comp_store = tree[3]

        # Iterate to find the inner-most "store".
        # We'll come back to the list iteration below.

        while n in (
            "list_iter",
            "list_afor",
            "list_afor2",
            "comp_iter",
            "set_afor",
            "set_afor2",
            "set_iter",
            "set_iter_async",
        ):
            # iterate one nesting deeper
            if n in ("list_afor", "set_afor"):
                n = n[1]
            elif n in ("list_afor2", "set_afor2", "set_iter_async"):
                if n[1] == "store":
                    store = n[1]
                n = n[2]
            else:
                n = n[0]

            if n in ("comp_for", "list_for", "set_for"):
                collection_node = n
                if n[2] == "store" and not store:
                    store = n[2]
                    if not comp_store:
                        comp_store = store
                n = n[3]
                assert n.kind in ("comp_iter", "list_iter", "set_iter")
            elif n in ("list_if_chained",):
                #  list_if_chained ::= list_if_compare ... list_iter
                if_nodes.append(n[0])
                assert n[0] == "list_if_compare"
                n = n[-1]
                assert n == "list_iter"
            elif n in (
                "comp_if_not_and",
                "comp_if_or",
                "comp_if_or2",
                "comp_if_or_not",
                "comp_if_not_or",
            ):
                if_nodes.append(n)
                n = n[-1]
                assert n == "comp_iter"
            elif n in (
                "list_if",
                "list_if_not",
                "list_if37",
                "list_if37_not",
                "comp_if",
                "comp_if_not",
            ):
                if n in ("list_if37", "list_if37_not", "comp_if"):
                    if n == "comp_if":
                        if_nodes.append(n[0])
                    n = n[1]
                else:
                    if n in ("comp_if_not",):
                        if_nodes.append(n)
                    else:
                        if_node_parent = n
                        if_nodes.append(n[0])
                    if n[1] == "store":
                        store = n[1]
                    n = n[-2] if n[-1] == "come_from_opt" else n[-1]
                    pass
            elif n.kind == "list_if_and_or":
                if_nodes.append(n[-1][0])
                n = n[-1]
            pass

        # Python 2.7+ starts including set_comp_body
        # Python 3.5+ starts including set_comp_func
        assert store, "Couldn't find store in list/set comprehension"

        # A problem created with later Python code generation is that there
        # is a lambda set up with a dummy argument name that is then called
        # So we can't just translate that as is but need to replace the
        # dummy name. Below we are picking out the variable name as seen
        # in the code. And trying to generate code for the other parts
        # that don't have the dummy argument name in it.
        # Another approach might be to be able to pass in the source name
        # for the dummy argument.

        if node not in ("list_afor", "set_afor"):
            self.preorder(n[0])

        if node.kind in (
            "dict_comp_async",
            "genexpr_func_async",
            "list_afor",
            "list_comp_async",
            "set_afor2",
            "set_comp_async",
        ):
            self.write(" async")
            in_node_index = 5 if len(node) > 6 and node[5] == "expr" else 3
        elif len(node) >= 3 and node[3] == "expr":
            in_node_index = 3
            collection_node = node[3]
            assert collection_node == "expr"
        else:
            in_node_index = -3

        self.write(" for ")

        if comp_store:
            self.preorder(comp_store)
        else:
            self.preorder(store)

        self.write(" in ")

        if node == "list_afor":
            list_afor2 = node[1]
            assert list_afor2 == "list_afor2"
            list_iter = list_afor2[2]
            assert list_iter == "list_iter"
            self.preorder(collection_node)
            if_nodes = []
        elif node == "set_comp_async":
            self.preorder(collection_node)
            if_nodes = []
        elif is_lambda_mode(self.compile_mode):
            if node == "list_comp_async":
                self.preorder(node[1])
            elif collection_node is None:
                assert node[3] == "expr"
                self.preorder(node[3])
            else:
                self.preorder(collection_node[0])
        else:
            if not collection_node:
                collection_node = node[in_node_index]
            self.preorder(collection_node)

        # Here is where we handle nested list iterations.
        if tree in ("list_comp", "set_comp"):
            list_iter = tree[1]
            assert list_iter in ("list_iter", "set_iter")
            list_for = list_iter[0]
            if list_for == "list_for":
                # In the grammar we have:
                #    list_for ::= _  for_iter store list_iter ...
                # or
                #    set_for ::= _   set_iter store set_iter ...
                list_iter_inner = list_for[3]
                assert list_iter_inner in ("list_iter", "set_iter")
                # If we have set_comp_body, we've done this above.
                if not (
                    list_iter_inner == "set_iter"
                    and list_iter_inner[0] == "set_comp_body"
                ):
                    self.preorder(list_iter_inner)
                    if if_node_parent == list_iter_inner[0]:
                        self.prec = p
                        return
                comp_store = None
            pass

        if tree == "set_comp_func":
            comp_iter = tree[5]
            assert comp_iter == "comp_iter"
            comp_for = comp_iter[0]
            if comp_for == "comp_for":
                self.template_engine(
                    ("for %c in %p", (2, "store"), (0, "expr", NO_PARENTHESIS_EVER)),
                    comp_for,
                )
        if comp_store:
            self.preorder(comp_for)
        for if_node in if_nodes:
            self.write(" if ")
            if if_node in (
                "comp_if_not_and",
                "comp_if_not_or",
                "comp_if_or",
                "comp_if_or2",
                "comp_if_or_not",
            ):
                self.preorder(if_node)
            else:
                # FIXME: go over these to add more of this in the template,
                # not here.
                if if_node in ("list_if_not", "comp_if_not", "list_if37_not"):
                    self.write("not ")
                    pass
                self.prec = 27
                self.preorder(if_node[0])
            pass
        self.prec = p

    def n_list_comp(self, node):
        self.write("[")
        if node[0].kind == "load_closure":
            self.listcomp_closure3(node)
        else:
            if node == "list_comp_async":
                # comprehension_walk_newer needs to pick out from node since
                # there isn't an iter_index at the top level
                list_iter_index = None
            else:
                list_iter_index = 1
            self.comprehension_walk_newer(node, list_iter_index, 0)
        self.write("]")
        self.prune()

    n_list_comp_async = n_list_comp

    def get_comprehension_function(self, node, code_index: int):
        """
        Build the body of a comprehension function and then
        find the comprehension node buried in the tree which may
        be surrounded with start-like symbols or dominiators,.
        """
        self.prec = 27
        code_node = node[code_index]
        if code_node == "load_genexpr":
            code_node = code_node[0]

        code_obj = code_node.attr
        assert iscode(code_obj), code_node

        code = Code(code_obj, self.scanner, self.currentclass, self.debug_opts["asm"])

        # FIXME: is there a way we can avoid this?
        # The problem is that in filter in top-level list comprehensions we can
        # encounter comprehensions of other kinds, and lambdas
        if is_lambda_mode(self.compile_mode):
            p_save = self.p
            self.p = get_python_parser(
                self.version, compile_mode="exec", is_pypy=self.is_pypy,
            )
            tree = self.build_ast(
                code._tokens, code._customize, code, is_lambda=self.is_lambda
            )
            self.p = p_save
        else:
            tree = self.build_ast(
                code._tokens, code._customize, code, is_lambda=self.is_lambda
            )

        self.customize(code._customize)

        # skip over: sstmt, stmt, return, return_expr
        # and other singleton derivations
        if tree == "lambda_start":
            tree = tree[0]

        while len(tree) == 1 or (tree in ("stmt", "sstmt", "return", "return_expr")):
            self.prec = 100
            tree = tree[0]
        return tree

    def n_dict_comp_func(self, node):
        self.write("{")
        self.comprehension_walk_newer(node, 5, 0, collection_node=node[1])
        self.write("}")
        self.prune()

    n_set_comp_func = n_dict_comp_func

    def closure_walk(self, node, collection_index: int):
        """Dictionary and Set comprehensions using closures.
        """
        p = self.prec

        code_index = 0 if node[0] == "load_genexpr" else 1
        tree = self.get_comprehension_function(node, code_index=code_index)

        if tree.kind in ("stmts", "lambda_start"):
            tree = tree[0]

        # Remove single reductions as in ("stmts", "sstmt"):
        while len(tree) == 1 or tree.kind in ("return_expr_lambda",):
            tree = tree[0]

        if tree == "genexpr_func_async":
            store = tree[2]
            iter_index = 3
            collection_index = 3
        elif tree == "genexpr_func":
            store = tree[3]
            iter_index = 4
        elif tree == "set_comp":
            tree = tree[1][0]
            assert tree == "set_for", tree.kind
            store = tree[2]
            iter_index = 3
            collection_index = 4
        else:
            store = tree[4]
            iter_index = 5

        if node[collection_index] == "get_iter":
            expr = node[collection_index][0]
            assert expr == "expr", expr.kind
            collection = expr
        else:
            collection = node[collection_index]
        n = tree[iter_index]
        list_if = None
        write_if = False

        assert n in ("comp_iter", "set_iter")

        # Find inner-most node.
        while n == "comp_iter":
            n = n[0]  # recurse one step

            # FIXME: adjust for set comprehension
            if n == "list_for":
                store = n[2]
                n = n[3]
            elif n[0].kind == "c_compare":
                list_if = n
                n = n[-1]
            elif n in (
                "list_if",
                "list_if_not",
                "list_if_and_or",
                "comp_if",
                "comp_if_not",
            ):
                if n[0].kind == "expr":
                    list_if = n
                    n = n[-1]  # n -1 ?
                elif n[0].kind in ("expr_pjif", "expr_pjiff"):
                    list_if = n
                    n = n[1]
                elif n[0].kind in ("or_jump_if_false_cf", "or_jump_if_false_loop_cf"):
                    list_if = n[1]
                    n = n[1]
                else:
                    if len(n) == 2:
                        list_if = n[0]
                        n = n[1]
                    else:
                        list_if = n[1]
                        n = n[2]
                pass
            pass

        assert n == "comp_body", tree

        self.preorder(n[0])
        self.write(" for ")
        self.preorder(store)
        self.write(" in ")
        self.preorder(collection)
        if list_if:
            self.preorder(list_if)
        self.prec = p

    def n_classdef(self, node):

        self.n_classdef36(node)

        # class definition ('class X(A,B,C):')
        cclass = self.currentclass

        # Pick out various needed bits of information
        # * class_name - the name of the class
        # * subclass_info - the parameters to the class  e.g.
        #      class Foo(bar, baz)
        #             -----------
        # * subclass_code - the code for the subclass body

        if node == "classdefdeco2":
            build_class = node
        else:
            build_class = node[0]
        build_list = build_class[1][0]
        if hasattr(build_class[-3][0], "attr"):
            subclass_code = build_class[-3][0].attr
            class_name = build_class[0].pattr
        elif (
            build_class[-3] == "mkfunc"
            and node == "classdefdeco2"
            and build_class[-3][0] == "load_closure"
        ):
            subclass_code = build_class[-3][1].attr
            class_name = build_class[-3][0][0].pattr
        elif hasattr(node[0][0], "pattr"):
            subclass_code = build_class[-3][1].attr
            class_name = node[0][0].pattr
        else:
            raise "Internal Error n_classdef: cannot find class name"

        if node == "classdefdeco2":
            self.write("\n")
        else:
            self.write("\n\n")

        self.currentclass = str(class_name)
        self.write(self.indent, "class ", self.currentclass)

        self.print_super_classes(build_list)
        self.println(":")

        # class body
        self.indent_more()
        self.build_class(subclass_code)
        self.indent_less()

        self.currentclass = cclass
        if len(self.param_stack) > 1:
            self.write("\n\n")
        else:
            self.write("\n\n\n")

        self.prune()

    n_classdefdeco2 = n_classdef

    def print_super_classes(self, node):
        if not (node == "tuple"):
            return

        n_subclasses = len(node[:-1])
        if n_subclasses > 0 or self.version > (2, 4):
            # Not an old-style pre-2.2 class
            self.write("(")

        line_separator = ", "
        sep = ""
        for elem in node[:-1]:
            value = self.traverse(elem)
            self.write(sep, value)
            sep = line_separator

        if n_subclasses > 0 or self.version > (2, 4):
            # Not an old-style pre-2.2 class
            self.write(")")

    def print_super_classes3(self, node):
        n = len(node) - 1
        if node.kind != "expr":
            if node == "kwarg":
                self.template_engine(("(%[0]{attr}=%c)", 1), node)
                return

            kwargs = None
            opname = node[n].kind
            assert opname.startswith("CALL_FUNCTION") or opname.startswith(
                "CALL_METHOD"
            )

            if node[n].kind.startswith("CALL_FUNCTION_KW"):
                # 3.6+ starts doing this
                kwargs = node[n - 1].attr
                assert isinstance(kwargs, tuple)
                i = n - (len(kwargs) + 1)
                j = 1 + n - node[n].attr
            else:
                i = start = n - 2
                for i in range(start, 0, -1):
                    if not node[i].kind in ["expr", "call", "LOAD_CLASSNAME"]:
                        break
                    pass

                if i == start:
                    return
                i += 2

            line_separator = ", "
            sep = ""
            self.write("(")
            if kwargs:
                # Last arg is tuple of keyword values: omit
                l = n - 1
            else:
                l = n

            if kwargs:
                # 3.6+ does this
                while j < i:
                    self.write(sep)
                    value = self.traverse(node[j])
                    self.write("%s" % value)
                    sep = line_separator
                    j += 1

                j = 0
                while i < l:
                    self.write(sep)
                    value = self.traverse(node[i])
                    self.write("%s=%s" % (kwargs[j], value))
                    sep = line_separator
                    j += 1
                    i += 1
            else:
                while i < l:
                    value = self.traverse(node[i])
                    i += 1
                    self.write(sep, value)
                    sep = line_separator
                    pass
            pass
        elif node == "dict_comp_async":
            # Handled this condition above
            pass
        else:
            if node[0] == "LOAD_STR":
                return
            value = self.traverse(node[0])
            self.write("(")
            self.write(value)
            pass

        self.write(")")

    def n_dict(self, node):
        """
        prettyprint a dict
        'dict' is something like k = {'a': 1, 'b': 42}"
        We will use source-code line breaks to guide us when to break.
        """
        p = self.prec
        self.prec = 100

        self.indent_more(INDENT_PER_LEVEL)
        sep = INDENT_PER_LEVEL[:-1]
        if node[0] != "dict_entry":
            self.write("{")
        line_number = self.line_number

        if self.version >= (3, 0) and not self.is_pypy:
            if node[0].kind.startswith("kvlist"):
                # Python 3.5+ style key/value list in dict
                kv_node = node[0]
                l = list(kv_node)
                length = len(l)
                if kv_node[-1].kind.startswith("BUILD_MAP"):
                    length -= 1
                i = 0

                # Respect line breaks from source
                while i < length:
                    self.write(sep)
                    name = self.traverse(l[i], indent="")
                    if i > 0:
                        line_number = self.indent_if_source_nl(
                            line_number, self.indent + INDENT_PER_LEVEL[:-1]
                        )
                    line_number = self.line_number
                    self.write(name, ": ")
                    value = self.traverse(
                        l[i + 1], indent=self.indent + (len(name) + 2) * " "
                    )
                    self.write(value)
                    sep = ", "
                    if line_number != self.line_number:
                        sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
                        line_number = self.line_number
                    i += 2
                    pass
                pass
            elif len(node) > 1 and node[1].kind.startswith("kvlist"):
                # Python 3.0..3.4 style key/value list in dict
                kv_node = node[1]
                l = list(kv_node)
                if len(l) > 0 and l[0].kind == "kv3":
                    # Python 3.2 does this
                    kv_node = node[1][0]
                    l = list(kv_node)
                i = 0
                while i < len(l):
                    self.write(sep)
                    name = self.traverse(l[i + 1], indent="")
                    if i > 0:
                        line_number = self.indent_if_source_nl(
                            line_number, self.indent + INDENT_PER_LEVEL[:-1]
                        )
                        pass
                    line_number = self.line_number
                    self.write(name, ": ")
                    value = self.traverse(
                        l[i], indent=self.indent + (len(name) + 2) * " "
                    )
                    self.write(value)
                    sep = ", "
                    if line_number != self.line_number:
                        sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
                        line_number = self.line_number
                    else:
                        sep += " "
                    i += 3
                    pass
                pass
            elif node[-1].kind.startswith("BUILD_CONST_KEY_MAP"):
                # Python 3.6+ style const map
                keys = node[-2].pattr
                values = node[:-2]
                # FIXME: Line numbers?
                for key, value in zip(keys, values):
                    self.write(sep)
                    self.write(repr(key))
                    line_number = self.line_number
                    self.write(":")
                    self.write(self.traverse(value[0]))
                    sep = ", "
                    if line_number != self.line_number:
                        sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
                        line_number = self.line_number
                    else:
                        sep += " "
                        pass
                    pass
                if sep.startswith(",\n"):
                    self.write(sep[1:])
                pass
            elif node[0].kind.startswith("dict_entry"):
                assert self.version >= (3, 5)
                template = ("%C", (0, len(node[0]), ", **"))
                self.template_engine(template, node[0])
                sep = ""
            elif node[-1].kind.startswith("BUILD_MAP_UNPACK") or node[
                -1
            ].kind.startswith("dict_entry"):
                assert self.version >= (3, 5)
                # FIXME: I think we can intermingle dict_comp's with other
                # dictionary kinds of things. The most common though is
                # a sequence of dict_comp's
                kwargs = node[-1].attr
                template = ("**%C", (0, kwargs, ", **"))
                self.template_engine(template, node)
                sep = ""

            pass
        elif self.version >= (3, 6) and self.is_pypy:
            # FIXME: DRY with above
            if node[-1].kind.startswith("BUILD_CONST_KEY_MAP"):
                # Python 3.6+ style const map
                keys = node[-2].pattr
                values = node[:-2]
                # FIXME: Line numbers?
                for key, value in zip(keys, values):
                    self.write(sep)
                    self.write(repr(key))
                    line_number = self.line_number
                    self.write(":")
                    self.write(self.traverse(value[0]))
                    sep = ", "
                    if line_number != self.line_number:
                        sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
                        line_number = self.line_number
                    else:
                        sep += " "
                        pass
                    pass
                if sep.startswith(",\n"):
                    self.write(sep[1:])
                pass
        else:
            # Python 2 style kvlist. Find beginning of kvlist.
            if node[0].kind.startswith("BUILD_MAP"):
                if len(node) > 1 and node[1].kind in ("kvlist", "kvlist_n"):
                    kv_node = node[1]
                else:
                    kv_node = node[1:]
            else:
                assert node[-1].kind.startswith("kvlist")
                kv_node = node[-1]

            first_time = True
            for kv in kv_node:
                assert kv in ("kv", "kv2", "kv3")

                # kv ::= DUP_TOP expr ROT_TWO expr STORE_SUBSCR
                # kv2 ::= DUP_TOP expr expr ROT_THREE STORE_SUBSCR
                # kv3 ::= expr expr STORE_MAP

                # FIXME: DRY this and the above
                indent = self.indent + "  "
                if kv == "kv":
                    self.write(sep)
                    name = self.traverse(kv[-2], indent="")
                    if first_time:
                        line_number = self.indent_if_source_nl(line_number, indent)
                        first_time = False
                        pass
                    line_number = self.line_number
                    self.write(name, ": ")
                    value = self.traverse(
                        kv[1], indent=self.indent + (len(name) + 2) * " "
                    )
                elif kv == "kv2":
                    self.write(sep)
                    name = self.traverse(kv[1], indent="")
                    if first_time:
                        line_number = self.indent_if_source_nl(line_number, indent)
                        first_time = False
                        pass
                    line_number = self.line_number
                    self.write(name, ": ")
                    value = self.traverse(
                        kv[-3], indent=self.indent + (len(name) + 2) * " "
                    )
                elif kv == "kv3":
                    self.write(sep)
                    name = self.traverse(kv[-2], indent="")
                    if first_time:
                        line_number = self.indent_if_source_nl(line_number, indent)
                        first_time = False
                        pass
                    line_number = self.line_number
                    self.write(name, ": ")
                    line_number = self.line_number
                    value = self.traverse(
                        kv[0], indent=self.indent + (len(name) + 2) * " "
                    )
                    pass
                self.write(value)
                sep = ", "
                if line_number != self.line_number:
                    sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
                    line_number = self.line_number
                else:
                    sep += " "
                    pass
                pass
            pass
        if sep.startswith(",\n"):
            self.write(sep[1:])
        if node[0] != "dict_entry":
            self.write("}")
        self.indent_less(INDENT_PER_LEVEL)
        self.prec = p
        self.prune()

    def n_list(self, node):
        """
        prettyprint a dict, list, set or tuple.
        """
        p = self.prec

        if len(node) == 1:
            lastnode = node[0]
            flat_elems = []
        else:
            self.prec = PRECEDENCE["yield"] - 1
            lastnode = node.pop()
            flat_elems = flatten_list(node)

        lastnodetype = lastnode.kind

        if lastnodetype.startswith("BUILD_LIST") or lastnodetype == "expr":
            self.write("[")
            endchar = "]"

        elif lastnodetype.startswith("BUILD_MAP_UNPACK"):
            self.write("{*")
            endchar = "}"

        elif lastnodetype.startswith("BUILD_SET"):
            self.write("{")
            endchar = "}"

        elif lastnodetype.startswith("BUILD_TUPLE") or node == "tuple":
            # Tuples can appear places that can NOT
            # have parenthesis around them, like array
            # subscripts. We check for that by seeing
            # if a tuple item is some sort of slice.
            no_parens = False
            for n in node:
                if n == "arg":
                    n = n[0]
                if n == "expr" and n[0].kind.startswith("slice"):
                    no_parens = True
                    break
                pass
            if no_parens:
                endchar = ""
            else:
                self.write("(")
                endchar = ")"
                pass

        elif lastnodetype.startswith("ROT_TWO"):
            self.write("(")
            endchar = ")"

        else:
            # from trepan.api import debug; debug()
            raise TypeError(
                "Internal Error: n_build_list expects list, tuple, set, or unpack"
            )

        self.indent_more(INDENT_PER_LEVEL)
        sep = ""
        for elem in flat_elems:
            if elem in ("ROT_THREE", "EXTENDED_ARG"):
                continue
            assert elem in ("expr", "list", "lists")
            line_number = self.line_number
            value = self.traverse(elem)
            if line_number != self.line_number:
                sep += "\n" + self.indent + INDENT_PER_LEVEL[:-1]
            else:
                if sep != "":
                    sep += " "
            self.write(sep, value)
            sep = ","
        if (
            isinstance(lastnode, Token)
            and lastnode.attr == 1
            and lastnodetype.startswith("BUILD_TUPLE")
        ):
            self.write(",")
        self.write(endchar)
        self.indent_less(INDENT_PER_LEVEL)

        self.prec = p
        self.prune()
        return

    n_set = n_build_set = n_tuple = n_list

    def n_attribute(self, node):
        if node[0] == "LOAD_CONST" or node[0] == "expr" and node[0][0] == "LOAD_CONST":
            # FIXME: I didn't record which constants parenthesis is
            # necessary. However, I suspect that we could further
            # refine this by looking at operator precedence and
            # eval'ing the constant value (pattr) and comparing with
            # the type of the constant.
            node.kind = "attribute_w_parens"
        self.default(node)

    def n_assign(self, node):
        # A horrible hack for Python 3.0 .. 3.2
        if (3, 0) <= self.version <= (3, 2) and len(node) == 2:
            if (
                node[0][0] == "LOAD_FAST"
                and node[0][0].pattr == "__locals__"
                and node[1][0].kind == "STORE_LOCALS"
            ):
                self.prune()
        self.default(node)

    def n_assign2(self, node):
        for n in node[-2:]:
            if n[0] == "unpack":
                n[0].kind = "unpack_w_parens"
        self.default(node)

    def n_assign3(self, node):
        for n in node[-3:]:
            if n[0] == "unpack":
                n[0].kind = "unpack_w_parens"
        self.default(node)

    def n_except_cond2(self, node):
        unpack_node = -3 if node[-1] == "come_from_opt" else -2
        if node[unpack_node][0] == "unpack":
            node[unpack_node][0].kind = "unpack_w_parens"
        self.default(node)

    def n_store(self, node):
        expr = node[0]
        if expr == "expr" and expr[0] == "LOAD_CONST" and node[1] == "STORE_ATTR":
            # FIXME: I didn't record which constants parenthesis is
            # necessary. However, I suspect that we could further
            # refine this by looking at operator precedence and
            # eval'ing the constant value (pattr) and comparing with
            # the type of the constant.
            node.kind = "store_w_parens"
        self.default(node)

    def n_unpack(self, node):
        if node[0].kind.startswith("UNPACK_EX"):
            # Python 3+
            before_count, after_count = node[0].attr
            for i in range(before_count + 1):
                self.preorder(node[i])
                if i != 0:
                    self.write(", ")
            self.write("*")
            for i in range(1, after_count + 2):
                self.preorder(node[before_count + i])
                if i != after_count + 1:
                    self.write(", ")
            self.prune()
            return
        if node[0] == "UNPACK_SEQUENCE_0":
            self.write("[]")
            self.prune()
            return
        for n in node[1:]:
            if n[0].kind == "unpack":
                n[0].kind = "unpack_w_parens"
        self.default(node)

    n_unpack_w_parens = n_unpack

    def template_engine(self, entry, startnode):
        """The format template interpetation engine.  See the comment at the
        beginning of this module for the how we interpret format
        specifications such as %c, %C, and so on.
        """

        # print("-----")
        # print(startnode)
        # print(entry[0])
        # print('======')
        fmt = entry[0]
        arg = 1
        i = 0

        m = escape.search(fmt)
        while m:
            i = m.end()
            self.write(m.group("prefix"))

            typ = m.group("type") or "{"
            node = startnode
            if m.group("child"):
                node = node[int(m.group("child"))]

            if typ == "%":
                self.write("%")
            elif typ == "+":
                self.line_number += 1
                self.indent_more()
            elif typ == "-":
                self.line_number += 1
                self.indent_less()
            elif typ == "|":
                self.line_number += 1
                self.write(self.indent)
            # Used mostly on the LHS of an assignment
            # BUILD_TUPLE_n is pretty printed and may take care of other uses.
            elif typ == ",":
                if node.kind in ("unpack", "unpack_w_parens") and node[0].attr == 1:
                    self.write(",")
            elif typ == "c":
                index = entry[arg]
                if isinstance(index, tuple):
                    if isinstance(index[1], str):

                        assert node[index[0]] == index[1], (
                            "at %s[%d], expected '%s' node; got '%s'"
                            % (node.kind, arg, index[1], node[index[0]].kind,)
                        )
                    else:
                        assert node[index[0]] in index[1], (
                            "at %s[%d], expected to be in '%s' node; got '%s'"
                            % (node.kind, arg, index[1], node[index[0]].kind,)
                        )

                    index = index[0]
                assert isinstance(index, int), (
                    "at %s[%d], %s should be int or tuple"
                    % (node.kind, arg, type(index),)
                )

                try:
                    node[index]
                except IndexError:
                    raise RuntimeError(
                        f"""
                        Expanding '{node.kind}' in template '{entry}[{arg}]':
                        {index} is invalid; has only {len(node)} entries
                        """
                    )
                self.preorder(node[index])

                arg += 1
            elif typ == "p":
                p = self.prec
                # entry[arg]
                tup = entry[arg]
                assert isinstance(tup, tuple)
                if len(tup) == 3:
                    (index, nonterm_name, self.prec) = tup
                    if isinstance(tup[1], str):
                        # if node[index] != nonterm_name:
                        #     from trepan.api import debug; debug()
                        assert node[index] == nonterm_name, (
                            "at %s[%d], expected '%s' node; got '%s'"
                            % (node.kind, arg, nonterm_name, node[index].kind,)
                        )
                    else:
                        assert (
                            node[tup[0]] in tup[1]
                        ), f"at {node.kind}[{tup[0]}], expected to be in '{tup[1]}' node; got '{node[tup[0]].kind}'"

                else:
                    assert len(tup) == 2
                    (index, self.prec) = entry[arg]

                self.preorder(node[index])
                self.prec = p
                arg += 1
            elif typ == "C":
                low, high, sep = entry[arg]
                remaining = len(node[low:high])
                for subnode in node[low:high]:
                    self.preorder(subnode)
                    remaining -= 1
                    if remaining > 0:
                        self.write(sep)
                        pass
                    pass
                arg += 1
            elif typ == "D":
                low, high, sep = entry[arg]
                remaining = len(node[low:high])
                for subnode in node[low:high]:
                    remaining -= 1
                    if len(subnode) > 0:
                        self.preorder(subnode)
                        if remaining > 0:
                            self.write(sep)
                            pass
                        pass
                    pass
                arg += 1
            elif typ == "x":
                # This code is only used in fragments
                assert isinstance(entry[arg], tuple)
                arg += 1
            elif typ == "P":
                p = self.prec
                low, high, sep, self.prec = entry[arg]
                remaining = len(node[low:high])
                # remaining = len(node[low:high])
                for subnode in node[low:high]:
                    self.preorder(subnode)
                    remaining -= 1
                    if remaining > 0:
                        self.write(sep)
                self.prec = p
                arg += 1
            elif typ == "{":
                expr = m.group("expr")

                # Line mapping stuff
                if (
                    hasattr(node, "linestart")
                    and node.linestart
                    and hasattr(node, "current_line_number")
                ):
                    self.source_linemap[self.current_line_number] = node.linestart

                if expr[0] == "%":
                    index = entry[arg]
                    self.template_engine((expr, index), node)
                    arg += 1
                else:
                    d = node.__dict__
                    try:
                        self.write(eval(expr, d, d))
                    except:
                        raise
            m = escape.search(fmt, i)
        self.write(fmt[i:])

    def default(self, node):
        mapping = self._get_mapping(node)
        table = mapping[0]
        key = node

        for i in mapping[1:]:
            key = key[i]
            pass

        if key.kind in table:
            self.template_engine(table[key.kind], node)
            self.prune()

    def customize(self, customize):
        """
        Special handling for opcodes, such as those that take a variable number
        of arguments -- we add a new entry for each in TABLE_R.
        """
        for k, v in list(customize.items()):
            if k in TABLE_R:
                continue
            op = k[: k.rfind("_")]

            if k.startswith("CALL_METHOD"):
                # This happens in PyPy and Python 3.7+
                TABLE_R[k] = ("%c(%P)", 0, (1, -1, ", ", 100))
            elif k.startswith("CALL_FUNCTION_KW"):
                TABLE_R[k] = ("%c(%P)", 0, (1, -1, ", ", 100))
            elif op == "CALL_FUNCTION":
                TABLE_R[k] = (
                    "%c(%P)",
                    (0, "expr"),
                    (1, -1, ", ", PRECEDENCE["yield"] - 1),
                )
            elif op in (
                "CALL_FUNCTION_VAR",
                "CALL_FUNCTION_VAR_KW",
                "CALL_FUNCTION_KW",
            ):

                # FIXME: handle everything in customize.
                # Right now, some of this is here, and some in that.

                if v == 0:
                    str = "%c(%C"  # '%C' is a dummy here ...
                    p2 = (0, 0, None)  # .. because of the None in this
                else:
                    str = "%c(%C, "
                    p2 = (1, -2, ", ")
                if op == "CALL_FUNCTION_VAR":
                    # Python 3.5 only puts optional args (the VAR part)
                    # lowest down the stack
                    if self.version == (3, 5):
                        if str == "%c(%C, ":
                            entry = ("%c(*%C, %c)", 0, p2, -2)
                        elif str == "%c(%C":
                            entry = ("%c(*%C)", 0, (1, 100, ""))
                    elif self.version == (3, 4):
                        # CALL_FUNCTION_VAR's top element of the stack contains
                        # the variable argument list
                        if v == 0:
                            str = "%c(*%c)"
                            entry = (str, 0, -2)
                        else:
                            str = "%c(%C, *%c)"
                            entry = (str, 0, p2, -2)
                    else:
                        str += "*%c)"
                        entry = (str, 0, p2, -2)
                elif op == "CALL_FUNCTION_KW":
                    str += "**%c)"
                    entry = (str, 0, p2, -2)
                elif op == "CALL_FUNCTION_VAR_KW":
                    str += "*%c, **%c)"
                    # Python 3.5 only puts optional args (the VAR part)
                    # lowest down the stack
                    na = v & 0xFF  # positional parameters
                    if self.version == (3, 5) and na == 0:
                        if p2[2]:
                            p2 = (2, -2, ", ")
                        entry = (str, 0, p2, 1, -2)
                    else:
                        if p2[2]:
                            p2 = (1, -3, ", ")
                        entry = (str, 0, p2, -3, -2)
                    pass
                else:
                    assert False, "Unhandled CALL_FUNCTION %s" % op

                TABLE_R[k] = entry
                pass
            # handled by n_dict:
            # if op == 'BUILD_SLICE':	TABLE_R[k] = ('%C'    ,    (0,-1,':'))
            # handled by n_list:
            # if   op == 'BUILD_LIST':	TABLE_R[k] = ('[%C]'  ,    (0,-1,', '))
            # elif op == 'BUILD_TUPLE':	TABLE_R[k] = ('(%C%,)',    (0,-1,', '))
            pass
        return

    def build_class(self, code):
        """Dump class definition, doc string and class body."""

        assert iscode(code)
        self.classes.append(self.currentclass)
        code = Code(code, self.scanner, self.currentclass)

        indent = self.indent
        # self.println(indent, '#flags:\t', int(code.co_flags))
        tree = self.build_ast(code._tokens, code._customize, code)

        # save memory by deleting no-longer-used structures
        code._tokens = None

        assert tree == "stmts"

        if tree[0] == "docstring":
            self.println(self.traverse(tree[0]))
            del tree[0]

        first_stmt = tree[0]
        try:
            if first_stmt == NAME_MODULE:
                if self.hide_internal:
                    del tree[0]
                    first_stmt = tree[0]
            pass
        except:
            pass

        have_qualname = False

        # Python 3.4+ has constants like 'cmp_to_key.<locals>.K'
        # which are not simple classes like the < 3 case.

        try:
            if (
                first_stmt == "assign"
                and first_stmt[0][0] == "LOAD_STR"
                and first_stmt[1] == "store"
                and first_stmt[1][0] == Token("STORE_NAME", pattr="__qualname__")
            ):
                have_qualname = True
        except:
            pass

        if have_qualname:
            if self.hide_internal:
                del tree[0]
            pass

        globals, nonlocals = find_globals_and_nonlocals(
            tree, set(), set(), code, self.version
        )
        # Add "global" declaration statements at the top
        # of the function
        for g in sorted(globals):
            self.println(indent, "global ", g)

        for nl in sorted(nonlocals):
            self.println(indent, "nonlocal ", nl)

        old_name = self.name
        self.gen_source(tree, code.co_name, code._customize)
        self.name = old_name

        # save memory by deleting no-longer-used structures
        code._tokens = None
        code._customize = None

        self.classes.pop(-1)

    def gen_source(
        self,
        tree,
        name,
        customize,
        is_lambda=False,
        returnNone=False,
        debug_opts=DEFAULT_DEBUG_OPTS,
    ):
        """convert parse tree to Python source code"""

        rn = self.return_none
        self.return_none = returnNone
        old_name = self.name
        self.name = name
        self.debug_opts = debug_opts
        # if code would be empty, append 'pass'
        if len(tree) == 0:
            self.println(self.indent, "pass")
        else:
            self.customize(customize)
            self.text = self.traverse(tree, is_lambda=is_lambda)
            self.println(self.text)
        self.name = old_name
        self.return_none = rn

    def build_ast(
        self,
        tokens,
        customize,
        code,
        is_lambda=False,
        noneInNames=False,
        isTopLevel=False,
    ):

        # FIXME: DRY with fragments.py

        # assert isinstance(tokens[0], Token)

        if is_lambda:
            for t in tokens:
                if t.kind == "RETURN_END_IF":
                    t.kind = "RETURN_END_IF_LAMBDA"
                elif t.kind == "RETURN_VALUE":
                    t.kind = "RETURN_VALUE_LAMBDA"
            tokens.append(Token("LAMBDA_MARKER"))
            try:
                if self.p_lambda is None:
                    self.p_lambda = get_python_parser(
                        self.version,
                        self.debug_parser,
                        compile_mode="lambda",
                        is_pypy=self.is_pypy,
                    )
                p = self.p_lambda
                p.insts = self.scanner.insts
                p.offset2inst_index = self.scanner.offset2inst_index
                ast = python_parser.parse(p, tokens, customize, is_lambda)
                self.customize(customize)

            except (heads.ParserError, AssertionError) as e:
                raise ParserError(e, tokens, self.p.debug["reduce"])
            transform_ast = self.treeTransform.transform(ast, code)
            self.maybe_show_tree(ast, phase="after")
            del ast  # Save memory
            return transform_ast

        # The bytecode for the end of the main routine has a
        # "return None". However you can't issue a "return" statement in
        # main. So as the old cigarette slogan goes: I'd rather switch (the token stream)
        # than fight (with the grammar to not emit "return None").
        if self.hide_internal:
            if len(tokens) >= 2 and not noneInNames:
                if tokens[-1].kind in ("RETURN_VALUE", "RETURN_VALUE_LAMBDA"):
                    # Python 3.4's classes can add a "return None" which is
                    # invalid syntax.
                    load_const = tokens[-2]
                    if load_const.kind == "LOAD_CONST":
                        if isTopLevel or load_const.pattr is None:
                            del tokens[-2:]
                        else:
                            tokens.append(Token("RETURN_LAST"))
                    else:
                        tokens.append(Token("RETURN_LAST"))
            if len(tokens) == 0:
                return PASS

        # Build a parse tree from a tokenized and massaged disassembly.
        try:
            # FIXME: have p.insts update in a better way
            # modularity is broken here
            p_insts = self.p.insts
            self.p.insts = self.scanner.insts
            self.p.offset2inst_index = self.scanner.offset2inst_index
            self.p.opc = self.scanner.opc
            ast = python_parser.parse(self.p, tokens, customize, is_lambda=is_lambda)

            self.p.insts = p_insts
        except (heads.ParserError, AssertionError) as e:
            # from trepan.api import debug; debug()
            raise ParserError(e, tokens, self.p.debug["reduce"])

        checker(ast, False, self.ast_errors)

        self.customize(customize)
        transform_ast = self.treeTransform.transform(ast, code)

        self.maybe_show_tree(ast, phase="after")

        del ast  # Save memory
        return transform_ast

    @classmethod
    def _get_mapping(cls, node):
        return MAP.get(node, MAP_DIRECT)


def code_deparse(
    co,
    out=sys.stdout,
    version=None,
    debug_opts=DEFAULT_DEBUG_OPTS,
    code_objects={},
    compile_mode="exec",
    is_pypy=IS_PYPY,
    walker=SourceWalker,
):
    """
    ingests and deparses a given code block 'co'. If version is None,
    we will use the current Python interpreter version.
    """

    assert iscode(co)

    if version is None:
        version = PYTHON_VERSION_TRIPLE

    # store final output stream for case of error
    scanner = get_scanner(version, is_pypy=is_pypy, show_asm=debug_opts["asm"])

    tokens, customize = scanner.ingest(
        co, code_objects=code_objects, show_asm=debug_opts["asm"]
    )

    debug_parser = debug_opts.get("grammar", dict(PARSER_DEFAULT_DEBUG))

    #  Build Syntax Tree from disassembly.
    linestarts = dict(scanner.opc.findlinestarts(co))
    deparsed = walker(
        version,
        out,
        scanner,
        showast=debug_opts.get("tree", TREE_DEFAULT_DEBUG),
        debug_parser=debug_parser,
        compile_mode=compile_mode,
        is_pypy=is_pypy,
        linestarts=linestarts,
    )

    isTopLevel = co.co_name == "<module>"
    if compile_mode == "eval":
        deparsed.hide_internal = False
    deparsed.compile_mode = compile_mode
    deparsed.ast = deparsed.build_ast(
        tokens,
        customize,
        co,
        is_lambda=is_lambda_mode(compile_mode),
        isTopLevel=isTopLevel,
    )

    #### XXX workaround for profiling
    if deparsed.ast is None:
        return None

    # FIXME use a lookup table here.
    if is_lambda_mode(compile_mode):
        expected_start = "lambda_start"
    elif compile_mode == "eval":
        expected_start = "expr_start"
    elif compile_mode == "expr":
        expected_start = "expr_start"
    elif compile_mode == "exec":
        expected_start = "stmts"
    elif compile_mode == "single":
        expected_start = "single_start"
    else:
        expected_start = None

    if expected_start:
        assert (
            deparsed.ast == expected_start
        ), f"Should have parsed grammar start to '{expected_start}'; got: {deparsed.ast.kind}"
    # save memory
    del tokens

    deparsed.mod_globs, nonlocals = find_globals_and_nonlocals(
        deparsed.ast, set(), set(), co, version
    )

    if compile_mode not in ("lambda", "listcomp"):
        assert not nonlocals

    deparsed.FUTURE_UNICODE_LITERALS = (
        COMPILER_FLAG_BIT["FUTURE_UNICODE_LITERALS"] & co.co_flags != 0
    )

    # What we've been waiting for: Generate source from Syntax Tree!
    deparsed.gen_source(
        deparsed.ast,
        name=co.co_name,
        customize=customize,
        is_lambda=is_lambda_mode(compile_mode),
        debug_opts=debug_opts,
    )

    for g in sorted(deparsed.mod_globs):
        deparsed.write("# global %s ## Warning: Unused global\n" % g)

    if deparsed.ast_errors:
        deparsed.write("# NOTE: have internal decompilation grammar errors.\n")
        deparsed.write("# Use -T option to show full context.")
        for err in deparsed.ast_errors:
            deparsed.write(err)
        raise SourceWalkerError("Deparsing hit an internal grammar-rule bug")

    if deparsed.ERROR:
        raise SourceWalkerError("Deparsing stopped due to parse error")
    return deparsed


def deparse_code2str(
    code,
    out=sys.stdout,
    version=None,
    debug_opts=DEFAULT_DEBUG_OPTS,
    code_objects={},
    compile_mode="exec",
    is_pypy=IS_PYPY,
    walker=SourceWalker,
):
    """Return the deparsed text for a Python code object. `out` is where any intermediate
    output for assembly or tree output will be sent.
    """
    return code_deparse(
        code,
        out,
        version,
        debug_opts,
        code_objects=code_objects,
        compile_mode=compile_mode,
        is_pypy=is_pypy,
        walker=walker,
    ).text


if __name__ == "__main__":

    def deparse_test(co):
        "This is a docstring"
        s = deparse_code2str(co, debug_opts={"asm": "after", "tree": True})
        # s = deparse_code2str(co, showasm=None, showast=False,
        #                       showgrammar=True)
        print(s)
        return

    deparse_test(deparse_test.__code__)
