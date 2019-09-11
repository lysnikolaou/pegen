import ast
import itertools
import re
from typing import Any, Optional, IO, Text, List, Dict, Tuple

from pegen.grammar import GrammarVisitor
from pegen import grammar
from pegen.parser_generator import dedupe, ParserGenerator
from pegen.tokenizer import exact_token_types

EXTENSION_PREFIX = """\
// @generated by pegen.py from {filename}

#include "pegen.h"
"""
EXTENSION_SUFFIX = """
// TODO: Allow specifying a module name

static PyObject *
parse_file(PyObject *self, PyObject *args)
{
    const char *filename;

    if (!PyArg_ParseTuple(args, "s", &filename))
        return NULL;
    return run_parser_from_file(filename, (void *)start_rule, %(mode)s);
}

static PyObject *
parse_string(PyObject *self, PyObject *args)
{
    const char *the_string;

    if (!PyArg_ParseTuple(args, "s", &the_string))
        return NULL;
    return run_parser_from_string(the_string, (void *)start_rule, %(mode)s);
}

static PyMethodDef ParseMethods[] = {
    {"parse_file",  parse_file, METH_VARARGS, "Parse a file."},
    {"parse_string",  parse_string, METH_VARARGS, "Parse a string."},
    {NULL, NULL, 0, NULL}        /* Sentinel */
};

static struct PyModuleDef parsemodule = {
    PyModuleDef_HEAD_INIT,
    .m_name = "parse",
    .m_doc = "A parser.",
    .m_methods = ParseMethods,
};

PyMODINIT_FUNC
PyInit_parse(void)
{
    PyObject *m = PyModule_Create(&parsemodule);
    if (m == NULL)
        return NULL;

    return m;
}

// The end
"""


class CCallMakerVisitor(GrammarVisitor):

    def __init__(self, parser_generator: ParserGenerator):
        self.gen = parser_generator
        self.cache: Dict[Any, Any] = {}

    def visit_NameLeaf(self, node):
        name = node.value
        if name in ('NAME', 'NUMBER', 'STRING', 'CUT', 'CURLY_STUFF'):
            name = name.lower()
            return f"{name}_var", f"{name}_token(p)"
        if name in ('NEWLINE', 'DEDENT', 'INDENT', 'ENDMARKER', 'ASYNC', 'AWAIT'):
            name = name.lower()
            return f"{name}_var", f"{name}_token(p)"
        return f"{name}_var", f"{name}_rule(p)"

    def visit_StringLeaf(self, node):
        val = ast.literal_eval(node.value)
        if re.match(r'[a-zA-Z_]\w*\Z', val):
            return 'keyword', f'keyword_token(p, "{val}")'
        else:
            assert val in exact_token_types, f"{node.value} is not a known literal"
            type = exact_token_types[val]
            return 'literal', f'expect_token(p, {type})'

    def visit_Rhs(self, node):
        if node in self.cache:
            return self.cache[node]
        if len(node.alts) == 1 and len(node.alts[0].items) == 1:
            self.cache[node] = self.visit(node.alts[0].items[0])
        else:
            name = self.gen.name_node(node)
            self.cache[node] = f"{name}_var", f"{name}_rule(p)"
        return self.cache[node]

    def visit_NamedItem(self, node):
        name, call = self.visit(node.item)
        if node.name:
            name = node.name
        return name, call

    def lookahead_call_helper(self, node, positive):
        name, call = self.visit(node.node)
        func, args = call.split('(', 1)
        assert args[-1] == ')'
        args = args[:-1]
        if not args.startswith("p,"):
            return None, f"lookahead({positive}, {func}, {args})"
        elif args[2:].strip().isalnum():
            return None, f"lookahead_with_int({positive}, {func}, {args})"
        else:
            return None, f"lookahead_with_string({positive}, {func}, {args})"

    def visit_PositiveLookahead(self, node):
        return self.lookahead_call_helper(node, 1)

    def visit_NegativeLookahead(self, node):
        return self.lookahead_call_helper(node, 0)

    def visit_Opt(self, node):
        name, call = self.visit(node.node)
        return "opt_var", f"{call}, 1"  # Using comma operator!

    def visit_Repeat0(self, node):
        if node in self.cache:
            return self.cache[node]
        name = self.gen.name_loop(node.node, False)
        self.cache[node] = f"{name}_var", f"{name}_rule(p)"
        return self.cache[node]

    def visit_Repeat1(self, node):
        if node in self.cache:
            return self.cache[node]
        name = self.gen.name_loop(node.node, True)
        self.cache[node] = f"{name}_var", f"{name}_rule(p)"  # But not here!
        return self.cache[node]

    def visit_Group(self, node):
        return self.visit(node.rhs)


