from io import StringIO
from token import NAME, NUMBER, NEWLINE, ENDMARKER
from tokenize import generate_tokens

from story6.tokenizer import Tokenizer
from story6.parser import Parser
from story6.grammar import Alt, GrammarParser, Rule

def test_grammar():
    program = ("stmt: asmt | expr\n"
               "asmt: NAME '=' expr\n"
               "expr: NAME\n")
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    rules = list(p.grammar().rules.values())
    assert rules == [Rule('stmt', [Alt(['asmt']), Alt(['expr'])]),
                     Rule('asmt', [Alt(['NAME', "'='", 'expr'])]),
                     Rule('expr', [Alt(['NAME'])])]

def test_failure():
    program = ("stmt: asmt | expr\n"
               "asmt: NAME '=' expr 42\n"
               "expr: NAME\n")
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    grammar = p.grammar()
    assert grammar is None

def test_action():
    program = "start: NAME { foo + bar } | NUMBER { -baz }\n"
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    rules = list(p.grammar().rules.values())
    assert rules == [Rule("start", [Alt(["NAME"], "foo + bar"),
                                    Alt(["NUMBER"], "- baz")])]
    assert rules != [Rule("start", [Alt(["NAME"], "foo + bar"),
                                    Alt(["NUMBER"], "baz")])]

def test_action_repr_str():
    alt = Alt(["one", "two"])
    assert repr(alt) == "Alt(['one', 'two'])"
    assert str(alt) == "one two"

    alt = Alt(["one", "two"], "foo + bar")
    assert repr(alt) == "Alt(['one', 'two'], 'foo + bar')"
    assert str(alt) == "one two { foo + bar }"

def test_indents():
    program = ("stmt: foo | bar\n"
               "    | baz\n"
               "    | booh | bah\n")
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    rules = list(p.grammar().rules.values())
    assert rules == [Rule('stmt',
                          [Alt(['foo']), Alt(['bar']),
                           Alt(['baz']),
                           Alt(['booh']), Alt(['bah'])])]

def test_indents2():
    program = ("stmt:\n"
               "    | foo | bar\n"
               "    | baz\n"
               "    | booh | bah\n"
               "foo: bar\n")
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    rules = list(p.grammar().rules.values())
    assert rules == [Rule('stmt',
                          [Alt(['foo']), Alt(['bar']),
                           Alt(['baz']),
                           Alt(['booh']), Alt(['bah'])]),
                     Rule('foo', [Alt(['bar'])])]

def test_meta():
    program = ("@start 'start'\n"
               "@foo bar\n"
               "@bar\n"
               "stmt: foo\n")
    file = StringIO(program)
    tokengen = generate_tokens(file.readline)
    tok = Tokenizer(tokengen)
    p = GrammarParser(tok)
    grammar = p.grammar()
    assert grammar
    assert grammar.rules == {'stmt': Rule('stmt', [Alt(["foo"])])}
    assert grammar.metas == {'start': 'start',
                             'foo': 'bar',
                             'bar': None}