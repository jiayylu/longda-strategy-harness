"""Pure, bounded generated construction policies. No I/O or rule mutation."""
import ast
import copy
import hashlib
import math
import sys

BUILTINS = dict(len=len, min=min, max=max, sum=sum, sorted=sorted,
                range=range, enumerate=enumerate, abs=abs, float=float,
                int=int, round=round, list=list, dict=dict, zip=zip,
                any=any, all=all)
ALLOWED = {ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
           ast.Assign, ast.AugAssign, ast.For, ast.If, ast.IfExp, ast.Break,
           ast.Continue, ast.Pass, ast.Expr, ast.Name, ast.Load, ast.Store,
           ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice,
           ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.keyword,
           ast.ListComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
           ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
           ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
           ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn}


class Program:
    def __init__(self, source):
        if not isinstance(source, str) or len(source) > 10000:
            raise ValueError("Program source must be <=10000 characters")
        tree = ast.parse(source)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise ValueError("Exactly one function rank(candidates, state) required")
        fn = tree.body[0]
        if fn.name != 'rank' or [a.arg for a in fn.args.args] != ['candidates','state'] or fn.decorator_list or fn.args.defaults or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
            raise ValueError("Signature must be rank(candidates, state)")
        nodes = list(ast.walk(tree))
        if len(nodes) > 1800:
            raise ValueError("Program is too large")
        for n in nodes:
            if type(n) not in ALLOWED:
                raise ValueError("Forbidden program syntax: " + type(n).__name__)
            if isinstance(n, ast.Name) and n.id.startswith('_'):
                raise ValueError("Private names forbidden")
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and '__' in n.value:
                raise ValueError("Private keys forbidden")
            if isinstance(n, ast.FunctionDef) and n is not fn:
                raise ValueError("Nested functions forbidden")
            if isinstance(n, ast.Call) and (not isinstance(n.func, ast.Name) or n.func.id not in BUILTINS):
                raise ValueError("Only pure whitelisted builtins may be called")
        scope = {'__builtins__': BUILTINS}
        exec(compile(tree, '<generated-constructor>', 'exec'), scope)
        self.fn = scope['rank']
        self.hash = hashlib.sha256(source.encode()).hexdigest()

    def scores(self, candidates, state):
        steps = [0]
        def trace(frame, event, arg):
            if frame.f_code.co_filename == '<generated-constructor>':
                frame.f_trace_opcodes = True
                if event == 'opcode':
                    steps[0] += 1
                    if steps[0] > 150000:
                        raise ValueError('Generated program instruction budget exceeded')
                return trace
            return None
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            result = self.fn(copy.deepcopy(candidates), copy.deepcopy(state))
        finally:
            sys.settrace(previous)
        if not isinstance(result, (list, tuple)) or len(result) != len(candidates):
            raise ValueError('rank must return one numeric score per candidate')
        if any(type(x) not in (int, float) or not math.isfinite(x) or abs(x)>1e15 for x in result):
            raise ValueError('rank scores must be finite bounded numbers')
        return result
