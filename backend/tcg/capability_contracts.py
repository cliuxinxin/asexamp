"""Capability-owned targeting, effects and bounded context contracts."""
import copy

BUSINESS_TARGETS = ('analysis', 'scenarios', 'cases', 'review')


def contract(definition, *, target_types=(), target_required=False, context_policy='metadata', prepare=None):
    value = {**definition, 'target_types': list(target_types), 'target_required': target_required,
             'context_policy': context_policy}
    if prepare is not None:
        value['prepare'] = prepare
    return value


def prepare_review(arguments):
    """Legacy optimize spelling is normalized by the capability, never the controller."""
    return {'arguments': copy.deepcopy(arguments), 'effect': 'write' if arguments.get('optimize') is True else 'read'}


def prepare_command(definition, arguments):
    prepared = definition['prepare'](copy.deepcopy(arguments)) if definition.get('prepare') else {'arguments': copy.deepcopy(arguments)}
    return prepared['arguments'], prepared.get('effect', definition['effect'])