class CParserGenerator(ParserGenerator, GrammarVisitor):

    def __init__(self, rules: Dict[str, grammar.Rule], file: Optional[IO[Text]]):
        super().__init__(rules, file)
        self.callmakervisitor = CCallMakerVisitor(self)
        self._varname_counter = 0

    def unique_varname(self, name="tmpvar"):
        new_var = name + "_" + str(self._varname_counter)
        self._varname_counter += 1
        return new_var

    def call_with_errorcheck_return(self, call_text, returnval):
        error_var = self.unique_varname()
        self.print(f"int {error_var} = {call_text};")
        self.print(f"if ({error_var}) {{")
        with self.indent():
            self.print(f"return {returnval};")
        self.print(f"}}")

    def call_with_errorcheck_goto(self, call_text, goto_target):
        error_var = self.unique_varname()
        self.print(f"int {error_var} = {call_text};")
        self.print(f"if ({error_var}) {{")
        with self.indent():
            self.print(f"goto {goto_target};")
        self.print(f"}}")

    def out_of_memory_return(self, expr, returnval, message="Parser out of memory"):
        self.print(f"if ({expr}) {{")
        with self.indent():
            self.print(f'PyErr_Format(PyExc_MemoryError, "{message}");')
            self.print(f"return {returnval};")
        self.print(f"}}")

    def out_of_memory_goto(self, expr, goto_target, message="Parser out of memory"):
        self.print(f"if ({expr}) {{")
        with self.indent():
            self.print(f'PyErr_Format(PyExc_MemoryError, "{message}");')
            self.print(f"goto {goto_target};")
        self.print(f"}}")

    def generate(self, filename: str) -> None:
        self.collect_todo()
        self.print(EXTENSION_PREFIX.format(filename=filename))
        for i, rulename in enumerate(self.todo, 1000):
            self.print(f"#define {rulename}_type {i}")
        self.print()
        for rulename, rule in self.todo.items():
            if rule.is_loop():
                type = 'asdl_seq *'
            elif rule.type:
                type = rule.type + ' '
            else:
                type = 'void *'
            self.print(f"static {type}{rulename}_rule(Parser *p);")
        self.print()
        while self.todo:
            for rulename, rule in list(self.todo.items()):
                del self.todo[rulename]
                self.print()
                self.visit(rule)
        mode = int(self.rules['start'].type == 'mod_ty')
        self.print(EXTENSION_SUFFIX.rstrip('\n') % dict(mode=mode))

    def visit_Rule(self, node):
        is_loop = node.is_loop()
        is_repeat1 = node.name.startswith('_loop1')
        memoize = not node.leader
        rhs = node.flatten()
        if is_loop:
            type = 'asdl_seq *'
        elif node.type:
            type = node.type
        else:
            type = 'void *'

        self.print(f"// {node}")
        if node.left_recursive:
            self.print(f"static {type} {node.name}_raw(Parser *);")

        self.print(f"static {type}")
        self.print(f"{node.name}_rule(Parser *p)")

        if node.left_recursive:
            self.print("{")
            with self.indent():
                self.print(f"{type} res = NULL;")
                self.print(f"if (is_memoized(p, {node.name}_type, &res))")
                with self.indent():
                    self.print("return res;")
                self.print("int mark = p->mark;")
                self.print("int resmark = p->mark;")
                self.print("while (1) {")
                with self.indent():
                    self.call_with_errorcheck_return(
                            f"update_memo(p, mark, {node.name}_type, res)", "res")
                    self.print("p->mark = mark;")
                    self.print(f"void *raw = {node.name}_raw(p);")
                    self.print("if (raw == NULL || p->mark <= resmark)")
                    with self.indent():
                        self.print("break;")
                    self.print("resmark = p->mark;")
                    self.print("res = raw;")
                self.print("}")
                self.print("p->mark = resmark;")
                self.print("return res;")
            self.print("}")
            self.print(f"static {type}")
            self.print(f"{node.name}_raw(Parser *p)")

        self.print("{")
        with self.indent():
            if is_loop:
                self.print(f"void *res = NULL;")
            else:
                self.print(f"{type} res = NULL;")
            if memoize:
                self.print(f"if (is_memoized(p, {node.name}_type, &res))")
                with self.indent():
                    self.print("return res;")
            self.print("int mark = p->mark;")
            if is_loop:
                self.print("void **children = PyMem_Malloc(0);")
                self.out_of_memory_return(f'!children', "NULL")
                self.print("ssize_t n = 0;")
            self.visit(rhs, is_loop=is_loop, rulename=node.name if memoize else None)
            if is_loop:
                if is_repeat1:
                    self.print("if (n == 0) {")
                    with self.indent():
                        self.print("PyMem_Free(children);")
                        self.print("return NULL;")
                    self.print("}")
                self.print("asdl_seq *seq = _Py_asdl_seq_new(n, p->arena);")
                self.out_of_memory_return(f'!seq', "NULL", message=f'asdl_seq_new {node.name}')
                self.print("for (int i = 0; i < n; i++) asdl_seq_SET(seq, i, children[i]);")
                self.print("PyMem_Free(children);")
                if node.name:
                    self.print(f"insert_memo(p, mark, {node.name}_type, seq);")
                self.print("return seq;")
            else:
                ## gen.print(f'fprintf(stderr, "Fail at %d: {self.name}\\n", p->mark);')
                self.print("res = NULL;")
        if not is_loop:
            self.print("  done:")
            with self.indent():
                if memoize:
                    self.print(f"insert_memo(p, mark, {node.name}_type, res);")
                self.print("return res;")
        self.print("}")

    def visit_NamedItem(self, node, names: List[str]):
        name, call = self.callmakervisitor.visit(node)
        if not name:
            self.print(call)
        else:
            if name != 'cut':
                name = dedupe(name, names)
            self.print(f"({name} = {call})")

    def visit_Rhs(self, node, is_loop: bool, rulename: Optional[str]):
        if is_loop:
            assert len(node.alts) == 1
        vars = {}
        for alt in node.alts:
            vars.update(self.collect_vars(alt))
        for v, type in sorted(item for item in vars.items() if item[0] is not None):
            if not type:
                type = 'void *'
            else:
                type += ' '
            self.print(f"{type}{v};")
        for alt in node.alts:
            self.visit(alt, is_loop=is_loop, rulename=rulename)

    def visit_Alt(self, node, is_loop: bool, rulename: Optional[str]):
        self.print(f"// {node}")
        names: List[str] = []
        if is_loop:
            self.print("while (")
        else:
            self.print("if (")
        with self.indent():
            first = True
            for item in node.items:
                if first:
                    first = False
                else:
                    self.print("&&")
                self.visit(item, names=names)
        self.print(") {")
        with self.indent():
            action = node.action
            if not action:
                ## self.print(f'fprintf(stderr, "Hit at %d: {node}, {names}\\n", p->mark);')
                if len(names) > 1:
                    self.print(f"res = CONSTRUCTOR(p, {', '.join(names)});")
                else:
                    self.print(f"res = {names[0]};")
            else:
                assert action[0] == '{' and action[-1] == '}', repr(action)
                action = action[1:-1].strip()
                self.print(f"res = {action};")
                ## self.print(f'fprintf(stderr, "Hit with action at %d: {node}, {names}, {action}\\n", p->mark);')
            if is_loop:
                self.print("children = PyMem_Realloc(children, (n+1)*sizeof(void *));")
                self.out_of_memory_return(f'!children', "NULL", message=f'realloc {rulename}')
                self.print(f"children[n++] = res;")
                self.print("mark = p->mark;")
            else:
                if rulename:
                    self.print(f"insert_memo(p, mark, {rulename}_type, res);")
                self.print(f"goto done;")
        self.print("}")
        self.print("p->mark = mark;")
        if "cut_var" in names:
            self.print("if (cut_var) return NULL;")

    def collect_vars(self, node) -> Dict[str, Optional[str]]:
        names: List[str] = []
        types = {}
        for item in node.items:
            name, type = self.add_var(item, names)
            types[name] = type
        return types

    def add_var(self, node, names: List[str]) -> Tuple[str, Optional[str]]:
        name: str
        call: str
        name, call = self.callmakervisitor.visit(node.item)
        type = None
        if not name:
            return name, type
        if name.startswith('cut'):
            return name, 'int'
        if name.endswith('_var'):
            rulename = name[:-4]
            rule = self.rules.get(rulename)
            if rule is not None:
                if rule.is_loop():
                    type = 'asdl_seq *'
                else:
                    type = rule.type
            elif name.startswith('_loop'):
                type = 'asdl_seq *'
        if node.name:
            name = node.name
        name = dedupe(name, names)
        return name, type
