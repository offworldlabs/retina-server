"""Find every environment key the backend's own code reads, by reading its source.

A read is `.get`, `.setdefault` or `.pop` on the environment, a subscript of it,
`os.getenv`, or `key in` it. The environment is `os.environ` or a copy of it,
under whatever name the module imports or assigns it, and anything that holds
it: a local assigned from it, or a parameter some call passes it to, as a
function taking an `env` mapping does. Only those shapes are followed; a read
through another one goes unseen, so test_env_example.py pins the shapes the
backend uses and a new one wants adding there.

The key is resolved statically: a string literal, a constant (a local or a
module's, imported ones included), an f-string over names that resolve, a loop
or comprehension over a literal collection, or a parameter of a helper, which
resolves to the arguments at every call of it (with anything it is reassigned
to). A key none of those reach is reported rather than skipped, and so is a
helper passed around as a value, whose calls cannot be found. A call counts as a
helper's only where its name can mean that helper: in the helper's module, or
one importing it or its module; a method's, only through its own class. It is a
check on how this backend reads its environment, not a proof against code
written to slip past it.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

ENVIRON_METHODS = {"get", "setdefault", "pop"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class Unresolved(Exception):
    pass


@dataclass
class Module:
    name: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    os_names: set[str] = field(default_factory=set)
    environ_names: set[str] = field(default_factory=set)
    getenv_names: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os" or (alias.name.startswith("os.") and not alias.asname):
                        self.os_names.add(alias.asname or "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                self.environ_names |= {a.asname or a.name for a in node.names if a.name == "environ"}
                self.getenv_names |= {a.asname or a.name for a in node.names if a.name == "getenv"}
        # Module-level aliases, such as `ENV = os.environ` or `_get = os.getenv`,
        # and aliases of those, until a pass learns nothing new.
        learning = True
        while learning:
            learning = False
            for statement in _in_scope(self.tree):
                for target, value in _assignments(statement):
                    if target not in self.environ_names and self.holds_environ(value):
                        self.environ_names.add(target)
                        learning = True
                    elif target not in self.getenv_names and self.is_getenv(value):
                        self.getenv_names.add(target)
                        learning = True

    def is_getenv(self, expr: ast.expr) -> bool:
        return _attribute_of(expr, self.os_names, "getenv") or (
            isinstance(expr, ast.Name) and expr.id in self.getenv_names
        )

    def holds_environ(self, expr: ast.expr) -> bool:
        """os.environ itself, a copy of it or a mapping merged from it, by this module's names."""
        if isinstance(expr, ast.Name):
            return expr.id in self.environ_names
        if _attribute_of(expr, self.os_names, "environ"):
            return True
        if isinstance(expr, ast.IfExp):
            return self.holds_environ(expr.body) or self.holds_environ(expr.orelse)
        if isinstance(expr, ast.BoolOp):
            return any(self.holds_environ(v) for v in expr.values)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self.holds_environ(expr.left) or self.holds_environ(expr.right)
        if isinstance(expr, ast.Dict):
            return any(k is None and self.holds_environ(v) for k, v in zip(expr.keys, expr.values, strict=True))
        if isinstance(expr, ast.Call) and len(expr.args) <= 1:
            func = expr.func
            if isinstance(func, ast.Attribute) and func.attr == "copy":
                return self.holds_environ(expr.args[0] if expr.args else func.value)
            if isinstance(func, ast.Name) and func.id == "dict" and expr.args:
                return self.holds_environ(expr.args[0])
        return False

    def enclosing(self, node: ast.AST, kinds) -> ast.AST | None:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, kinds):
                return node
        return None


@dataclass(frozen=True)
class Read:
    module: str
    line: int
    keys: frozenset[str] = frozenset()
    problem: str = ""


def load(root: Path, excluded: set[str]) -> dict[str, Module]:
    """Every module under root, by dotted name, skipping hidden and excluded top-level directories."""
    modules = {}
    for directory, subdirs, files in os.walk(root):
        rel_dir = Path(directory).relative_to(root)
        subdirs[:] = [d for d in subdirs if not d.startswith((".", "__")) and (rel_dir / d).parts[0] not in excluded]
        for file in files:
            if file.endswith(".py"):
                rel = rel_dir / file
                name = ".".join(rel.with_suffix("").parts)
                modules[name] = Module(name, ast.parse((root / rel).read_text(), str(rel)))
    return modules


