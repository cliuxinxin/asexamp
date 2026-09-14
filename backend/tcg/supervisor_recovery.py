"""Restart reconciliation and wake-up of a supervisor's durable queue."""
import asyncio
import copy
from contextlib import suppress

from .schemas import DomainError
from .storage import now, uid


def continuation_turn(plan):
    return {'id': uid('planreply_'), 'client_message_id': '', 'project_id': plan['project_id'],
        'chat_id': plan['chat_id'], 'created_at': now(), 'status': 'running',
        'message': '', 'parts': [], 'pending': [], 'actions': [], '_runtime': 'native'}


def recover_plans(supervisor):
    """Only durable tool receipts prove a side effect completed before a crash."""
    for plan in supervisor.store.list('execution_plan'):
        if plan['status'] != 'running':
            continue
        step = next((s for s in plan['steps'] if s['status'] == 'running'), None)
        if step and not step['receipts']:
            with suppress(DomainError):
                receipt = supervisor.store.get('execution_step_receipt', 'receipt:' + step['id'])
                if receipt.get('chat_id') == plan['chat_id']:
                    step['receipts'].append(receipt)
        if step and step['receipts']:
            supervisor._receipt_state(plan, step)
        elif step:
            step.update(status='blocked', message='服务中断时本步骤结果尚未确认，请查看成果后重新提出未完成要求。')
            plan.update(status='blocked', _block_reason='uncertain_effect')
        if plan['status'] != 'cancelled' and all(s['status'] in ('completed', 'cancelled') for s in plan['steps']):
            plan['status'] = 'completed'
        elif plan['status'] == 'running':
            plan['_resume_after_restart'] = True
        supervisor.save(plan)


async def reconcile_approval(supervisor, plan):
    prompt = plan.get('_waiting_prompt') or {}
    if not prompt.get('id'):
        return
    try:
        receipt = supervisor.store.get('native_approval_receipt', 'approval:' + prompt['id'])
    except DomainError as exc:
        if exc.status == 404:
            return
        raise
    if receipt.get('chat_id') != plan['chat_id'] or receipt.get('project_id') != plan['project_id']:
        raise DomainError('确认回执与执行计划不属于同一对话', 409)
    turn = continuation_turn(plan)
    turn['parts'] = copy.deepcopy(receipt.get('parts', []))
    turn['message'] = '已恢复确认后的后续计划。'
    turn['status'] = 'succeeded'
    await supervisor.after_control(plan['chat_id'], prompt, turn,
        rejected=receipt['status'] == 'cancelled', applied=receipt)


async def continue_run(supervisor, plan):
    store = supervisor.store
    run = store.run(plan['_waiting_run_id'])
    if run['chat_id'] != plan['chat_id'] or run['project_id'] != plan['project_id']:
        raise DomainError('后台任务不属于执行计划所在对话', 409)
    step = next(s for s in plan['steps'] if s['status'] == 'waiting_pipeline')
    if run['status'] in ('queued', 'running', 'waiting'):
        return
    if run['status'] == 'cancelled':
        plan['status'] = 'cancelled'
        for remaining in plan['steps']:
            if remaining['status'] not in ('completed', 'cancelled'):
                remaining.update(status='cancelled', message='后台任务已取消，后续操作已取消。')
        supervisor.save(plan)
        return
    if run['status'] != 'completed':
        message = '后台任务尚未完成，尾部操作保持暂停；请先重试或取消当前流程。'
        if step.get('message') != message:
            step['message'] = message
            supervisor.save(plan)
        return
    ids = list(dict.fromkeys(run.get('artifact_ids', []) +
        ([run['current_artifact_id']] if run.get('current_artifact_id') else [])))
    candidates = [store.get('artifact', aid) for aid in ids]
    candidates = [a for a in candidates if a.get('_visible') and a['chat_id'] == plan['chat_id']]
    for artifact in candidates:
        plan['_bindings'][artifact['id']] = artifact['revision']
    preferred = next((a for a in reversed(candidates) if a['type'] == 'cases'), None) or next(
        (a for a in reversed(candidates) if a['type'] == 'scenarios'), None)
    if preferred:
        plan['_output_artifact_id'] = preferred['id']
    step.update(status='completed', message='后台任务已完成')
    plan['status'] = 'running'
    supervisor.save(plan)
    turn = continuation_turn(plan)
    await supervisor.execute(plan, turn)


async def watch_plans(supervisor):
    """A tiny wake-up loop watches receipts; it does not implement pipeline gates."""
    while True:
        await asyncio.sleep(1)
        for saved in supervisor.store.list('execution_plan'):
            if saved['status'] not in ('waiting_pipeline', 'waiting_confirmation') and not saved.get('_resume_after_restart'):
                continue
            try:
                # All queue paths use the same order: chat mutex, then plan mutex.
                # Pipeline inference runs independently, so chat remains available at gates.
                async with supervisor.owner._chat_locks.setdefault(saved['chat_id'], asyncio.Lock()):
                    plan = supervisor.store.get('execution_plan', saved['id'])
                    supervisor.store.get('chat', plan['chat_id'])
                    if plan['status'] == 'waiting_confirmation':
                        # after_control owns the plan mutex, so do not acquire it twice.
                        await reconcile_approval(supervisor, plan)
                        continue
                    async with supervisor.locks.setdefault(plan['id'], asyncio.Lock()):
                        if plan['status'] == 'waiting_pipeline':
                            await continue_run(supervisor, plan)
                        elif plan.pop('_resume_after_restart', False) and plan['status'] == 'running':
                            supervisor.save(plan)
                            await supervisor.execute(plan, continuation_turn(plan))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A deleted chat or malformed pointer affects this queue only.
                with suppress(Exception):
                    plan = supervisor.store.get('execution_plan', saved['id'])
                    plan.update(status='blocked', _block_reason='resume_failed')
                    plan.pop('_resume_after_restart', None)
                    step = next((s for s in plan['steps'] if s['status'] not in ('completed', 'cancelled')), None)
                    if step:
                        step.update(status='blocked', message=str(exc)[:500])
                    supervisor.save(plan)
                    turn = continuation_turn(plan)
                    turn.update(status='failed', message='后续计划已暂停：' + str(exc)[:500])
                    from .supervisor import plan_part
                    turn['parts'].append(plan_part(plan))
                    supervisor.owner._save(turn)
