"""Intake recovery and approval events, stored through the selected adapter.

Demo concurrency is serialized by the backend process lock. Production deployment
must replace this with database transactions/CAS across workers.
"""
import copy
import hashlib
import json
import uuid


def events(B, card_id=None, kind=None):
    filt = {'property': {'property': '关联卡片ID', 'text': {'equals': card_id}}} if card_id else None
    out = []
    for r in B.lc.query(B.TBL['version_snapshots'], filt=filt):
        try:
            d = json.loads(B.val(r, '快照JSON', '{}'))
        except (ValueError, TypeError):
            continue
        if kind and d.get('kind') != kind:
            continue
        d['_record_id'] = r['record_id']
        out.append(d)
    return sorted(out, key=lambda e: e.get('time', ''))


def append(B, object_id, kind, **data):
    event = dict(data, kind=kind, time=B._now().isoformat(timespec='microseconds'))
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    result = B.lc.add(B.TBL['version_snapshots'], [{
        '关联卡片ID': {'text': object_id}, '版本号': {'text': uuid.uuid4().hex},
        '变更日期': {'date': event['time']}, '变更原因': {'text': kind},
        '变更依据': {'text': data.get('actor', '')}, '快照JSON': {'text': raw}}])
    item = result[0] if result else {}
    rid = item.get('record_id') or item.get('id') or item.get('_id')
    stored = B.lc.get_record(B.TBL['version_snapshots'], rid) if rid else None
    if not stored or B.val(stored, '快照JSON') != raw:
        raise RuntimeError('操作记录尚未回读确认，请保留草稿重试')
    return event


def latest_intake(B, card_id):
    found = events(B, card_id, 'intake-v2')
    return found[-1] if found else None


def owner(B, card_id):
    found = latest_intake(B, card_id)
    return found.get('actor') if found else None


def can_edit(B, cur, card_id):
    return cur.get('role') in (B.ROLE_KEEPER, B.ROLE_ADMIN) and owner(B, card_id) == cur.get('account')


def save_intake(B, cur, body):
    if cur.get('role') not in (B.ROLE_KEEPER, B.ROLE_ADMIN) or not cur.get('account'):
        return {'ok': False, 'error': '无权保存合同草稿'}
    request = str(body.get('request_id', ''))
    if not request or len(request) > 100:
        return {'ok': False, 'error': '缺少草稿标识，请保留当前页面'}
    with B._glob:
        card_id = body.get('card_id')
        prior = [e for e in events(B, kind='intake-v2')
                 if e.get('request_id') == request and e.get('actor') == cur['account']]
        if prior:
            card_id = prior[-1]['card_id']
        payload = copy.deepcopy(body)
        payload.pop('_token', None)
        if not isinstance(payload.get('card'), dict):
            return {'ok': False, 'error': '合同信息格式错误'}
        if card_id and not can_edit(B, cur, card_id):
            return {'ok': False, 'error': '只能恢复本人创建的草稿'}
        if card_id:
            card = B.lc.get_record(B.TBL['cards'], card_id)
            if B.val(card or {}, '审批状态') not in ('草稿', '退回'):
                return {'ok': False, 'error': '待审或已通过合同不能覆盖'}
        else:
            result = B.create_card(cur, payload['card'])
            card_id = result.get('card_id')
            if not result.get('ok') or not card_id:
                return result
        completed = []
        try:
            append(B, card_id, 'intake-v2', actor=cur['account'], request_id=request,
                   card_id=card_id, payload=payload, complete=False)
            saved = B.save_card_basic(cur, card_id, payload['card'])
            if not saved.get('ok'):
                raise ValueError(saved.get('error', '基础信息保存失败'))
            completed.append('基础信息')
            steps = [('付款原始明细', lambda: B.save_payment_draft(cur, card_id, payload.get('rows', [])))]
            if payload.get('segments'):
                steps.append(('计费分段', lambda: B.save_amount_overview(cur, card_id, payload['segments'])))
            if payload.get('sharing'):
                steps.append(('分摊', lambda: B.save_shares(cur, card_id, payload['sharing'])))
            if payload.get('oneoff'):
                steps.append(('一次性费用', lambda: B.save_oneoff(cur, card_id, payload['oneoff'])))
            for label, action in steps:
                result = action()
                if not result.get('ok'):
                    raise ValueError(label + '：' + result.get('error', '保存失败'))
                completed.append(label)
            append(B, card_id, 'intake-v2', actor=cur['account'], request_id=request,
                   card_id=card_id, payload=payload, complete=True)
            return {'ok': True, 'card_id': card_id, 'write_confirmed': True, 'completed': completed}
        except Exception as exc:
            return {'ok': False, 'card_id': card_id, 'completed': completed,
                    'error': str(exc), 'recoverable': True}


