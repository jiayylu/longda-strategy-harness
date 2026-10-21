"""Original physical model with cold-start identity symmetry handled correctly.

Once a material has an observed history its identity cannot be swapped with
another material merely because their routes match. Disabling that cold-only
quotient is not a relaxation of furnace, process or transport feasibility.
"""
from longda_cpsat.model import build_model as original_build
from longda_cpsat.audit import audit as original_audit

def physical_scenario(sc):
    return {**sc,'quotient_identical_orders':False} if sc.get('production_state') else sc

def build_model(sc,*args,**kwargs):
    return original_build(physical_scenario(sc),*args,**kwargs)

def audit(sc,s):
    return original_audit(physical_scenario(sc),s)
