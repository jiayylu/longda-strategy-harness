"""Inspect prompts, algorithm versions and Memory without credentials."""
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('kind',choices=['prompts','algorithms','memory','run']);p.add_argument('path');a=p.parse_args();root=Path(a.path)
if a.kind=='memory':
    db=sqlite3.connect(f'file:{root.resolve()}?mode=ro',uri=True)
    rows=db.execute('select id,profile,strategy,outcome from experiences order by created desc').fetchall()
    print(json.dumps([dict(id=r[0],context=json.loads(r[1]),strategy=json.loads(r[2]),outcome=json.loads(r[3])) for r in rows],ensure_ascii=False,indent=2))
elif a.kind=='prompts':
    for f in sorted((root/'calls').glob('*/request.json')):
        print(json.dumps(dict(file=str(f),**json.loads(f.read_text())),ensure_ascii=False,indent=2))
elif a.kind=='algorithms':
    for f in sorted(set((root/'algorithm-versions').glob('*.json')) | set(root.glob('**/algorithm_versions/*.json'))):print(json.dumps(dict(file=str(f),**json.loads(f.read_text())),ensure_ascii=False,indent=2))
else:
    file=root/('report.json' if (root/'report.json').exists() else 'progress.json')
    print(file.read_text())
