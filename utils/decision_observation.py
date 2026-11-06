"""Observation-only decision outcomes; never changes routing or calls a model."""
KEY='_gcp_decision_observation_v1'
STATUSES={'allow','decline','empty_response','protocol_failed','provider_unavailable','failed','timeout','cancelled','unknown'}
def _put(event,value):
    try:event.set_extra(KEY,value)
    except Exception:
        try:setattr(event,KEY,value)
        except Exception:pass
def _get(event):
    try:return event.get_extra(KEY,None)
    except Exception:return getattr(event,KEY,None)
def reset(event,source='model'):
    _put(event,{'source':source,'status':'unknown'})
    try:event._decision_ai_error=False
    except Exception:pass
def record(event,status):
    old=_get(event)
    _put(event,{'source':old.get('source','model') if isinstance(old,dict) else 'model','status':status if status in STATUSES else 'unknown'})
def resolve(event,result):
    data=_get(event)
    if not isinstance(data,dict):return 'unknown'
    status=data.get('status','unknown')
    if data.get('source')=='rule' and status=='unknown':return 'rule_allow' if result else 'rule_decline'
    return status if status in STATUSES else 'unknown'
def finish(event,trace,flow,result):
    status=resolve(event,result)
    if trace:trace.end('decision',status)
    if not result and flow:
        reason={'decline':'no_reply','rule_decline':'filtered','cancelled':'cancelled','timeout':'decision_timeout',
                'empty_response':'decision_empty','protocol_failed':'decision_protocol','provider_unavailable':'decision_provider',
                'failed':'decision_failed','unknown':'decision_unknown'}.get(status,'decision_unknown')
        flow.exit(event,reason)
    return status
