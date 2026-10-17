"""Pure prompt composition: inputs are snapshots, never permission or tool owners."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class PromptParts:
    persona: str = ''
    state_memory: str = ''
    perception: str = ''
    capabilities: str = ''
    runtime_policy: str = ''

    def render(self) -> str:
        return '\n\n'.join(part.strip() for part in (
            self.persona, self.state_memory, self.perception,
            self.capabilities, self.runtime_policy) if part.strip()) + '\n'


def render_capabilities(capabilities=()) -> str:
    """Render only descriptions already filtered by the server's discovery owner.

    This layer does not read sidecars, probe availability, reserve quota or run
    tools. Descriptor fields match CapabilityDescription without importing the
    optional discovery implementation into the baseline runtime.
    """
    items = []
    for item in capabilities or ():
        items.append({'id':item.id, 'summary':item.summary,
            'arguments_schema':item.arguments_schema, 'channel':item.channel,
            'backend':item.backend, 'skills':[{'name':s['name'], 'sha256':s.get('sha256','')}
                                            for s in item.skills]})
    if not items:
        return ''
    return '# Available Capabilities\n' + json.dumps(items, ensure_ascii=False) + (
        '\n这些是服务端本轮提供的能力描述，不代表已经调用、读过网页或完成行动。'
        '可以选择不调用；执行仍须经过服务端权限、频道和额度校验。')
