# Release and Version Policy

The current pre-release risk/feature/configuration audit is maintained in [RELEASE_READINESS.md](RELEASE_READINESS.md). This file defines policy; the readiness register records the current release decision surface.

Character Memory uses a lightweight release model for the current 0.x phase.

## Branch model

`main` is the active integration branch and must stay CI-green. Normal work lands through short-lived `feature/*`, `fix/*`, `refactor/*`, or `chore/*` branches.

There is deliberately no permanent `develop` or moving `stable` branch. A stable baseline is an immutable Git tag and GitHub Release, not another branch that can drift.

When a maintained stable line needs a hotfix after `main` has already moved to the next minor series, create a temporary maintenance branch such as `release/0.5` from the last stable tag, publish the patch release, then merge/cherry-pick the fix back to `main`.

## Version format

The Python package follows PEP 440:

```text
0.5.0rc1
0.5.0
0.5.1
0.6.0
```

Git tags and GitHub Releases use the human-readable form:

```text
v0.5.0-rc.1
v0.5.0
v0.5.1
v0.6.0
```

During the 0.x phase:

- minor versions (`0.5 -> 0.6`) may contain meaningful product or architecture changes;
- patch versions (`0.5.0 -> 0.5.1`) are bug fixes, compatibility fixes, performance work, and small UI corrections;
- release candidates are real acceptance baselines, but are not the long-term stable baseline.

## Release candidate flow

```text
feature/fix branches
        |
        v
      main
   CI must pass
        |
        v
 v0.5.0-rc.1
        |
  local/real usage soak
        |
        v
    v0.5.0
```

A release candidate requires:

1. Linux full pytest green.
2. Windows-sensitive smoke green.
3. Browser smoke green.
4. package version and `CHANGELOG.md` updated.
5. maintained architecture/docs reflect the shipped behavior.
6. no known data-loss, migration, startup, or durable-fact regression.

Promotion from RC to stable additionally requires a real local soak on the normal stack. GPU/model-provider acceptance is recorded separately from CI because CI intentionally does not require local model weights or CUDA.

## 0.5 line

The 0.5 line is the first **Persistent Person Runtime Baseline**: Direct, Group, Memory/Mental State, Voice, Vision/Image, and Character Space are all channels around the same persistent person model rather than separate personalities.

`0.5.0rc1` is the first release-candidate package version for this baseline.

## 1.0 boundary

Do not treat 1.0 as a feature-count milestone. It should wait until the project is prepared to make explicit compatibility promises around durable schema/migrations, configuration, public HTTP contracts, upgrade/rollback behavior, and release support.
