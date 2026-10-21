"""Low-overhead inclusive/exclusive timers; resource limits are search limits, not rules."""
import functools, resource, sys, time

class Metrics:
    def __init__(self):
        self.rows = {}; self.stack = []
    def snapshot(self):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return dict(components=self.rows, peak_rss_mb=rss/(1024*1024 if sys.platform=='darwin' else 1024),
                    timing='inclusive includes children; exclusive excludes measured children')

def measured(name):
    def decorate(fn):
        @functools.wraps(fn)
        def call(self,*args,**kwargs):
            m=self.metrics; frame=[time.perf_counter(),0.0];m.stack.append(frame)
            try:return fn(self,*args,**kwargs)
            finally:
                elapsed=time.perf_counter()-frame[0];m.stack.pop()
                if m.stack:m.stack[-1][1]+=elapsed
                row=m.rows.setdefault(name,dict(calls=0,inclusive_s=0.0,exclusive_s=0.0))
                row['calls']+=1;row['inclusive_s']+=elapsed;row['exclusive_s']+=max(0,elapsed-frame[1])
        return call
    return decorate