def submit(B, cur, card_id):
    with B._glob:
        if not can_edit(B, cur, card_id):
            return {'ok': False, 'error': '提交需要本人账号关联的草稿；旧数据请先完成归属迁移'}
        draft = latest_intake(B, card_id)
        if not draft.get('complete'):
            return {'ok': False, 'error': '草稿保存未完成，请恢复保存后再提交'}
        result = B._legacy_submit_card(cur, card_id)
        if result.get('ok'):
            ticket = B._find_ticket_active(card_id)
            append(B, card_id, 'approval-v2', actor=cur['account'], name=cur['name'],
                   action='提交', ticket_id=ticket['record_id'], payload=draft['payload'])
        return result


def active_submit(B, card_id):
    entries = events(B, card_id, 'approval-v2')
    if entries and entries[-1].get('action') == '提交':
        return entries[-1]
    # 兼容早期版本创建的卡片：它们没有 approval-v2 快照，
    # 但仍有正式 approval_tickets 待审工单。
    ticket = B._find_ticket_active(card_id)
    if ticket:
        return {
            'kind': 'approval-v2-legacy',
            'action': '提交',
            'ticket_id': ticket.get('record_id'),
            'actor_name': B.val(ticket, '提交人', ''),
            'time': B.val(ticket, '提交时间', ''),
        }
    return None


def decide(B, cur, card_id, action, opinion):
    with B._glob:
        if cur.get('role') not in (B.ROLE_APPROVER, B.ROLE_ADMIN):
            return {'ok': False, 'error': '无审批权限'}
        sub = active_submit(B, card_id)
        if not sub:
            return {'ok': False, 'error': '缺少有效提交记录或不允许审批本人提交'}
        submitter_account = sub.get('actor')
        submitter_name = sub.get('actor_name')
        if (submitter_account and submitter_account == cur.get('account')) or (submitter_name and submitter_name == cur.get('name')):
            return {'ok': False, 'error': '缺少有效提交记录或不允许审批本人提交'}
        if action == '退回' and not opinion.strip():
            return {'ok': False, 'error': '请填写退回原因'}
        card = B.lc.get_record(B.TBL['cards'], card_id)
        if B.val(card or {}, '审批状态') != '待审':
            return {'ok': False, 'error': '状态已变化，请刷新'}
        B.lc.update_checked(B.TBL['approval_tickets'], [{'record_id':sub['ticket_id'], 'properties':{
            '状态':{'select':action}, '审批人':{'text':cur['name']}, '意见':{'text':opinion or action},
            '审批时间':{'date':B._now().isoformat()}}}])
        B._set_card_state(card_id, action)
        append(B, card_id, 'approval-v2', actor=cur['account'], name=cur['name'],
               action=action, opinion=opinion, ticket_id=sub['ticket_id'])
        # Actual payment/vouchers must not be created as payment facts by approval.
        return {'ok':True, 'state':action}


