"""Compose interpreted preference candidates with the existing explicit Profile.

Effective feedback is explicit first: a stored explicit choice is never replaced by
an interpretation. Applying a candidate is an ordinary Profile CAS write, so the
version/history/restore/clear semantics stay in one owner.

Lifecycle: PENDING -> APPLIED (a normal new profile version) or DISMISSED. An
applied candidate can be REVERTED by re-writing its base values as another new
version; history is never rewritten. Clearing the profile expires every open
interpretation, so an old comment cannot offer a cleared value back.
"""
from character_memory.domain.experience_candidates import (
    CandidateApply, CandidateDismiss, candidate_fields, interpret_feedback,
)
from character_memory.domain.experience_profile import PreferenceChanges
from character_memory.storage.experience_candidates import (
    CandidateConflict, CandidateMissing, PreferenceCandidateRepository,
)
from character_memory.storage.experience_profiles import ExperienceProfileRepository, ProfileConflict

_STATUS_ORDER = {'PENDING': 0, 'SATISFIED': 1, 'OVERRIDDEN': 2, 'APPLIED': 3, 'REVERTED': 4,
                 'DISMISSED': 5, 'SUPERSEDED': 6, 'WITHDRAWN': 7}


class FeedbackCandidateService:
    def __init__(self, store, profiles=None, candidates=None):
        self.store = store
        self.profiles = profiles or ExperienceProfileRepository(store)
        self.candidates = candidates or PreferenceCandidateRepository(store)

    def _view(self, row, profile, interpretation):
        fields, satisfied, overridden = candidate_fields(interpretation, profile['preferences'])
        decided = row.get('candidate_status')
        if decided == 'APPLIED' and row.get('candidate_applied_version') != profile['version']:
            # The active version moved on. Keep the historical apply fact, but it
            # is no longer the current effect of this candidate.
            status = 'SUPERSEDED'
        elif decided:
            status = decided
        elif row.get('withdrawn_at') is not None:
            status = 'WITHDRAWN'
        elif fields:
            status = 'PENDING'
        elif satisfied:
            status = 'SATISFIED'
        else:
            status = 'OVERRIDDEN'
        return {
            'signal_id': row['id'], 'comment': row['comment'],
            'scope': interpretation.scope, 'fields': interpretation.fields,
            'applicable_fields': fields, 'satisfied_fields': satisfied,
            'overridden_fields': overridden,
            # What the operator explicitly holds today, per proposed field, so the
            # UI can show a real before/after comparison without guessing.
            'current_fields': {field: profile['preferences'].get(field) for field in interpretation.fields},
            'evidence': [item.model_dump() for item in interpretation.evidence],
            'note': interpretation.note, 'status': status,
            'applied_version': row.get('candidate_applied_version'),
            'base_version': row.get('candidate_base_version'),
            'decided_at': row.get('candidate_decided_at'),
            'source_withdrawn': row.get('withdrawn_at') is not None,
            'source_profile_version': row['profile_version'],
        }

    def list(self):
        with self.store._lock:
            profile = self.profiles.get()
            rows = self.candidates.sources()
        items = []
        for row in rows:
            interpretation = interpret_feedback(row['comment'])
            if interpretation is None:
                # Withdrawal clears the authored text. Keep the retired candidate
                # visible as history instead of silently dropping it.
                if row.get('withdrawn_at') is None and row.get('candidate_status') is None:
                    continue
                items.append(self._retired(row))
                continue
            items.append(self._view(row, profile, interpretation))
        items.sort(key=lambda item: (_STATUS_ORDER.get(item['status'], 9), item['signal_id']))
        return {'profile_version': profile['version'], 'preferences': profile['preferences'],
                'candidates': items}

    def _retired(self, row):
        return {'signal_id': row['id'], 'comment': None, 'scope': None, 'fields': {},
                'applicable_fields': {}, 'satisfied_fields': [], 'overridden_fields': [],
                'current_fields': {},
                'evidence': [], 'note': '来源反馈已撤回，不再提供偏好建议。',
                'status': 'WITHDRAWN' if row.get('withdrawn_at') is not None else row.get('candidate_status'),
                'applied_version': row.get('candidate_applied_version'),
                'base_version': row.get('candidate_base_version'),
                'decided_at': row.get('candidate_decided_at'),
                'source_withdrawn': row.get('withdrawn_at') is not None,
                'source_profile_version': row['profile_version']}

    def _source(self, signal_id):
        row = self.candidates.decision(signal_id)
        if row is None:
            raise CandidateMissing('candidate source is unavailable')
        if row.get('withdrawn_at') is not None:
            raise CandidateConflict('candidate source was withdrawn')
        interpretation = interpret_feedback(row['comment'])
        if interpretation is None:
            if row.get('candidate_status') is not None:
                raise CandidateConflict('candidate was already decided; read the current profile')
            # Authored text that never yielded a candidate (corrected into
            # something unsupported) has nothing to apply.
            raise CandidateMissing('candidate source is unavailable')
        return row, interpretation

    def apply(self, signal_id, request: CandidateApply):
        with self.store.transaction(immediate=True):
            row, interpretation = self._source(signal_id)
            if row.get('candidate_request_id') == request.request_id and row.get('candidate_status') == 'APPLIED':
                # Idempotent replay: return the recorded outcome without a second write.
                return {'candidate': self._view(row, self.profiles.get(), interpretation),
                        'applied_profile_version': row['candidate_applied_version']}
            if row.get('withdrawn_at') is not None:
                raise CandidateConflict('candidate source was withdrawn')
            if row.get('candidate_status') is not None:
                raise CandidateConflict('candidate was already decided; read the current profile')
            profile = self.profiles.get()
            if profile['version'] != request.expected_profile_version:
                raise ProfileConflict('experience profile changed; read the latest version')
            if interpretation.scope == 'SCENE' and not request.as_long_term:
                raise CandidateConflict('scene-scoped feedback needs explicit long-term confirmation')
            fields, _, _ = candidate_fields(interpretation, profile['preferences'])
            if not fields:
                decided = self.candidates.mark_noop(signal_id, request_id=request.request_id)
                return {'candidate': self._view(decided, profile, interpretation),
                        'applied_profile_version': None}
            result = self.profiles.patch(profile['version'],
                                         PreferenceChanges.model_validate(fields).model_dump(exclude_unset=True))
            decided = self.candidates.mark_applied(signal_id, request_id=request.request_id,
                                                   applied_version=result['version'],
                                                   base_version=profile['version'])
            return {'candidate': self._view(decided, result, interpretation),
                    'applied_profile_version': result['version']}

    def dismiss(self, signal_id, request: CandidateDismiss):
        with self.store.transaction(immediate=True):
            _, interpretation = self._source(signal_id)
            decided = self.candidates.mark_dismissed(signal_id, request_id=request.request_id)
            return {'candidate': self._view(decided, self.profiles.get(), interpretation)}

    def revert(self, signal_id, request):
        """Undo an applied candidate by re-writing its base values as a new version.

        History is never rewritten: the applied version stays intact and the undo
        is another ordinary Profile PATCH. Only fields still holding the inferred
        value are touched, so a later explicit choice is not clobbered.
        """
        with self.store.transaction(immediate=True):
            row, interpretation = self._source(signal_id)
            if row.get('candidate_request_id') == request.request_id and row.get('candidate_status') == 'REVERTED':
                return {'candidate': self._view(row, self.profiles.get(), interpretation),
                        'applied_profile_version': row['candidate_applied_version']}
            if row.get('candidate_status') != 'APPLIED':
                raise CandidateConflict('only an applied candidate can be reverted')
            profile = self.profiles.get()
            if profile['version'] != request.expected_profile_version:
                raise ProfileConflict('experience profile changed; read the latest version')
            base = row.get('candidate_base_version')
            if base is None:
                raise CandidateConflict('candidate has no restorable base version')
            base_preferences = {}
            if base > 0:
                snapshot = next((item for item in self.profiles.versions(limit=100)
                                 if item['version'] == base), None)
                if snapshot is None:
                    raise CandidateConflict('the candidate base version is no longer available')
                base_preferences = snapshot['preferences']
            changes = {field: base_preferences.get(field)
                       for field in interpretation.fields
                       if profile['preferences'].get(field) == interpretation.fields[field]}
            if not changes:
                raise CandidateConflict('the applied preference was already changed elsewhere')
            result = self.profiles.patch(profile['version'],
                                         PreferenceChanges.model_validate(changes).model_dump(exclude_unset=True))
            decided = self.candidates.mark_reverted(signal_id, request_id=request.request_id,
                                                    reverted_version=result['version'])
            return {'candidate': self._view(decided, result, interpretation),
                    'applied_profile_version': result['version']}

    def invalidate(self):
        """Called after a Profile clear: old source text must not resurface."""
        self.candidates.supersede_all()
