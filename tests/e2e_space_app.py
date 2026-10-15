"""The deterministic browser app with Character Space routes attached."""

from e2e_realtime_app import app

from character_memory.space_web import attach_space_routes
from character_memory.character_onboarding import CharacterOnboardingService

# Keep actual persona registration/group creation, but never contact avatar or
# voice providers from this deterministic browser fixture.
CharacterOnboardingService.ensure_avatar = lambda *args, **kwargs: {"status": "unavailable", "reason": "E2E fixture"}
CharacterOnboardingService.ensure_voice = lambda *args, **kwargs: {"status": "unavailable", "reason": "E2E fixture"}


attach_space_routes(app)
from character_memory.ensemble_web import attach_ensemble_routes
attach_ensemble_routes(app)
