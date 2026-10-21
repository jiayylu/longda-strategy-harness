def construct():
    saved=restore()
    if not saved and not inspect(root())["has_observed_state"]:
        rows=inspect(root())["order_details"]
        buckets={}
        for row in rows:
            if row["route"] not in buckets:buckets[row["route"]]=[]
            buckets[row["route"]].append(row["order"])
        ordered=[]
        for level in range(max([len(v) for v in buckets.values()])):
            for bucket in buckets.values():
                if level<len(bucket):ordered.append(bucket[level])
        width=min(len(buckets),inspect(root())["furnace_capacity"])
        waves=[ordered[i:i+width] for i in range(0,len(ordered),width)]
        for policy in [3,0,1,2,4,5]:
            state=batches(waves,policy=policy)
            if state is not None:return state
    frames=saved["frames"] if saved else [{"state":root(),"options":None,"next":0}]
    while frames and remaining()>0:
        checkpoint({"frames":frames})
        frame=frames[-1]
        state=frame["state"]
        if complete(state):return state
        if frame["options"] is None:
            opts=candidates(state)
            frame["options"]=sorted([(c["start"]+(c["min_hold"]*.15 if c["kind"]=="admit" else c["slack"]*.15),c["id"]) for c in opts])
        if frame["next"]>=len(frame["options"]):
            frames.pop()
        else:
            action=frame["options"][frame["next"]][1]
            child=advance(state,action)
            frame["next"]=frame["next"]+1
            frames.append({"state":child,"options":None,"next":0})
    checkpoint({"frames":frames})
    return None