def _attribute_of(expr: ast.expr, owners: set[str], attr: str) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == attr
        and isinstance(expr.value, ast.Name)
        and expr.value.id in owners
    )


def _assignments(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """(name, value) for each plain name a statement or walrus binds from a value."""
    if isinstance(node, ast.Assign):
        return [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [(node.target.id, node.value)]
    if isinstance(node, ast.NamedExpr):
        return [(node.target.id, node.value)]
    return []


def _in_scope(scope: ast.AST):
    """Every node of a scope, not descending into the functions, classes and
    lambdas nested in it, which are scopes of their own."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _bound(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [e.id if isinstance(e, ast.Name) else "" for e in target.elts]
    return []


def _called(call: ast.Call) -> str | None:
    """The name a call is made by, `f` for both `f()` and `x.f()`."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _argument(function: ast.FunctionDef, call: ast.Call, param: str) -> ast.expr | None:
    """What a call passes for one of the function's parameters, if it can be told."""
    passed = next((k.value for k in call.keywords if k.arg == param), None)
    if passed is not None:
        return passed
    positional = [a.arg for a in function.args.posonlyargs + function.args.args]
    if param not in positional:
        return None
    index = positional.index(param)
    if positional and positional[0] in {"self", "cls"} and isinstance(call.func, ast.Attribute):
        index -= 1
    if any(isinstance(a, ast.Starred) for a in call.args[: index + 1]):
        raise Unresolved(f"{function.name}(*...)")
    return call.args[index] if 0 <= index < len(call.args) else None


def _default(function: ast.FunctionDef, param: str) -> ast.expr | None:
    positional = [a.arg for a in function.args.posonlyargs + function.args.args]
    if param in positional:
        offset = len(positional) - len(function.args.defaults)
        index = positional.index(param)
        return function.args.defaults[index - offset] if index >= offset else None
    for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
        if arg.arg == param:
            return default
    return None


def _parameters(function: ast.FunctionDef | ast.Lambda) -> set[str]:
    args = function.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}


class Resolver:
    def __init__(self, modules: dict[str, Module]):
        self.modules = modules
        self.calls: dict[str, list[tuple[Module, ast.Call]]] = {}
        self.as_values: dict[str, list[tuple[Module, ast.AST]]] = {}
        for module in modules.values():
            for node in ast.walk(module.tree):
                if isinstance(node, ast.Call) and (name := _called(node)):
                    self.calls.setdefault(name, []).append((module, node))
                elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                    parent = module.parents.get(node)
                    if not (isinstance(parent, ast.Call) and parent.func is node):
                        name = node.id if isinstance(node, ast.Name) else node.attr
                        self.as_values.setdefault(name, []).append((module, node))
        self.aliases: dict[str, set[str]] = {}
        for module in modules.values():
            for node in ast.walk(module.tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.asname:
                            self.aliases.setdefault(alias.name, set()).add(alias.asname)
        self._locals: dict[ast.AST, dict[str, list[ast.expr]]] = {}
        self._environ_params: dict[tuple[str, int, str], bool] = {}
        self._cycle = False

    @staticmethod
    def _from(module: Module, statement: ast.ImportFrom) -> str:
        """The absolute dotted name a from-import reads from."""
        if not statement.level:
            return statement.module or ""
        package = module.name.split(".")[: -statement.level]
        return ".".join(package + ([statement.module] if statement.module else []))

    def imported(self, module: Module, statement: ast.ImportFrom) -> Module | None:
        """The module a from-import names, relative ones and packages included, if it is one of ours."""
        name = self._from(module, statement)
        return self.modules.get(name) or self.modules.get(f"{name}.__init__")

    def _origin(self, caller: Module, local: str) -> tuple[Module, str] | None:
        """The module and name a name bound by one of caller's from-imports comes
        from, followed through re-exports."""
        module, name = caller, local
        for _ in range(8):
            found = None
            for statement in ast.walk(module.tree):
                if isinstance(statement, ast.ImportFrom) and (source := self.imported(module, statement)):
                    for alias in statement.names:
                        if (alias.asname or alias.name) == name:
                            found = (source, alias.name)
            if found is None:
                return (module, name) if module is not caller else None
            module, name = found
        return None

    def _is(self, caller: Module, node: ast.expr, home: Module, name: str) -> bool:
        """Whether a bare name in caller is home's `name`."""
        if not isinstance(node, ast.Name):
            return False
        if caller is home:
            return node.id == name
        return self._origin(caller, node.id) == (home, name)

    def _may_mean(self, home: Module, function: ast.AST, caller: Module, node: ast.AST) -> bool:
        """Whether a call or reference in caller can be to home's function."""
        if isinstance(node, ast.Call):
            node = node.func
        cls = home.parents.get(function)
        if isinstance(cls, ast.ClassDef):
            # A method, through its class or an instance made from it here.
            if not isinstance(node, ast.Attribute) or node.attr != function.name:
                return False
            receiver = node.value.func if isinstance(node.value, ast.Call) else node.value
            if isinstance(receiver, ast.Name) and receiver.id in {"self", "cls"}:
                return caller is home and caller.enclosing(node, ast.ClassDef) is cls
            return self._is(caller, receiver, home, cls.name)
        if isinstance(node, ast.Name):
            return self._is(caller, node, home, function.name)
        if isinstance(node, ast.Attribute) and node.attr == function.name:
            return self._names_module(caller, node.value, home)
        return False

    def _names_module(self, caller: Module, expr: ast.expr, home: Module) -> bool:
        """Whether expr, `mod` or `pkg.mod`, is home as caller imported it."""
        dotted, target = ast.unparse(expr), home.name.removesuffix(".__init__")
        for s in ast.walk(caller.tree):
            if isinstance(s, ast.Import):
                if any(a.name == target and (a.asname or a.name) == dotted for a in s.names):
                    return True
            elif isinstance(s, ast.ImportFrom):
                base = self._from(caller, s)
                if any(f"{base}.{a.name}" == target and (a.asname or a.name) == dotted for a in s.names):
                    return True
        return False

    def _calls_of(self, module: Module, function: ast.AST) -> list[tuple[Module, ast.Call]]:
        names = {function.name} | self.aliases.get(function.name, set())
        candidates = [pair for name in names for pair in self.calls.get(name, [])]
        return [(c, call) for c, call in candidates if self._may_mean(module, function, c, call)]

    def reads(self) -> list[Read]:
        found = []
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                key = self._key_expr(module, node)
                if key is None:
                    continue
                try:
                    found.append(Read(module.name, node.lineno, frozenset(self.resolve(module, key, frozenset()))))
                except Unresolved as exc:
                    found.append(Read(module.name, node.lineno, problem=str(exc)))
        return found

    def _local_values(self, function: ast.AST) -> dict[str, list[ast.expr]]:
        if function not in self._locals:
            values: dict[str, list[ast.expr]] = {}
            for node in _in_scope(function):
                for name, value in _assignments(node):
                    values.setdefault(name, []).append(value)
            self._locals[function] = values
        return self._locals[function]

    # ── what reads the environment ──
    def _key_expr(self, module: Module, node: ast.AST) -> ast.expr | None:
        if isinstance(node, ast.Call):
            func = node.func
            if module.is_getenv(func) or (isinstance(func, ast.Name) and self._local_getenv(module, func)):
                return self._first_argument(node)
            if isinstance(func, ast.Attribute) and func.attr in ENVIRON_METHODS and self.is_environ(module, func.value):
                return self._first_argument(node)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and self.is_environ(module, node.value):
            return node.slice
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and self.is_environ(module, node.comparators[0])
        ):
            return node.left
        return None

    @staticmethod
    def _first_argument(call: ast.Call) -> ast.expr | None:
        if call.args:
            return call.args[0]
        return next((k.value for k in call.keywords if k.arg == "key"), None)

    def _scopes(self, module: Module, node: ast.AST):
        """The functions enclosing a node, innermost first."""
        function = module.enclosing(node, FUNCTIONS)
        while function is not None:
            yield function
            function = module.enclosing(function, FUNCTIONS)

    def _local_getenv(self, module: Module, name: ast.Name) -> bool:
        for function in self._scopes(module, name):
            local = self._local_values(function).get(name.id)
            if local is not None:
                return any(module.is_getenv(value) for value in local)
        return False

    def is_environ(self, module: Module, expr: ast.expr, seen: frozenset = frozenset()) -> bool:
        """Whether expr can hold the environment."""
        if not isinstance(expr, ast.Name) and module.holds_environ(expr):
            return True
        if isinstance(expr, (ast.IfExp, ast.BoolOp)):
            parts = [expr.body, expr.orelse] if isinstance(expr, ast.IfExp) else expr.values
            return any(self.is_environ(module, part, seen) for part in parts)
        if not isinstance(expr, ast.Name) or (module.name, expr.id, id(expr)) in seen:
            return False
        seen = seen | {(module.name, expr.id, id(expr))}
        for function in self._scopes(module, expr):
            local = self._local_values(function).get(expr.id)
            if local is not None:
                return any(self.is_environ(module, value, seen) for value in local) or (
                    expr.id in _parameters(function) and self._environ_param(module, function, expr.id, seen)
                )
            if expr.id in _parameters(function):
                return self._environ_param(module, function, expr.id, seen)
        return expr.id in module.environ_names

    def _environ_param(self, module: Module, function: ast.FunctionDef, param: str, seen: frozenset) -> bool:
        """Whether any call passes the environment to this parameter, or it defaults to it."""
        key = (module.name, function.lineno, param)
        if key in self._environ_params:
            return self._environ_params[key]
        if key in seen:
            self._cycle = True
            return False
        seen = seen | {key}
        outer, self._cycle = self._cycle, False
        default = _default(function, param)
        result = default is not None and self.is_environ(module, default, seen)
        for caller, call in self._calls_of(module, function):
            if result:
                break
            try:
                argument = _argument(function, call, param)
            except Unresolved:
                continue
            result = argument is not None and self.is_environ(caller, argument, seen)
        if result or not self._cycle:
            self._environ_params[key] = result
        self._cycle = outer or self._cycle
        return result

    # ── what key it reads ──
    def resolve(self, module: Module, expr: ast.expr, seen: frozenset) -> set[str]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.JoinedStr):
            results = {""}
            for part in expr.values:
                if isinstance(part, ast.Constant):
                    options = {part.value}
                elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
                    options = self._formatted(module, part.value, seen)
                else:
                    raise Unresolved(ast.unparse(expr))
                results = {r + o for r in results for o in options}
            return results
        if isinstance(expr, ast.Name):
            return self._name(module, expr, seen)
        raise Unresolved(ast.unparse(expr))

    def _formatted(self, module: Module, value: ast.expr, seen: frozenset) -> set[str]:
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr in {"upper", "lower"}
        ):
            if not value.args:
                return {getattr(v, value.func.attr)() for v in self.resolve(module, value.func.value, seen)}
        return self.resolve(module, value, seen)

    def _name(self, module: Module, name: ast.Name, seen: frozenset) -> set[str]:
        if (module.name, id(name)) in seen:
            raise Unresolved(f"{name.id}, which refers to itself")
        seen = seen | {(module.name, id(name))}
        node: ast.AST = name
        while node in module.parents:
            node = module.parents[node]
            if isinstance(node, COMPREHENSIONS):
                for generator in node.generators:
                    if name.id in _bound(generator.target):
                        return self._iterated(module, generator.iter, generator.target, name.id, seen)
            if isinstance(node, (ast.For, ast.AsyncFor)) and name.id in _bound(node.target):
                in_body = (n for s in node.body for n in (s, *_in_scope(s)))
                if any(t == name.id for n in in_body for t, _ in _assignments(n)):
                    raise Unresolved(f"{name.id}, reassigned in its loop")
                return self._iterated(module, node.iter, node.target, name.id, seen)
            if isinstance(node, ast.Lambda) and name.id in _parameters(node):
                raise Unresolved(f"{name.id}, a lambda parameter")
            if isinstance(node, FUNCTIONS):
                local = self._local_values(node).get(name.id, [])
                keys = set().union(*(self.resolve(module, value, seen) for value in local))
                if name.id in _parameters(node):
                    return keys | self._call_sites(module, node, name.id, seen)
                if local:
                    return keys
        return self._module_constant(module, name.id, seen)

    def _module_constant(self, module: Module, name: str, seen: frozenset) -> set[str]:
        if (module.name, name) in seen:
            raise Unresolved(f"{name}, which refers to itself")
        seen = seen | {(module.name, name)}
        values = []
        for statement in module.tree.body:
            if isinstance(statement, ast.ImportFrom) and (source := self.imported(module, statement)):
                for alias in statement.names:
                    if (alias.asname or alias.name) == name:
                        return self._module_constant(source, alias.name, seen)
            values += [value for target, value in _assignments(statement) if target == name]
        if not values:
            raise Unresolved(name)
        return set().union(*(self.resolve(module, value, seen) for value in values))

    def _collection(self, module: Module, expr: ast.expr, seen: frozenset = frozenset()) -> tuple[Module, ast.expr]:
        """A collection literal and the module it is in, looking through a name:
        an enclosing function's local, or a module's, imported or not."""
        if isinstance(expr, ast.Name):
            if (module.name, expr.id) in seen:
                raise Unresolved(f"{expr.id}, which refers to itself")
            seen = seen | {(module.name, expr.id)}
            scope = module.enclosing(expr, FUNCTIONS) if expr in module.parents else None
            while scope is not None:
                local = self._local_values(scope).get(expr.id)
                if local:
                    if len(local) > 1:
                        raise Unresolved(f"{expr.id}, assigned more than once")
                    return self._collection(module, local[0], seen)
                scope = module.enclosing(scope, FUNCTIONS)
            for statement in module.tree.body:
                if isinstance(statement, ast.ImportFrom) and (source := self.imported(module, statement)):
                    for alias in statement.names:
                        if (alias.asname or alias.name) == expr.id:
                            return self._collection(source, ast.Name(alias.name), seen)
                values = [value for target, value in _assignments(statement) if target == expr.id]
                if values:
                    if sum(1 for s in module.tree.body for t, _ in _assignments(s) if t == expr.id) > 1:
                        raise Unresolved(f"{expr.id}, assigned more than once")
                    return self._collection(module, values[0], seen)
        if isinstance(expr, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
            return module, expr
        raise Unresolved(ast.unparse(expr))

    def _iterated(self, module: Module, iterable: ast.expr, target: ast.expr, name: str, seen: frozenset) -> set[str]:
        position = None if isinstance(target, ast.Name) else _bound(target).index(name)
        view = None
        if isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Attribute) and not iterable.args:
            view, iterable = iterable.func.attr, iterable.func.value
        home, collection = self._collection(module, iterable)
        if isinstance(collection, ast.Dict):
            if None in collection.keys:
                raise Unresolved(f"{ast.unparse(collection)}, which spreads another mapping")
            items = collection.values if view == "values" or (view == "items" and position == 1) else collection.keys
        else:
            items = collection.elts
            if position is not None:
                items = [e.elts[position] if isinstance(e, ast.Tuple) else e for e in items]
        return set().union(*(self.resolve(home, item, seen) for item in items))

    def _call_sites(self, module: Module, function: ast.FunctionDef, param: str, seen: frozenset) -> set[str]:
        values = sorted(
            {c.name for c, node in self.as_values.get(function.name, []) if self._may_mean(module, function, c, node)}
        )
        if values:
            raise Unresolved(f"{function.name} is passed as a value in {', '.join(values)}")
        calls = self._calls_of(module, function)
        if not calls:
            raise Unresolved(f"{param} of {function.name}, which nothing calls")
        keys: set[str] = set()
        for caller, call in calls:
            argument = _argument(function, call, param)
            if argument is None:
                argument = _default(function, param)
            if argument is None:
                raise Unresolved(f"{function.name}(...) in {caller.name} passes no {param}")
            keys |= self.resolve(caller, argument, seen)
        return keys
