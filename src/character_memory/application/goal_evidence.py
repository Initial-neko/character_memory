"""Read current real facts in one Person's authoritative visibility scope."""
import hashlib
import json
from character_memory.domain.experience_scope import require_real_experience
from character_memory.domain.facts import FactRef
from character_memory.group_store import GroupRepository

class GoalMissing(LookupError):pass
class GoalConflict(ValueError):pass
class EvidenceUnavailable(GoalConflict):pass

def evidence_digest(store,character_id,reference):
    reference=FactRef.model_validate(reference)
    try:
        if reference.kind=='EVENT':
            fact=store.fact_reader.resolve_direct(reference, character_id=character_id)
            if fact is None:raise EvidenceUnavailable('EVIDENCE_UNAVAILABLE')
        else:
            fact=GroupRepository.read_source_fact(store,reference.conversation_id,reference.id,character_id)
            if fact is None:raise EvidenceUnavailable('EVIDENCE_UNAVAILABLE')
        if fact.metadata.get('metadata_status')=='INVALID':
            raise EvidenceUnavailable('EVIDENCE_UNAVAILABLE')
        require_real_experience(fact.metadata)
        raw=json.dumps(fact.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()
    except (ValueError,TypeError) as error:
        raise EvidenceUnavailable('EVIDENCE_UNAVAILABLE') from error
