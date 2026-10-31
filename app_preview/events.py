"""Preview-only events. Standard run events (start/phase/out/err/stats/top/end) still go to ``plugins.build_execution.events.BUS``
so the existing Live Run tab shows preview runs too; everything specific to the preview panel goes to ``PV``.

PV event types: pv_state, pv_trace, pv_out, pv_stop, pv_vars, pv_eval, pv_break, pv_exc, pv_note, pv_frame, pv_web, pv_diag.
"""
from plugins.build_execution.events import RunBus

PV = RunBus()
