#!/usr/bin/env python3
"""
查「函数里用了一个哪儿都没定义的名字」。

用法：
    python check_undefined_names.py *.py

为什么需要：语法检查（ast.parse）抓不到这类错——代码语法完全合法，要真跑到
那一行才 NameError。而录制脚本里出事的那一行往往是录完一整段之后才走到的，
等于要录满一小时才炸一次；现场就这么丢过一段的配对文件。

按作用域栈查，所以闭包（内层函数读外层变量）不会误报——那是合法的。
第一次跑就抓到两个真问题：run_cameras 忘了把 pair_filter 往下传给
_run_one_segment，以及 hicc_ble_live.py 用了 struct 却没 import。
"""
import ast, builtins, sys

BUILTINS = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__spec__', '__builtins__'}
SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _params(fn):
    if isinstance(fn, ast.ClassDef):
        return set()
    a = fn.args
    out = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
    if a.vararg: out.add(a.vararg.arg)
    if a.kwarg: out.add(a.kwarg.arg)
    return out


def _iter_scope(node):
    """遍历这个作用域内的节点，不钻进嵌套函数/lambda 的函数体"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, SCOPE):
            continue            # 嵌套作用域由它自己那轮处理
        if isinstance(n, ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _bound(node):
    """这个作用域里被绑定的名字"""
    names = _params(node) if isinstance(node, SCOPE) else set()
    for n in _iter_scope(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                names.add((al.asname or al.name).split('.')[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
        elif isinstance(n, (ast.comprehension,)):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def check(path):
    tree = ast.parse(open(path, encoding='utf-8').read(), path)
    bad = []

    def descend(node, scopes):
        for n in _iter_scope(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if not any(n.id in s for s in scopes) and n.id not in BUILTINS:
                    bad.append((n.lineno, getattr(node, 'name', '<module>'), n.id))
        # 嵌套作用域：带上自己这一层再往下
        for n in _iter_scope(node):
            if isinstance(n, SCOPE) or isinstance(n, ast.ClassDef):
                descend(n, scopes + [_bound(n)])

    descend(tree, [_bound(tree)])
    return sorted(set(bad))


rc = 0
for p in sys.argv[1:]:
    for line, fn, name in check(p):
        print(f'{p}:{line}: {fn}() 用了未定义的 {name!r}')
        rc = 1
if rc == 0:
    print('✓ 没发现未定义的名字')
sys.exit(rc)
