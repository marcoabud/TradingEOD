"""Read-only paper recovery contract probe; no orders, state writes or messages."""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import requests
from .broker import Broker
from .core_recovery import CoreRecoveryStore, MAX_RECOVERY_DAYS, reconcile_intent
from .safety import NY, SafetyError


class ReadOnlyPaperSession(requests.Session):
    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        allowed = {'/v2/account', '/v2/positions', '/v2/orders', '/v2/account/activities/FILL'}
        if method.upper() != 'GET' or parsed.scheme != 'https' or parsed.netloc != 'paper-api.alpaca.markets' or parsed.path not in allowed:
            raise SafetyError('Recovery diagnostic transport refused request')
        kwargs.update(timeout=(5,15), allow_redirects=False)
        return super().request(method, url, **kwargs)


def probe(broker, store_factory=CoreRecoveryStore):
    account = broker.account()
    scope = hashlib.sha256(str(account.id).encode()).hexdigest()[:16]
    if scope != 'ba8a3aeebf4a89eb':
        raise SafetyError('Recovery diagnostic paper account scope mismatch')
    today = datetime.now(NY).date()
    store = store_factory(account.id, True, dry_run=True)
    intent = store.pending(today)
    result = {'mode':'paper', 'account_hash':scope, 'observed_at':datetime.now(NY).isoformat(),
              'intent':intent, 'equity':str(account.equity), 'cash':str(account.cash)}
    if intent is None:
        return dict(result, status='no_pending_intent')
    origin = datetime.fromisoformat(intent['started_at']).date()
    if not 0 <= (today-origin).days <= MAX_RECOVERY_DAYS:
        raise SafetyError('Recovery diagnostic date range invalid')
    orders, fills = [], []
    for offset in range((today-origin).days+1):
        day = origin+timedelta(days=offset)
        orders.extend(broker.orders(day))
        fills.extend(broker.fills_today(day))
    positions = broker.positions()
    result['positions'] = {s:{'symbol':s,'qty':str(p.qty)} for s,p in positions.items()}
    result['orders'] = [{**broker._order_diagnostic(o),
                         **{k:str(getattr(o,k,None)) for k in ('qty','filled_qty','filled_avg_price')}} for o in orders]
    result['fills'] = [{k:f.get(k) for k in ('id','order_id','symbol','side','qty','price','transaction_time','activity_type','type')} for f in fills]
    result['open_orders'] = [broker._order_diagnostic(o) for o in broker.orders(open_only=True)]
    try:
        result['reconciliation'] = reconcile_intent(intent,orders,fills,positions,today)
        result['status'] = 'reconciled' if not result['open_orders'] else 'open_orders'
    except SafetyError as exc:
        result.update(status='blocked',reason=str(exc))
    return result


def main():
    try:
        broker = Broker(os.environ['ALPACA_API_KEY'],os.environ['ALPACA_SECRET_KEY'],paper=True)
        broker.client._session.close()
        broker.client._session = ReadOnlyPaperSession()
        result = probe(broker)
    except Exception as exc:
        result = {'status':'error','reason':str(exc) if isinstance(exc,SafetyError) else type(exc).__name__}
    Path('recovery_diagnostics.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'reason':result.get('reason')}))
    return 0 if result['status'] in {'reconciled','no_pending_intent'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
