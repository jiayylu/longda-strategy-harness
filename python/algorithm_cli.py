"""Read-only algorithm validation, with structured failures."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'reference'))
from algorithm import AlgorithmProgram

if __name__=='__main__':
    req=json.loads(Path(sys.argv[2]).read_text())
    try:
        p=AlgorithmProgram(req['source'])
        result=dict(valid=True,algorithm_hash=p.hash,stage='syntax_and_api_only',complete_schedule=False)
    except (ValueError,SyntaxError) as e:
        result=dict(valid=False,error=str(e),failure_kind='program_error',proof_scope='none')
    print(json.dumps(result))
