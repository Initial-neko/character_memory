"""Fail-closed boundary for the existing real Person; no simulation runtime."""
from typing import Any


def is_real_experience(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    if 'experience_scope' not in metadata:
        return True  # Existing facts retain their original real-world semantics.
    scope = metadata['experience_scope']
    return (isinstance(scope, dict) and set(scope) == {'schema_version', 'reality'}
            and type(scope.get('schema_version')) is int and scope['schema_version'] == 1
            and scope.get('reality') == 'REAL')


def require_real_experience(metadata: Any) -> None:
    if not is_real_experience(metadata):
        raise ValueError('experience scope cannot enter the real Person runtime')


def real_experience_sql(column: str) -> str:
    """Internal SQL fragment; column is a trusted source-code identifier only."""
    m = f"(CASE WHEN json_valid({column}) THEN CASE WHEN json_type({column})='object' THEN {column} ELSE '{{}}' END ELSE '{{}}' END)"
    return (f"(json_type({m})='object' AND (json_type({m},'$.experience_scope') IS NULL OR ("
            f"json_type({m},'$.experience_scope')='object' AND "
            f"json_type({m},'$.experience_scope.schema_version')='integer' AND "
            f"json_extract({m},'$.experience_scope.schema_version')=1 AND "
            f"json_extract({m},'$.experience_scope.reality')='REAL' AND "
            f"(SELECT COUNT(*) FROM json_each({m},'$.experience_scope'))=2)))")