def withdraw(B, cur, card_id, opinion):
    with B._glob:
        sub = active_submit(B, card_id)
        if not sub or sub['actor'] != cur.get('account'):
            return {'ok':False, 'error':'只有本次提交账号可以撤回'}
        card = B.lc.get_record(B.TBL['cards'], card_id)
        if B.val(card or {}, '审批状态') != '待审':
            return {'ok':False, 'error':'仅待审状态可以撤回'}
        # Existing table schema has no guaranteed withdrawal select option.
        B.lc.update_checked(B.TBL['approval_tickets'], [{'record_id':sub['ticket_id'], 'properties':{
            '状态':{'select':'退回'}, '意见':{'text':'[提交人撤回] '+opinion},
            '审批时间':{'date':B._now().isoformat()}}}])
        B._set_card_state(card_id, '草稿')
        append(B, card_id, 'approval-v2', actor=cur['account'], name=cur['name'], action='撤回', opinion=opinion)
        return {'ok':True,'state':'草稿'}


def batches(B):
    latest = {}
    for e in events(B, kind='payment-v2'):
        latest[e['batch']] = e
    return latest


def submit_payment(B, cur, record_ids):
    if cur.get('role') not in (B.ROLE_KEEPER,B.ROLE_ADMIN) or not record_ids or len(set(record_ids)) != len(record_ids):
        return {'ok':False,'error':'无权提交或付款选择为空/重复'}
    with B._glob:
        busy = {r for b in batches(B).values() if b['state'] not in ('退回','撤回') for r in b['rows']}
        for rid in record_ids:
            row = B.lc.get_record(B.TBL['pay_plan'], rid)
            if not row or rid in busy or B.val(row,'已付',False):
                return {'ok':False,'error':'明细不存在、已提交或已支付，请刷新'}
            amount = B.dnum(B.val(row,'计划付款金额',0))
            card_id = B.val(row,'关联卡片ID')
            card = B.lc.get_record(B.TBL['cards'], card_id)
            if not amount.is_finite() or amount <= 0 or B.val(card or {},'审批状态') != '通过':
                return {'ok':False,'error':'只能提交已批准合同的有效正数付款'}
            if owner(B,card_id) != cur.get('account'):
                return {'ok':False,'error':'只能提交本人负责合同的付款'}
        batch = 'ZF'+uuid.uuid4().hex[:20]
        append(B, '__payment__', 'payment-v2', actor=cur['account'], batch=batch,
               rows=record_ids, state='待审', opinion='', reviewer='')
        return {'ok':True,'batch':batch,'count':len(record_ids)}


def decide_payment(B, cur, batch, action, opinion):
    with B._glob:
        previous = batches(B).get(batch)
        if cur.get('role') not in ('付款审批人',B.ROLE_ADMIN) or not previous or previous['state'] != '待审':
            return {'ok':False,'error':'无权审批或批次状态已变化'}
        if previous['actor'] == cur.get('account'):
            return {'ok':False,'error':'不能审批本人提交的付款'}
        if action == '退回' and not opinion.strip():
            return {'ok':False,'error':'退回必须填写原因'}
        append(B,'__payment__','payment-v2',actor=previous['actor'],batch=batch,
               rows=previous['rows'],state=action,opinion=opinion,reviewer=cur['account'])
        return {'ok':True,'state':action}


def record_payment(B, cur, payment_id, body):
    with B._glob:
        if cur.get('role') not in (B.ROLE_KEEPER,B.ROLE_ADMIN,'资金管理处'):
            return {'ok':False,'error':'无付款回写权限'}
        row = B.lc.get_record(B.TBL['pay_plan'],payment_id)
        if not row or B.val(row,'已付',False):
            return {'ok':False,'error':'付款不存在或已记录，禁止重复回写'}
        approved = [b for b in batches(B).values() if payment_id in b['rows'] and b['state']=='通过']
        if not approved:
            return {'ok':False,'error':'必须先通过对应付款批次审批'}
        result = B._legacy_record_payment(cur,payment_id,body)
        if result.get('ok'):
            append(B,B.val(row,'关联卡片ID'),'payment-result-v2',actor=cur['account'],
                   payment_id=payment_id,batch=approved[-1]['batch'],result=body)
        return result
