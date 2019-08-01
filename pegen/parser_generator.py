from __future__ import annotations  # Requires Python 3.7 or later

import contextlib
from typing import *

from pegen import sccutils
from pegen.grammar import Rule
from pegen.grammar import Rhs
from pegen.grammar import Alt
from pegen.grammar import NamedItem
from pegen.grammar import Plain

MODULE_PREFIX = """\
#!/usr/bin/env python3.8
# @generated by pegen.py from {filename}
from __future__ import annotations

import ast
import sys
import tokenize

from pegen import memoize, memoize_left_rec, Parser

"""
MODULE_SUFFIX = """

if __name__ == '__main__':
    from pegen import simple_parser_main
    simple_parser_main(GeneratedParser)
"""
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
    return run_parser(filename, (void *)start_rule, %(mode)s);
}

static PyMethodDef ParseMethods[] = {
    {"parse",  parse_file, METH_VARARGS, "Parse a file."},
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


class ParserGenerator:

    def __init__(self, rules: Dict[str, Rule], file: Optional[IO[Text]]):
        self.rules = rules
        self.file = file
        self.level = 0
        compute_nullables(rules)
        self.first_graph, self.first_sccs = compute_left_recursives(self.rules)
        self.todo = self.rules.copy()  # Rules to generate
        self.counter = 0  # For name_rule()/name_loop()

    @contextlib.contextmanager
    def indent(self) -> Iterator[None]:
        self.level += 1
        try:
            yield
        finally:
            self.level -= 1

    def print(self, *args):
        if not args:
            print(file=self.file)
        else:
            print("    " * self.level, end="", file=self.file)
            print(*args, file=self.file)

    def printblock(self, lines):
        for line in lines.splitlines():
            self.print(line)

    def generate_python_module(self, filename: str) -> None:
        self.print(MODULE_PREFIX.format(filename=filename))
        self.print("class GeneratedParser(Parser):")
        while self.todo:
            for rulename, rule in list(self.todo.items()):
                del self.todo[rulename]
                self.print()
                with self.indent():
                    rule.pgen_func(self)
        self.print(MODULE_SUFFIX.rstrip('\n'))

    def generate_cpython_extension(self, filename: str) -> None:
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
                rule.cgen_func(self)
        mode = int(self.rules['start'].type == 'mod_ty')
        self.print(EXTENSION_SUFFIX.rstrip('\n') % dict(mode=mode))

    def collect_todo(self) -> None:
        done = set()  # type: Set[str]
        while True:
            alltodo = set(self.todo)
            todo = alltodo - done
            if not todo:
                break
            for rulename in todo:
                self.todo[rulename].collect_todo(self)
            done = alltodo

    def name_node(self, rhs: Rhs) -> str:
        self.counter += 1
        name = f'_tmp_{self.counter}'  # TODO: Pick a nicer name.
        self.todo[name] = Rule(name, None, rhs)
        return name

    def name_loop(self, node: Plain, is_repeat1: bool) -> str:
        self.counter += 1
        if is_repeat1:
            prefix = '_loop1_'
        else:
            prefix = '_loop0_'
        name = f'{prefix}{self.counter}'  # TODO: It's ugly to signal via the name.
        self.todo[name] = Rule(name, None, Rhs([Alt([NamedItem(None, node)])]))
        return name


def compute_nullables(rules: Dict[str, Rule]) -> None:
    """Compute which rules in a grammar are nullable.

    Thanks to TatSu (tatsu/leftrec.py) for inspiration.
    """
    for rule in rules.values():
        rule.visit(rules)


def compute_left_recursives(rules: Dict[str, Rule]) -> Tuple[Dict[str, AbstractSet[str]], List[AbstractSet[str]]]:
    graph = make_first_graph(rules)
    sccs = list(sccutils.strongly_connected_components(graph.keys(), graph))
    for scc in sccs:
        if len(scc) > 1:
            for name in scc:
                rules[name].left_recursive = True
            # Try to find a leader such that all cycles go through it.
            leaders = set(scc)
            for start in scc:
                for cycle in sccutils.find_cycles_in_scc(graph, scc, start):
                    ## print("Cycle:", " -> ".join(cycle))
                    leaders -= (scc - set(cycle))
                    if not leaders:
                        raise ValueError(
                            f"SCC {scc} has no leadership candidate (no element is included in all cycles)")
            ## print("Leaders:", leaders)
            leader = min(leaders)  # Pick an arbitrary leader from the candidates.
            rules[leader].leader = True
        else:
            name = min(scc)  # The only element.
            if name in graph[name]:
                rules[name].left_recursive = True
                rules[name].leader = True
    return graph, sccs


def make_first_graph(rules: Dict[str, Rule]) -> Dict[str, AbstractSet[str]]:
    """Compute the graph of left-invocations.

    There's an edge from A to B if A may invoke B at its initial
    position.

    Note that this requires the nullable flags to have been computed.
    """
    graph = {}
    vertices: Set[str] = set()
    for rulename, rhs in rules.items():
        graph[rulename] = names = rhs.initial_names()
        vertices |= names
    for vertex in vertices:
        graph.setdefault(vertex, set())
    return graph
